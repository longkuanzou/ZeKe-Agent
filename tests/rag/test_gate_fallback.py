"""S1：hash 降级模式用关键词覆盖率兜底，并显式声明「依据未核实」。"""

from __future__ import annotations

from pathlib import Path

import pytest

from zkagent.core.rag.embedder import FALLBACK_MODE, HashEmbedder
from zkagent.core.rag.gate import text_coverage
from zkagent.core.rag.knowledge_tool import KnowledgeSearchTool
from zkagent.core.rag.llamaindex_engine import LlamaIndexRetriever
from zkagent.core.rag.sparse import ChunkRecord
from zkagent.core.types import Chunk, ToolContext


def test_coverage_separates_related_from_unrelated() -> None:
    """验证：关键词覆盖率能分开「相关」与「完全不相关」——它是降级模式的兜底闸门。"""
    related = text_coverage("空调保修几年", "本机型空调整机保修三年，主要部件保修五年。")
    unrelated = text_coverage("空调保修几年", "红楼梦金陵十二钗判词与人物关系。")

    assert related > 0.5
    assert unrelated == 0.0


def _retriever(tmp_path: Path) -> LlamaIndexRetriever:
    return LlamaIndexRetriever(
        industry_id="test",
        knowledge_dir=tmp_path,
        index_dir=tmp_path,
        embedder=HashEmbedder(),
        embedding_mode=FALLBACK_MODE,
        mode="hybrid",
    )


def test_fallback_search_drops_low_coverage_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证：hash 降级模式下，向量分虽高但**覆盖率不足**的候选仍被闸门丢弃。

    hash 嵌入只有字面相似度，不靠覆盖率兜底就会召回「红楼梦」这类噪声。
    """
    retriever = _retriever(tmp_path)
    retriever._store = object()  # 跳过真实加载；本用例只验证闸门
    retriever._chunks = [
        ChunkRecord(node_id="d1:0", doc_id="d1", chunk_idx=0, text="空调整机保修三年。"),
        ChunkRecord(node_id="d2:0", doc_id="d2", chunk_idx=0, text="红楼梦判词与人物关系。"),
    ]
    monkeypatch.setattr(
        retriever, "_dense_hits", lambda _q, _n: [("d1:0", 0.9), ("d2:0", 0.8)]
    )
    monkeypatch.setattr(retriever, "_sparse_hits", lambda _q, _n: [])
    monkeypatch.setattr(
        retriever, "_visible_doc_ids", lambda *_a, **_k: {"d1", "d2"}
    )

    trace: dict = {}
    chunks = retriever.search("空调保修几年", top_k=5, trace=trace)

    assert [chunk.node_id for chunk in chunks] == ["d1:0"]
    assert trace["gate"]["coverage_dropped"] == 1
    assert trace["gate"]["coverage_min"] > 0


def test_all_candidates_dropped_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证：候选全被丢弃时返回空并在 trace 里写明「覆盖率不足」——不得拿次优结果凑数。"""
    retriever = _retriever(tmp_path)
    retriever._store = object()
    retriever._chunks = [
        ChunkRecord(node_id="d:0", doc_id="d", chunk_idx=0, text="红楼梦判词。")
    ]
    monkeypatch.setattr(retriever, "_dense_hits", lambda _q, _n: [("d:0", 0.99)])
    monkeypatch.setattr(retriever, "_sparse_hits", lambda _q, _n: [])
    monkeypatch.setattr(retriever, "_visible_doc_ids", lambda *_a, **_k: {"d"})

    trace: dict = {}
    assert retriever.search("空调保修几年", top_k=5, trace=trace) == []
    assert trace["gate"]["passed"] is False
    assert "覆盖率不足" in trace["gate"]["reason"]


class _FakeRetriever:
    embedding_mode = FALLBACK_MODE

    def search(self, *_args, **_kwargs):
        return [
            Chunk(
                text="空调整机保修三年。",
                source="warranty.md",
                node_id="warranty.md:0",
                score=0.5,
                metadata={"doc_type": "policy", "journey": "after_sales"},
            )
        ]


def test_knowledge_tool_marks_result_unverified_in_fallback() -> None:
    """验证：降级模式下答案必须标记 ``degraded`` / ``verified=False`` 并声明「依据未核实」。

    对应 S1：宁可明说降级，也不能让 hash 召回的结论冒充语义检索的结论。
    """
    tool = KnowledgeSearchTool(lambda: _FakeRetriever())  # type: ignore[arg-type]

    result = tool.invoke(
        "knowledge_search",
        {"query": "空调保修几年"},
        ToolContext(trace_id="t1", session_id="s", user_id="U1"),
    )

    assert result.ok is True
    assert result.data["degraded"] is True
    assert result.data["verified"] is False
    assert "未核实" in result.data["note"]
