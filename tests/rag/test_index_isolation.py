"""行业隔离、检索引用与可见性过滤（对应 DESIGN §6.3.2 / §6.3.4 与 P3 Gate）。

三项判定：

1. **两个行业包的索引并存互不干扰**——每个实例各自持有嵌入实现与索引目录，
   证明没有依赖 LlamaIndex 的全局 ``Settings`` 单例；
2. 检索结果**带引用**（``source`` = 文档路径、``node_id`` = 可复现的分块标识）；
3. **可见性过滤在返回之前生效**：默认只返回公开文档，带标签才可见受限文档。
"""

from __future__ import annotations

from conftest import DEFAULT_DOCS, write_knowledge

OTHER_DOCS = {
    "phone_warranty.md": "手机的保修期为一年，屏幕不在保修范围内。",
    "phone_return.md": "手机支持七天无理由退货，需保持外包装完整。",
}

OTHER_META = """version: 1
defaults:
  doc_type: faq
  acl_tags: []
  roles: []
docs:
  - doc_id: phone_warranty.md
    doc_type: policy
"""


def test_two_industry_indexes_do_not_interfere(make_retriever, tmp_path) -> None:
    """appliance 与 3c 各自索引、各自检索，互不串味（反证未用全局 Settings）。"""
    appliance = make_retriever(write_knowledge(tmp_path / "appliance"), industry_id="appliance")
    three_c = make_retriever(
        write_knowledge(tmp_path / "3c", OTHER_DOCS, OTHER_META), industry_id="3c"
    )

    appliance.index()
    three_c.index()

    assert appliance.index_dir != three_c.index_dir
    assert appliance._embedder is not three_c._embedder, "嵌入实现必须由实例各自持有"

    appliance_hits = appliance.search("保修几年", top_k=3)
    three_c_hits = three_c.search("保修几年", top_k=3)

    assert {chunk.source for chunk in appliance_hits} <= set(DEFAULT_DOCS)
    assert {chunk.source for chunk in three_c_hits} <= set(OTHER_DOCS)
    assert all("手机" not in chunk.text for chunk in appliance_hits), "不得召回别的行业包文档"
    assert all("压缩机" not in chunk.text for chunk in three_c_hits)


def test_search_returns_citable_chunks(make_retriever) -> None:
    """回答要能给引用，因此 ``source`` 与 ``node_id`` 必须可复现、可定位。"""
    retriever = make_retriever()
    retriever.index()

    hits = retriever.search("保修几年", top_k=3)

    assert hits, "同一语料下的字面命中应当能召回"
    warranty = next(chunk for chunk in hits if chunk.source == "warranty.md")
    assert warranty.node_id.startswith("warranty.md:")
    assert warranty.score > 0
    assert warranty.metadata["doc_type"] == "policy"
    assert "doc_id" in warranty.metadata


def test_restricted_document_hidden_by_default(make_retriever) -> None:
    """默认最小可见集：未声明标签的调用方看不到 acl_tags 非空的文档。"""
    retriever = make_retriever()
    retriever.index()

    hits = retriever.search("退款必须转人工审核", top_k=5)

    assert all(chunk.source != "internal/limits.md" for chunk in hits), "内部口径不得泄露"


def test_restricted_document_visible_with_matching_tag(make_retriever) -> None:
    """验证：带上匹配标签后受限文档可见，且**同一次调用之外**不带标签仍不可见。

    与上一条成对：证明可见性由调用方标签决定，不是被永久放开。
    """
    retriever = make_retriever()
    retriever.index()

    hits = retriever.search("退款必须转人工审核", top_k=5, filters={"acl_tags": ["internal"]})

    assert any(chunk.source == "internal/limits.md" for chunk in hits)
    assert all(
        chunk.metadata["doc_type"] != "internal"
        for chunk in retriever.search("退款", top_k=5)
    ), "不带标签时仍不得返回受限文档"


def test_doc_type_filter_narrows_results(make_retriever) -> None:
    """验证：``doc_types`` 过滤只保留指定类型——检索收窄后不得混入其它类型。"""
    retriever = make_retriever()
    retriever.index()

    hits = retriever.search("保修 退货 审核", top_k=5, filters={"doc_types": ["faq"]})

    assert all(chunk.metadata["doc_type"] == "faq" for chunk in hits)


def test_allowed_roles_filter(make_retriever) -> None:
    """``allowed_roles`` 非空时要求与文档 roles 有交集。"""
    retriever = make_retriever()
    retriever.index()

    without_role = retriever.search("退款必须转人工审核", top_k=5, filters={"acl_tags": ["internal"]})
    with_role = retriever.search(
        "退款必须转人工审核",
        top_k=5,
        filters={"acl_tags": ["internal"], "allowed_roles": ["agent"]},
    )

    assert any(chunk.source == "internal/limits.md" for chunk in without_role)
    assert any(chunk.source == "internal/limits.md" for chunk in with_role)

    blocked = retriever.search(
        "退款必须转人工审核",
        top_k=5,
        filters={"acl_tags": ["internal"], "allowed_roles": ["someone-else"]},
    )
    assert all(chunk.source != "internal/limits.md" for chunk in blocked), "角色不匹配应被过滤"


def test_loaded_index_is_reusable_across_instances(make_retriever) -> None:
    """索引落盘后可被新实例加载并检索（进程重启后仍可用）。"""
    writer = make_retriever()
    writer.index()

    reader = make_retriever(index_dir=writer.index_dir)
    assert reader.is_loaded is False

    hits = reader.search("保修几年", top_k=3)

    assert reader.is_loaded is True
    assert any(chunk.source == "warranty.md" for chunk in hits)


def test_stats_reports_embedding_mode(make_retriever) -> None:
    """验证：``stats()`` 上报 industry / 嵌入模式 / 文档与分块数——降级口径必须可观测。"""
    retriever = make_retriever()
    retriever.index()

    stats = retriever.stats()

    assert stats["industry_id"] == "appliance"
    assert stats["embedding_mode"] == "fallback_hash", "降级口径必须可上报"
    assert stats["docs"] == len(DEFAULT_DOCS)
    assert stats["chunks"] >= len(DEFAULT_DOCS)
