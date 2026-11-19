"""索引构建、增量更新与重建判定（对应 DESIGN §6.3.1 / §6.3.3 与 P3 Gate）。

关键判定：

1. **首次全量**、再次执行**零变动**（``--dry-run`` 连跑两次必须是 0/0/0）；
2. 新增 / 修改 / 删除分别被识别，且**未变动的文档不重新入库**；
3. 分块参数或嵌入实现变化 → **拒绝增量**并要求 ``--rebuild``（不允许旧向量配新模型）；
4. 索引缺失时检索必须显式报错，而不是安静返回空结果。
"""

from __future__ import annotations

import pytest

from zkagent.core.rag.embedder import HashEmbedder
from zkagent.core.rag.errors import RagIndexError, RetrievalError
from zkagent.core.rag.llamaindex_engine import LlamaIndexRetriever
from zkagent.core.rag.metadata import KnowledgeMetaError

from conftest import DEFAULT_DOCS, DEFAULT_META, write_knowledge

#: 删除 ``returns.md`` 之后应使用的 meta.yaml（声明必须与磁盘一致）。
META_AFTER_DELETION = """version: 1
defaults:
  doc_type: faq
  acl_tags: []
  roles: []
docs:
  - doc_id: warranty.md
    doc_type: policy
  - doc_id: internal/limits.md
    doc_type: internal
    acl_tags: [internal]
    roles: [agent]
"""


def test_first_index_is_full_and_counts_chunks(make_retriever) -> None:
    """验证：首次建索引为 full 模式，全量入库并落盘 manifest / chunks / vector_store 三件套。"""
    retriever = make_retriever()

    report = retriever.index()

    assert report.mode == "full"
    assert report.added == ["internal/limits.md", "returns.md", "warranty.md"]
    assert report.removed == [] and report.modified == []
    assert report.chunk_total >= 3
    assert report.embedding_mode == "fallback_hash"
    assert report.embed_dim == 64
    assert (retriever.index_dir / "manifest.json").exists()
    assert (retriever.index_dir / "chunks.json").exists()
    assert (retriever.index_dir / "vector_store.json").exists()


def test_second_run_reports_no_changes(make_retriever) -> None:
    """Gate 判定：``--dry-run`` 再次执行应为 0/0/0（增量幂等）。"""
    retriever = make_retriever()
    retriever.index()

    report = retriever.index(dry_run=True)

    assert report.mode == "dry-run"
    assert (report.added, report.modified, report.removed) == ([], [], [])
    assert report.unchanged == ["internal/limits.md", "returns.md", "warranty.md"]


def test_incremental_after_second_index_reports_unchanged(make_retriever) -> None:
    """验证：二次构建走 incremental 且全部标为 unchanged——未变动的文档不重新入库。"""
    retriever = make_retriever()
    retriever.index()

    report = retriever.index()

    assert report.mode == "incremental"
    assert report.unchanged == ["internal/limits.md", "returns.md", "warranty.md"]
    assert report.added == [] and report.modified == []


def test_added_modified_removed_are_applied(make_retriever, tmp_path) -> None:
    """验证：新增 / 修改 / 删除三类变更都真正落到索引与元数据，未变的不重复入库。"""
    knowledge = write_knowledge(tmp_path)
    retriever = make_retriever(knowledge)
    retriever.index()

    # 改一篇、删一篇（同时删掉 meta.yaml 里的声明，见下一个用例）、加一篇
    (knowledge / "warranty.md").write_text("整机保修两年，压缩机保修八年。", encoding="utf-8")
    (knowledge / "returns.md").unlink()
    (knowledge / "logistics.md").write_text("发货后可在订单详情查看运单号。", encoding="utf-8")
    (knowledge / "meta.yaml").write_text(META_AFTER_DELETION, encoding="utf-8")

    report = retriever.index()

    assert report.mode == "incremental"
    assert report.modified == ["warranty.md"]
    assert report.removed == ["returns.md"]
    assert report.added == ["logistics.md"]
    assert report.unchanged == ["internal/limits.md"]

    retriever.load()
    hits = retriever.search("保修两年", top_k=3)
    assert any(chunk.source == "warranty.md" and "两年" in chunk.text for chunk in hits)


