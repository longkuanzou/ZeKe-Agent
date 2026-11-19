"""索引清单与增量比对的单元测试（框架无关）。

重点验证三件事：

1. ``doc_id`` 稳定且可定位（相对 ``knowledge/`` 的 POSIX 路径），内容哈希对换行不敏感；
2. 增量比对把「新增 / 修改 / 删除 / 未变」四类分开，且**未变不动**；
3. 任何「旧向量配新模型」的组合都必须被**拒绝增量**并要求全量重建。
"""

from __future__ import annotations

import json

import pytest

from zkagent.core.rag.manifest import (
    CHUNKER_VERSION,
    MANIFEST_SCHEMA_VERSION,
    DocEntry,
    IndexManifest,
    IndexManifestError,
    check_incremental_compatible,
    diff_documents,
    doc_id_for,
    iter_document_paths,
    load_manifest,
    manifest_path,
    save_manifest,
    scan_documents,
    sha256_text,
)


def _build_knowledge(tmp_path):
    """构造一个小的知识目录：两篇正文 + 一份 meta.yaml（元数据不参与检索）。"""
    knowledge = tmp_path / "knowledge"
    (knowledge / "manual").mkdir(parents=True)
    (knowledge / "warranty.md").write_text("保修一年", encoding="utf-8")
    (knowledge / "manual" / "install.md").write_text("免费安装", encoding="utf-8")
    (knowledge / "meta.yaml").write_text("docs: []\n", encoding="utf-8")
    return knowledge


def _manifest(docs: list[DocEntry], **overrides) -> IndexManifest:
    base = {
        "industry_id": "appliance",
        "embed_model_id": "BAAI/bge-small-zh-v1.5",
        "embed_dim": 512,
        "chunk_size": 500,
        "chunk_overlap": 50,
    }
    base.update(overrides)
    return IndexManifest(docs=docs, **base)


# --------------------------------------------------------------------------- 标识与哈希


def test_doc_id_is_stable_relative_posix_path(tmp_path) -> None:
    """验证：``doc_id`` 是相对知识目录的 POSIX 路径且反复取值一致。

    id 不稳定会让增量比对每次都判成「全变」，索引被迫全量重建。
    """
    knowledge = _build_knowledge(tmp_path)
    doc = knowledge / "manual" / "install.md"
    assert doc_id_for(knowledge, doc) == "manual/install.md"
    # 同一个文件反复取 id 必须一致（否则增量比对会全量重建）
    assert doc_id_for(knowledge, doc) == doc_id_for(knowledge, doc)


def test_doc_id_rejects_file_outside_knowledge_dir(tmp_path) -> None:
    """验证：目录外的文件直接报错——算不出相对路径就无从定位，也就不能入库。"""
    knowledge = _build_knowledge(tmp_path)
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(IndexManifestError, match="不在知识目录内"):
        doc_id_for(knowledge, outside)


def test_content_hash_ignores_line_ending_style() -> None:
    """验证：内容哈希对换行风格不敏感——跨平台编辑不该被判成「文档已修改」而触发重建。"""
    assert sha256_text("a\r\nb") == sha256_text("a\nb") == sha256_text("a\rb")
    assert sha256_text("a") != sha256_text("b")


# --------------------------------------------------------------------------- 扫描


def test_scan_skips_metadata_and_non_documents(tmp_path) -> None:
    """验证：扫描跳过 ``meta.yaml``、二进制与隐藏文件——配置与随手放的图不是知识正文。"""
    knowledge = _build_knowledge(tmp_path)
    (knowledge / "logo.png").write_bytes(b"\x89PNG")
    (knowledge / ".hidden.md").write_text("secret", encoding="utf-8")

    scanned = scan_documents(knowledge)

    assert set(scanned) == {"warranty.md", "manual/install.md"}, "meta.yaml / 二进制 / 隐藏文件都不入库"


def test_iter_documents_requires_existing_dir(tmp_path) -> None:
    """验证：知识目录不存在时报错——路径写错要立刻暴露，而不是当成「没有文档」索引成功。"""
    with pytest.raises(IndexManifestError, match="知识目录不存在"):
        iter_document_paths(tmp_path / "nope")


# --------------------------------------------------------------------------- 增量比对


def test_diff_on_empty_manifest_marks_everything_added(tmp_path) -> None:
    """验证：没有旧清单（首次构建）时全部记为 added——这是 full 模式的判定依据。"""
    knowledge = _build_knowledge(tmp_path)
    diff = diff_documents(None, scan_documents(knowledge))
    assert diff.added == ["manual/install.md", "warranty.md"]
    assert diff.modified == [] and diff.removed == []
    assert diff.is_empty is False


def test_diff_detects_added_modified_removed_and_keeps_unchanged(tmp_path) -> None:
    """验证：增量比对把新增/修改/删除/未变四类分开，且**未变的文档不重复入库**。"""
    knowledge = _build_knowledge(tmp_path)
    current = scan_documents(knowledge)

    manifest = _manifest(
        [
            DocEntry(doc_id="manual/install.md", path="manual/install.md", content_sha256="old"),
            DocEntry(
                doc_id="warranty.md",
                path="warranty.md",
                content_sha256=current["warranty.md"],
            ),
            DocEntry(doc_id="removed.md", path="removed.md", content_sha256="whatever"),
        ]
    )

    (knowledge / "new.md").write_text("新文档", encoding="utf-8")
    diff = diff_documents(manifest, scan_documents(knowledge))

    assert diff.added == ["new.md"]
    assert diff.modified == ["manual/install.md"], "哈希不同即视为修改"
    assert diff.removed == ["removed.md"], "清单有、磁盘无即视为删除"
    assert diff.unchanged == ["warranty.md"], "未变动的文档不得重复入库"
    assert diff.summary() == {"added": 1, "modified": 1, "removed": 1, "unchanged": 1}


