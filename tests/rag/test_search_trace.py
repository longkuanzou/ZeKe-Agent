"""检索追踪：``search(trace=...)`` 必须给出**真实中间值**，而不是事后推测。

面板要展示的每一段（召回 / 融合 / 闸门 / 过滤 / 精排 / 最终）都从这里取数。
这里钉住两件事：

1. 各阶段的键与内容都存在，且 ``final`` 与实际返回的片段一致；
2. 被过滤的候选要带**原因**（复用 ``metadata.exclusion_reason``，与实际过滤同一份规则）。
"""

from __future__ import annotations

from zkagent.core.rag.llamaindex_engine import LlamaIndexRetriever


def test_trace_records_all_stages(make_retriever) -> None:
    """验证：trace 覆盖召回/融合/闸门/过滤/精排/最终六段，且 ``final`` 与实际返回**逐条一致**。

    面板靠这份数据画图，事后推测出来的中间值会让排查结论失真。
    """
    retriever: LlamaIndexRetriever = make_retriever()
    retriever.index()

    trace: dict = {}
    hits = retriever.search("签收后七天内可以无理由退货吗", top_k=2, trace=trace)

    assert hits
    assert trace["mode"] == "hybrid"
    assert trace["candidates"] >= trace["top_k"]
    assert trace["dense"] and trace["sparse"], "两路召回都要有记录"
    assert trace["gate"]["passed"] is True
    assert trace["fused"], "融合结果不能为空"
    assert trace["filter"]["total_docs"] == 3
    assert trace["filter"]["visible_docs"] == 2, "internal 文档默认不可见"
    # final 与实际返回一致（顺序与 doc_id 都要对得上）
    assert [item["node_id"] for item in trace["final"]] == [chunk.node_id for chunk in hits]
    assert {"dense", "sparse", "fuse"} <= set(trace["timings_ms"])


def test_trace_explains_dropped_candidates(make_retriever) -> None:
    """验证：被过滤掉的候选必须带原因（且与真实过滤共用 ``exclusion_reason`` 同一份规则）。

    只说「没召回」而不说「为什么没召回」，面板就没法定位是 ACL 还是链路不匹配。
    """
    retriever = make_retriever()
    retriever.index()

    trace: dict = {}
    retriever.search("超过自助额度的退款必须转人工审核", top_k=3, trace=trace)

    dropped = {item["doc_id"]: item["reason"] for item in trace["filter"]["dropped"]}
    assert dropped.get("internal/limits.md") == "acl", "受限文档被丢弃时必须写明原因是 acl"
    assert all(item["reason"] for item in trace["filter"]["dropped"])


def test_trace_absent_by_default(make_retriever) -> None:
    """不传 trace 时不产生额外结构，也不应报错。"""
    retriever = make_retriever()
    retriever.index()

    assert retriever.search("保修", top_k=2)


def test_candidate_window_expands_when_filters_narrow(make_retriever, monkeypatch) -> None:
    """过滤在融合之后、截断之前：带收窄条件时候选窗口必须放大，否则返回条数不足 k。

    固定窗口（``k * 3``）遇上型号过滤时常常只剩两三条可见候选，看起来像「语料里
    只有这几篇相关」；放大窗口是让「过滤」真的只做过滤，而不是变相砍召回。
    """
    from zkagent.core.rag.llamaindex_engine import _CANDIDATE_FACTOR

    retriever = make_retriever()
    retriever._meta = {f"d{index}": None for index in range(1000)}
    retriever._chunks = [object() for _ in range(1850)]  # noqa: SLF001 - 只需 len()
    monkeypatch.setattr(retriever, "_visible_doc_ids", lambda *_a, **_k: {"d0"})

    assert retriever._candidate_window(5, None) == 5 * _CANDIDATE_FACTOR
    widened = retriever._candidate_window(5, {"product_ids": ["m1"]})
    assert 5 * _CANDIDATE_FACTOR < widened <= 1850

    # 一条可见文档都没有时退回原窗口：查询仍要能返回空并解释原因
    monkeypatch.setattr(retriever, "_visible_doc_ids", lambda *_a, **_k: set())
    assert retriever._candidate_window(5, {"product_ids": ["m1"]}) == 5 * _CANDIDATE_FACTOR
