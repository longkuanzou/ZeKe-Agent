"""索引清单（manifest）与增量比对——决定「哪些文档要新增 / 重建 / 删除」。

本模块**框架无关**：不 import ``llama_index`` / ``langchain``，因此可以被 Core、
组合根与测试直接使用；真正的解析 / 分块 / 向量化在接缝
``core/rag/llamaindex_engine.py`` 内完成。

设计要点（对应 DESIGN §6.3.1 / §6.3.3）：

1. **稳定标识**：``doc_id`` = 相对 ``knowledge/`` 的 POSIX 路径；``node_id`` 由引擎拼为
   ``f"{doc_id}:{chunk_idx}"``，可复现、可定位、可按前缀删除。
2. **内容哈希**：以文本 ``sha256`` 判定「改没改」，不依赖 mtime（复制 / 重排都会变）。
3. **拒绝错误加载优先于静默降级**：行业包 id、嵌入模型、维度、分块参数任一不匹配，
   一律**拒绝增量更新**并要求显式 ``--rebuild``，避免「拿旧向量配新模型」产出不可比的结果。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from zkagent.core.rag.errors import RagError

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "CHUNKER_VERSION",
    "DOC_SUFFIXES",
    "IndexManifestError",
    "DocEntry",
    "IndexManifest",
    "DocumentDiff",
    "RebuildReasons",
    "manifest_path",
    "load_manifest",
    "save_manifest",
    "doc_id_for",
    "sha256_text",
    "iter_document_paths",
    "scan_documents",
    "diff_documents",
    "check_incremental_compatible",
    "utc_now",
]

#: manifest 结构版本；未来结构变化时用它做迁移判定。
MANIFEST_SCHEMA_VERSION = 1

#: 分块器实现版本；分块逻辑一改就必须递增，否则旧索引的分块无法与新代码对应。
CHUNKER_VERSION = "recursive-char-v1"

#: 参与索引的文档后缀（只认纯文本类，二进制留给后续阶段）。
DOC_SUFFIXES: tuple[str, ...] = (".md", ".txt")


class IndexManifestError(RagError):
    """manifest 缺失 / 结构非法 / 与当前配置不兼容——**必须显式失败**。"""


def utc_now() -> str:
    """当前 UTC 时间（ISO 8601，秒级）。"""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class DocEntry(BaseModel):
    """单篇文档在清单中的记录。"""

    doc_id: str = Field(description="相对 knowledge/ 的 POSIX 路径，稳定不变")
    path: str = Field(description="入库时的原始相对路径（用于排查）")
    content_sha256: str = Field(description="文本内容哈希，用于判定「改没改」")
    chunk_count: int = Field(default=0, ge=0)


class IndexManifest(BaseModel):
    """``data/indexes/<industry_id>/manifest.json`` 的强类型表示。"""

    schema_version: int = MANIFEST_SCHEMA_VERSION
    industry_id: str
    embed_model_id: str
    embed_dim: int = Field(ge=1)
    chunk_size: int = Field(gt=0)
    chunk_overlap: int = Field(ge=0)
    chunker_version: str = CHUNKER_VERSION
    reranker_model_id: str | None = None
    docs: list[DocEntry] = Field(default_factory=list)
    built_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    def doc_ids(self) -> list[str]:
        """清单中全部 ``doc_id``（排序后）。"""
        return sorted(entry.doc_id for entry in self.docs)

    def with_docs(self, docs: list[DocEntry]) -> "IndexManifest":
        """返回替换 ``docs`` 并刷新 ``updated_at`` 的副本（不改原对象）。"""
        return self.model_copy(update={"docs": docs, "updated_at": utc_now()})


class DocumentDiff(BaseModel):
    """``knowledge/`` 现状与清单的差异——增量更新的输入。"""

    added: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """没有任何变动（``--dry-run`` 连跑两次应为此状态）。"""
        return not (self.added or self.modified or self.removed)

    def summary(self) -> dict[str, int]:
        """计数摘要（供 CLI 打印，避免用户猜）。"""
        return {
            "added": len(self.added),
            "modified": len(self.modified),
            "removed": len(self.removed),
            "unchanged": len(self.unchanged),
        }


class RebuildReasons(BaseModel):
    """拒绝增量更新的原因清单；为空表示可以安全增量。"""

    reasons: list[str] = Field(default_factory=list)

    @property
    def requires_rebuild(self) -> bool:
        """是否存在必须全量重建的原因。"""
        return bool(self.reasons)

    def describe(self) -> str:
        """人类可读描述（CLI 报错与 `--dry-run` 提示共用）。"""
        return "；".join(self.reasons)


# --------------------------------------------------------------------------- 路径与哈希


def manifest_path(index_dir: Path | str) -> Path:
    """索引目录内的 manifest 路径。"""
    return Path(index_dir) / "manifest.json"


def doc_id_for(knowledge_dir: Path | str, path: Path | str) -> str:
    """把文档路径转为稳定 ``doc_id``（相对 ``knowledge/`` 的 POSIX 路径）。

    之所以不用绝对路径：目录搬迁后 ``doc_id`` 不应变化，否则增量比对会全量重建。
    """
    base = Path(knowledge_dir).resolve()
    target = Path(path).resolve()
    try:
        return target.relative_to(base).as_posix()
    except ValueError as exc:
        raise IndexManifestError(f"文档不在知识目录内：{target}（应位于 {base}）") from exc


def sha256_text(text: str) -> str:
    """文本内容哈希（UTF-8 归一化，避免平台换行差异）。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def iter_document_paths(
    knowledge_dir: Path | str, *, suffixes: Iterable[str] = DOC_SUFFIXES
) -> list[Path]:
    """列出知识目录下参与索引的文档（跳过隐藏文件与元数据文件）。"""
    base = Path(knowledge_dir)
    if not base.exists():
        raise IndexManifestError(f"知识目录不存在：{base}")
    allowed = tuple(suffix.lower() for suffix in suffixes)
    found: list[Path] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.name in {"meta.yaml", "meta.yml"}:
            # 元数据不是知识正文，不参与检索（否则会污染召回）
            continue
        if path.suffix.lower() in allowed:
            found.append(path)
    return found


