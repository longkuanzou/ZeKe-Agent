"""离线可跑与嵌入降级（对应 DESIGN §6.3.5 / §6.3.6 与 P3 Gate）。

两项判定：

1. **禁网也能建索引、也能检索**——并且用「联网 hook 计数为 0」证明真的没触网，
   而不是「恰好没失败」；
2. 本地模型不可用时**显式降级**为确定性 hash 向量并说明原因，且降级后的
   ``model_id`` 与真实模型不同（这会让下一次索引触发「嵌入模型不匹配 → 重建」，
   避免两种向量混进同一个索引）。
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

import zkagent.core.rag.embedder as embedder_module
from zkagent.core.rag.embedder import (
    FALLBACK_MODE,
    LOCAL_MODE,
    HashEmbedder,
    build_embedder,
)


@pytest.fixture()
def no_network(monkeypatch):
    """阻断所有对外连接，并统计被尝试的连接次数。"""
    attempts = {"count": 0}

    real_connect = socket.socket.connect

    def blocked_connect(self, address):  # noqa: ANN001 - 与标准库签名保持一致
        attempts["count"] += 1
        raise OSError(f"网络已被测试阻断：{address}")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    try:
        yield attempts
    finally:
        monkeypatch.setattr(socket.socket, "connect", real_connect)


def test_index_and_search_work_without_network(make_retriever, no_network) -> None:
    """禁网下全流程可用：建索引 → 检索 → 给出引用。"""
    retriever = make_retriever()

    report = retriever.index()
    hits = retriever.search("未发货订单只能取消", top_k=3)

    assert report.chunk_total >= 3
    assert report.embedding_mode == FALLBACK_MODE
    assert any(chunk.source == "returns.md" for chunk in hits)
    assert no_network["count"] == 0, f"不得尝试联网，实际尝试 {no_network['count']} 次"


def test_loading_index_offline_does_not_touch_network(make_retriever, no_network) -> None:
    """验证：加载已落盘索引同样不触网——「能建」还不够，「重启后能用」才算离线可用。"""
    retriever = make_retriever()
    retriever.index()

    reader = make_retriever(index_dir=retriever.index_dir)
    hits = reader.search("保修几年", top_k=3)

    assert hits
    assert no_network["count"] == 0


def test_hub_network_checks_are_bounded() -> None:
    """回归：HF 联网检查必须有超时上限，否则断网时命令挂起数分钟。

    实测背景：已缓存的模型在断网环境下默认加载 >150 秒仍未返回（每次加载都做 HEAD
    检查并按 1/2/4/8/16 秒重试），导致 P3 Gate 超时。
    """
    from zkagent.core.rag.embedder import _bound_hub_timeouts

    for name in ("HF_HUB_ETAG_TIMEOUT", "HF_HUB_DOWNLOAD_TIMEOUT"):
        os.environ.pop(name, None)
    _bound_hub_timeouts()

    assert int(os.environ["HF_HUB_ETAG_TIMEOUT"]) <= 10
    assert int(os.environ["HF_HUB_DOWNLOAD_TIMEOUT"]) <= 30

    # 不覆盖用户已有的显式设置
    os.environ["HF_HUB_ETAG_TIMEOUT"] = "99"
    _bound_hub_timeouts()
    assert os.environ["HF_HUB_ETAG_TIMEOUT"] == "99"


def test_cached_model_loads_without_network(no_network) -> None:
    """已缓存的本地模型必须能在**完全断网**下加载（不触网、不重试、秒开）。"""
    cache = Path(
        os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))
    ) / "hub" / "models--BAAI--bge-small-zh-v1.5"
    if not cache.exists():  # pragma: no cover - 其他机器/CI 上跳过
        pytest.skip("本机未缓存 bge-small-zh-v1.5，跳过该用例")

    bundle = build_embedder()

    assert bundle.mode == LOCAL_MODE
    assert bundle.embedder.dim == 512
    assert no_network["count"] == 0, f"不得尝试联网，实际 {no_network['count']} 次"


def test_download_can_be_forbidden(no_network, monkeypatch) -> None:
    """禁止下载时，缓存缺失必须**如实失败并降级**，而不是偷偷联网。"""
    monkeypatch.setenv("ZKAGENT_EMBED_ALLOW_DOWNLOAD", "0")
    assert embedder_module.download_allowed() is False

    bundle = build_embedder("BAAI/definitely-not-cached-model")

    assert bundle.mode == FALLBACK_MODE
    assert no_network["count"] == 0


# --------------------------------------------------------------------------- 降级


def test_build_embedder_falls_back_when_local_model_unavailable(monkeypatch) -> None:
    """本地模型不可用 → 降级并**说明原因**，而不是抛异常或悄悄换实现。"""

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003 - 模拟下载失败
        raise OSError("无法下载模型（模拟断网）")

    monkeypatch.setattr(embedder_module, "SentenceTransformerEmbedder", boom)

    bundle = build_embedder("BAAI/does-not-matter")

    assert bundle.mode == FALLBACK_MODE
    assert bundle.degraded is True
    assert "OSError" in bundle.detail and "本地模型不可用" in bundle.detail
    assert bundle.embedder.model_id != "BAAI/does-not-matter", "降级实现必须使用不同的 model_id"
    assert FALLBACK_MODE in bundle.describe()


def test_explicit_hash_mode_skips_local_model() -> None:
    """验证：显式关掉本地模型时直接走 hash 且标记降级——不偷偷尝试下载模型。"""
    bundle = build_embedder(prefer_local=False)
    assert bundle.mode == FALLBACK_MODE
    assert bundle.degraded is True


def test_hash_embedder_is_deterministic_and_normalized() -> None:
    """验证：hash 嵌入确定性 + 归一化——同一输入必得同一向量，索引才可复现、可比相似度。"""
    embedder = HashEmbedder(32)

    first = embedder.embed(["整机保修一年"])[0]
    second = embedder.embed(["整机保修一年"])[0]
    different = embedder.embed(["手机屏幕保修"])[0]

    assert first == second, "同一输入必须得到同一向量（否则索引不可复现）"
    assert first != different
    assert len(first) == 32
    assert abs(sum(value * value for value in first) - 1.0) < 1e-9, "向量应已归一化"


def test_degraded_embedder_triggers_rebuild_on_switch(make_retriever) -> None:
    """降级口径与正常口径**不能混在同一个索引里**：切换后必须重建。"""
    hash_retriever = make_retriever(dim=64)
    hash_retriever.index()

    # 换一个 model_id 的嵌入实现（模拟从 hash 切回真实模型）
    class _OtherEmbedder(HashEmbedder):
        @property
        def model_id(self) -> str:
            return "BAAI/bge-small-zh-v1.5"

    from zkagent.core.rag.llamaindex_engine import LlamaIndexRetriever

    switched = LlamaIndexRetriever(
        industry_id="appliance",
        knowledge_dir=hash_retriever.knowledge_dir,
        index_dir=hash_retriever.index_dir,
        embedder=_OtherEmbedder(64),
        embedding_mode=LOCAL_MODE,
        chunk_size=100,
        chunk_overlap=10,
    )

    report = switched.index()

    assert report.mode == "full", "嵌入模型变化必须全量重建"
    assert any("嵌入模型不匹配" in reason for reason in report.rebuild_reasons)
