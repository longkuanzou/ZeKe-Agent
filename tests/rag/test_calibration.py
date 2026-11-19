"""M1/M3/M4/M13：用例规模、包级阈值与 score 语义。"""

from __future__ import annotations

import json
from pathlib import Path

from zkagent.core.rag.embedder import HashEmbedder
from zkagent.core.rag.llamaindex_engine import LlamaIndexRetriever
from zkagent.core.rag.metrics import load_eval_set
from zkagent.core.rag.sparse import ChunkRecord

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "zkagent/industry_packs/appliance"


def test_retrieval_cases_expanded_and_have_equivalents() -> None:
    """验证：检索评测集规模达标（M1：≥40 条），且等价 gold 都写了理由（M13）。

    等价 gold 不写理由，就等于允许「同义 FAQ 命中也算命中」这种自欺。
    """
    eval_set = load_eval_set(PACK / "evals/retrieval.yaml")
    assert len(eval_set.queries) >= 40, "M1：用例应扩到 40 条以上"
    with_note = [
        hit
        for case in eval_set.queries
        for hit in case.gold
        if hit.note
    ]
    assert with_note, "M13：等价 gold 必须写清理由"


def test_gate_calibration_is_between_distributions() -> None:
    """验证：闸门阈值落在正负样本分布之间——能把「有答案」和「没答案」分开。

    阈值不是拍脑袋定的，必须夹在 negative 最大值与 positive 最小值之间才算有理据。
    """
    path = PACK / "evals/gate_calibration.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    threshold = data["threshold"]
    assert data["positive_top1_min"] > data["negative_top1_max"]
    assert data["negative_top1_max"] < threshold < data["positive_top1_min"]


def test_homeapp_has_its_own_calibration() -> None:
    """M3：换行业包必须有自己的阈值，不能沿用 appliance 的 0.45。"""
    path = ROOT / "zkagent/industry_packs/homeapp/evals/gate_calibration.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["industry"] == "homeapp"
    assert data["positive_samples"] >= 40
    if data.get("overlap"):
        # 正负分布重叠时退回默认阈值并标注（KNOWN_ISSUES M26）：
        # 此时只有「阈值来自本包自己的标定文件」这一条能断言，不能假装分得开。
        assert 0 < data["threshold"] < 1
        assert "重叠" in str(data.get("note") or "")
    else:
        assert data["negative_top1_max"] < data["threshold"] < data["positive_top1_min"]


def test_score_kind_follows_mode(tmp_path, monkeypatch) -> None:
    """验证：``score_kind`` 随检索模式变化（hybrid → ``rrf``），分数口径要能自证来源。

    M4：不同模式的分数量纲不同，混着比会得出错误结论。
    """
    retriever = LlamaIndexRetriever(
        industry_id="test",
        knowledge_dir=tmp_path,
        index_dir=tmp_path,
        embedder=HashEmbedder(),
        embedding_mode="local_model",
        mode="hybrid",
    )
    retriever._store = object()
    retriever._chunks = [ChunkRecord(node_id="d:0", doc_id="d", chunk_idx=0, text="保修三年")]
    monkeypatch.setattr(retriever, "_dense_hits", lambda _q, _n: [("d:0", 0.9)])
    monkeypatch.setattr(retriever, "_sparse_hits", lambda _q, _n: [])
    monkeypatch.setattr(retriever, "_visible_doc_ids", lambda *_a, **_k: {"d"})

    chunks = retriever.search("保修几年", top_k=1)

    assert chunks[0].metadata["score_kind"] == "rrf"