def scan_documents(knowledge_dir: Path | str) -> dict[str, str]:
    """扫描知识目录，返回 ``doc_id -> content_sha256``（当前磁盘状态）。"""
    base = Path(knowledge_dir)
    return {
        doc_id_for(base, path): sha256_text(path.read_text(encoding="utf-8"))
        for path in iter_document_paths(base)
    }


# --------------------------------------------------------------------------- 读写


def load_manifest(index_dir: Path | str) -> IndexManifest | None:
    """读取清单；文件不存在返回 ``None``（由调用方决定全量重建），结构非法则报错。"""
    path = manifest_path(index_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IndexManifestError(f"manifest 不是合法 JSON：{path}：{exc}") from exc
    try:
        return IndexManifest.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError：结构损坏必须显式失败
        raise IndexManifestError(f"manifest 结构非法：{path}：{exc}") from exc


def save_manifest(index_dir: Path | str, manifest: IndexManifest) -> Path:
    """原子写入清单（先写临时文件再替换，避免中断留下半个 JSON）。"""
    directory = Path(index_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = manifest_path(directory)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)
    return path


# --------------------------------------------------------------------------- 比对与兼容性


def diff_documents(
    manifest: IndexManifest | None, current: Mapping[str, str]
) -> DocumentDiff:
    """对比清单与磁盘现状。

    - ``added``：磁盘有、清单无 → 新增；
    - ``modified``：两边都有但哈希不同 → 先删该 ``doc_id`` 的全部 chunk，再插入新 chunk；
    - ``removed``：清单有、磁盘无 → 按 ``doc_id`` 删除并更新清单。
    """
    if manifest is None:
        return DocumentDiff(added=sorted(current), unchanged=[])

    previous = {entry.doc_id: entry.content_sha256 for entry in manifest.docs}
    added, modified, unchanged = [], [], []
    for doc_id in sorted(current):
        if doc_id not in previous:
            added.append(doc_id)
        elif previous[doc_id] != current[doc_id]:
            modified.append(doc_id)
        else:
            unchanged.append(doc_id)
    removed = sorted(doc_id for doc_id in previous if doc_id not in current)
    return DocumentDiff(added=added, modified=modified, removed=removed, unchanged=unchanged)


def check_incremental_compatible(
    manifest: IndexManifest | None,
    *,
    industry_id: str,
    embed_model_id: str,
    embed_dim: int,
    chunk_size: int,
    chunk_overlap: int,
    chunker_version: str = CHUNKER_VERSION,
) -> RebuildReasons:
    """判断能否在既有索引上做增量；返回**必须重建的原因**（空表示可以增量）。

    宁可要求重建，也不允许「旧向量 + 新模型」混在同一个索引里——那会让检索指标不可比，
    且错误极难察觉（召回率下降但不报错）。
    """
    if manifest is None:
        return RebuildReasons(reasons=["索引不存在，将执行全量构建"])

    reasons: list[str] = []
    if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
        reasons.append(
            f"manifest 结构版本不匹配（索引 {manifest.schema_version} ≠ 代码 {MANIFEST_SCHEMA_VERSION}）"
        )
    if manifest.industry_id != industry_id:
        reasons.append(f"行业包不匹配（索引 {manifest.industry_id!r} ≠ 当前 {industry_id!r}）")
    if manifest.embed_model_id != embed_model_id:
        reasons.append(
            f"嵌入模型不匹配（索引 {manifest.embed_model_id!r} ≠ 当前 {embed_model_id!r}）"
        )
    if manifest.embed_dim != embed_dim:
        reasons.append(f"向量维度不匹配（索引 {manifest.embed_dim} ≠ 当前 {embed_dim}）")
    if manifest.chunk_size != chunk_size:
        reasons.append(f"分块大小不匹配（索引 {manifest.chunk_size} ≠ 当前 {chunk_size}）")
    if manifest.chunk_overlap != chunk_overlap:
        reasons.append(
            f"分块重叠不匹配（索引 {manifest.chunk_overlap} ≠ 当前 {chunk_overlap}）"
        )
    if manifest.chunker_version != chunker_version:
        reasons.append(
            f"分块器版本不匹配（索引 {manifest.chunker_version!r} ≠ 当前 {chunker_version!r}）"
        )
    return RebuildReasons(reasons=reasons)