def test_chunk_size_change_forces_full_rebuild(make_retriever) -> None:
    """参数已变的索引不能增量：宁可重建，也不产出不可比的检索结果。"""
    first = make_retriever(chunk_size=100)
    first.index()

    # 同一索引目录，但分块参数变了
    second = make_retriever(chunk_size=40, index_dir=first.index_dir)
    report = second.index()

    assert report.mode == "full", "参数不匹配时必须走全量重建"
    assert report.rebuild_reasons, "必须给出重建原因，便于排障"


def test_explicit_rebuild_rebuilds_everything(make_retriever) -> None:
    """验证：``rebuild=True`` 走全量且不做增量比对（全部算 added），是参数变更后的逃生通道。"""
    retriever = make_retriever()
    retriever.index()
    (retriever.knowledge_dir / "warranty.md").write_text("改了内容", encoding="utf-8")

    report = retriever.index(rebuild=True)

    assert report.mode == "full"
    assert report.modified == [], "全量重建不做增量比对"
    assert report.added == ["internal/limits.md", "returns.md", "warranty.md"]


def test_dry_run_writes_nothing(make_retriever) -> None:
    """验证：``--dry-run`` 只出计划不落盘——可以先看会改什么，再决定是否真建。"""
    retriever = make_retriever()

    report = retriever.index(dry_run=True)

    assert report.added  # 报告里有计划
    assert not retriever.index_dir.exists(), "--dry-run 不得写任何文件"


def test_search_without_index_fails_loudly(make_retriever) -> None:
    """验证：索引不存在时检索报 ``RagIndexError``——不得安静返回空结果冒充「知识库没依据」。"""
    retriever = make_retriever()

    with pytest.raises(RagIndexError, match="索引不存在"):
        retriever.search("保修")


def test_search_rejects_empty_query(make_retriever) -> None:
    """验证：空查询报 ``RetrievalError``——空白查询的「相似度」没有意义。"""
    retriever = make_retriever()
    retriever.index()

    with pytest.raises(RetrievalError, match="不能为空"):
        retriever.search("   ")


def test_meta_referencing_missing_doc_fails(make_retriever) -> None:
    """meta.yaml 里写错路径的代价很高，必须显式失败而不是静默忽略。

    若静默忽略，被写错的条目会退回 ``defaults``（公开），**受限文档会因此降级为公开**——
    这是安全相关的失败，所以宁可让索引构建直接报错。
    """
    retriever = make_retriever(
        meta=DEFAULT_META + "  - doc_id: not/exists.md\n    doc_type: policy\n"
    )

    with pytest.raises(KnowledgeMetaError, match="不存在"):
        retriever.index()


def test_deleting_a_document_requires_updating_meta(make_retriever, tmp_path) -> None:
    """删除工作流：文件与 meta.yaml 声明必须同时删，否则报错（上一条用例的安全理由）。"""
    knowledge = write_knowledge(tmp_path)
    retriever = make_retriever(knowledge)
    retriever.index()

    (knowledge / "returns.md").unlink()

    with pytest.raises(KnowledgeMetaError, match="不存在"):
        retriever.index(dry_run=True)

    (knowledge / "meta.yaml").write_text(META_AFTER_DELETION, encoding="utf-8")
    assert retriever.index(dry_run=True).removed == ["returns.md"]


def test_chunk_overlap_must_be_smaller_than_size(tmp_path) -> None:
    """验证：``chunk_overlap >= chunk_size`` 直接报错——相等会让分块无限递进、索引失控。"""
    knowledge = write_knowledge(tmp_path)

    with pytest.raises(RagIndexError, match="分块重叠"):
        LlamaIndexRetriever(
            industry_id="appliance",
            knowledge_dir=knowledge,
            index_dir=tmp_path / "indexes" / "appliance",
            embedder=HashEmbedder(32),
            chunk_size=50,
            chunk_overlap=50,
        )


def test_metadata_is_not_indexed_as_knowledge(make_retriever) -> None:
    """``meta.yaml`` 是配置不是知识正文，不得进入召回结果。"""
    retriever = make_retriever()
    retriever.index()

    hits = retriever.search("doc_type defaults acl_tags", top_k=5)

    assert all(chunk.source != "meta.yaml" for chunk in hits)
    assert DEFAULT_DOCS  # 语料来自 conftest，避免误删