def test_diff_is_empty_when_nothing_changed(tmp_path) -> None:
    """``--dry-run`` 连跑两次必须为 0/0/0，这是 P3 Gate 的一条判定。"""
    knowledge = _build_knowledge(tmp_path)
    current = scan_documents(knowledge)
    manifest = _manifest(
        [
            DocEntry(doc_id=doc_id, path=doc_id, content_sha256=digest)
            for doc_id, digest in current.items()
        ]
    )
    assert diff_documents(manifest, current).is_empty is True


# --------------------------------------------------------------------------- 兼容性


@pytest.mark.parametrize(
    ("overrides", "expect_fragment"),
    [
        ({"industry_id": "3c"}, "行业包不匹配"),
        ({"embed_model_id": "other-model"}, "嵌入模型不匹配"),
        ({"embed_dim": 384}, "向量维度不匹配"),
        ({"chunk_size": 800}, "分块大小不匹配"),
        ({"chunk_overlap": 0}, "分块重叠不匹配"),
        ({"chunker_version": "old-chunker"}, "分块器版本不匹配"),
        ({"schema_version": MANIFEST_SCHEMA_VERSION + 1}, "结构版本不匹配"),
    ],
)
def test_incompatible_manifest_requires_rebuild(overrides, expect_fragment) -> None:
    """「旧向量配新模型」必须被拒绝，而不是静默产出不可比的检索结果。"""
    manifest = _manifest([], **overrides)
    reasons = check_incremental_compatible(
        manifest,
        industry_id="appliance",
        embed_model_id="BAAI/bge-small-zh-v1.5",
        embed_dim=512,
        chunk_size=500,
        chunk_overlap=50,
    )
    assert reasons.requires_rebuild is True
    assert expect_fragment in reasons.describe()


def test_matching_manifest_allows_incremental() -> None:
    """验证：参数完全一致时**允许**增量——与上一条成对，避免「永远全量重建」的保守退化。"""
    reasons = check_incremental_compatible(
        _manifest([]),
        industry_id="appliance",
        embed_model_id="BAAI/bge-small-zh-v1.5",
        embed_dim=512,
        chunk_size=500,
        chunk_overlap=50,
    )
    assert reasons.requires_rebuild is False, "参数一致时应允许增量，避免每次都全量重建"


def test_missing_manifest_reports_rebuild_but_not_as_error() -> None:
    """验证：清单缺失时要求重建，但措辞是「全量构建」而非报错——首次构建是正常路径。"""
    reasons = check_incremental_compatible(
        None,
        industry_id="appliance",
        embed_model_id="m",
        embed_dim=8,
        chunk_size=100,
        chunk_overlap=10,
    )
    assert reasons.requires_rebuild is True
    assert "全量构建" in reasons.describe()


# --------------------------------------------------------------------------- 读写


def test_manifest_round_trip(tmp_path) -> None:
    """验证：清单写盘再读回字段完整，且不残留 ``.tmp`` 临时文件（写盘要原子）。"""
    index_dir = tmp_path / "indexes" / "appliance"
    manifest = _manifest(
        [DocEntry(doc_id="warranty.md", path="warranty.md", content_sha256="abc", chunk_count=3)]
    )

    path = save_manifest(index_dir, manifest)

    assert path == manifest_path(index_dir) and path.exists()
    assert not path.with_suffix(".json.tmp").exists(), "不得残留临时文件"
    loaded = load_manifest(index_dir)
    assert loaded is not None
    assert loaded.industry_id == "appliance"
    assert loaded.docs[0].chunk_count == 3
    assert loaded.embed_dim == 512


def test_load_missing_manifest_returns_none(tmp_path) -> None:
    """验证：清单不存在返回 ``None``——「从未构建」交由调用方决定走全量，不是错误。"""
    assert load_manifest(tmp_path / "indexes" / "appliance") is None


def test_broken_manifest_fails_loudly(tmp_path) -> None:
    """验证：清单坏成非 JSON、或结构缺字段时都报错——坏清单若被当成「没有清单」会触发错误重建。"""
    index_dir = tmp_path / "indexes" / "appliance"
    index_dir.mkdir(parents=True)
    manifest_path(index_dir).write_text("{ not json", encoding="utf-8")
    with pytest.raises(IndexManifestError, match="不是合法 JSON"):
        load_manifest(index_dir)

    manifest_path(index_dir).write_text(json.dumps({"industry_id": "appliance"}), encoding="utf-8")
    with pytest.raises(IndexManifestError, match="结构非法"):
        load_manifest(index_dir)


def test_with_docs_refreshes_timestamp_without_mutating_original() -> None:
    """验证：``with_docs`` 返回新对象、不改原对象，并刷新时间戳与分块器版本。"""
    manifest = _manifest([])
    updated = manifest.with_docs([DocEntry(doc_id="a.md", path="a.md", content_sha256="x")])

    assert manifest.docs == [], "原对象不得被修改"
    assert [entry.doc_id for entry in updated.docs] == ["a.md"]
    assert updated.updated_at >= manifest.updated_at
    assert updated.chunker_version == CHUNKER_VERSION
    assert updated.doc_ids() == ["a.md"]
