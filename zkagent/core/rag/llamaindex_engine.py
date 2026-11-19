"""接缝二：**唯一**允许 import ``llama_index`` 的文件。

职责（DESIGN §6.3）：

- **解析与分块**：``SentenceSplitter``（实例参数，不碰全局 ``Settings``）；
- **向量存储与稠密检索**：``SimpleVectorStore``（节点自带 embedding，持久化到磁盘）；
- **增量更新**：按 ``doc_id``（= 节点的 ``ref_doc_id``）整篇删除后重插；
- **混合检索**：稠密（本文件） + 稀疏（``sparse.py`` 的 BM25）经 RRF 融合；
- 对外**只暴露自研 ``Chunk``**，``llama_index`` 的 ``Node`` / ``Document`` 不越出本文件。

三处刻意的设计取舍（与 DESIGN §3.3 的初步设想不同，理由如下）：

1. **不使用全局 ``Settings``，也不用 ``BaseEmbedding`` 桥接**：分块时直接用自研
   ``Embedder`` 算出向量写进节点，查询时用同一个 ``Embedder`` 算查询向量后传给
   ``VectorStore.query``。这样嵌入实现（含确定性 hash 降级）完全由我们掌控，
   也不会因为某个测试改了全局 ``Settings`` 而互相污染。
2. **不使用 ``VectorStoreIndex`` + ``QueryFusionRetriever``**：``VectorStoreIndex`` 会引入
   一层用不到的 docstore（文本的事实来源已是 ``chunks.json``），而 ``QueryFusionRetriever``
   无法在融合前统一做权限过滤。BM25 与 RRF 放在框架外的 ``sparse.py``，可独立持久化与测试。
3. **节点 id 由我们指定**（``f"{doc_id}:{chunk_idx}"``）：必须**可复现**，否则检索评测的
   gold 标注（``evals/retrieval.yaml``）与增量删除都无法定位。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import Document, TextNode
from llama_index.core.vector_stores import SimpleVectorStore
from llama_index.core.vector_stores.types import VectorStoreQuery

from zkagent.core.interfaces import Embedder, Retriever
from zkagent.core.rag.embedder import (
    FALLBACK_MODE,
    LOCAL_DENSE_MIN_SIMILARITY,
    LOCAL_MODE,
    default_dense_min_similarity,
)
from zkagent.core.rag.errors import RagIndexError, RetrievalError
from zkagent.core.rag.index_lock import index_lock
from zkagent.core.rag.gate import DEFAULT_MIN_COVERAGE, text_coverage
from zkagent.core.rag.manifest import (
    CHUNKER_VERSION,
    DocEntry,
    IndexManifest,
    check_incremental_compatible,
    diff_documents,
    load_manifest,
    save_manifest,
    scan_documents,
    utc_now,
)
from zkagent.core.rag.metadata import (
    DocMeta,
    exclusion_reason,
    load_knowledge_meta,
    parse_front_matter,
)
from zkagent.core.rag.sparse import (
    ChunkRecord,
    SparseIndex,
    load_chunks,
    reciprocal_rank_fusion,
    save_chunks,
)
from zkagent.core.types import Chunk

__all__ = ["LlamaIndexRetriever", "IndexBuildReport", "VECTOR_STORE_FILENAME"]

#: 向量存储持久化文件名（与 manifest.json / chunks.json 同目录）。
VECTOR_STORE_FILENAME = "vector_store.json"

#: 召回候选倍数：融合前每路多取一些，避免某一路独占候选集。
_CANDIDATE_FACTOR = 3

#: 支持的检索模式（供指标对照：稠密 / 稀疏 / 融合 / 融合+精排）。
_SUPPORTED_MODES = frozenset({"dense", "sparse", "hybrid", "hybrid+rerank"})

#: 默认分块参数（DESIGN 未固定，取中文客服文档常用值）。
DEFAULT_CHUNK_SIZE = 400
DEFAULT_CHUNK_OVERLAP = 50


def _doc_of_node(node_id: str) -> str:
    """``node_id``（``"{doc_id}:{chunk_idx}"``）→ ``doc_id``。

    与 ``metrics.document_ranking`` 用同一套拆分口径，保证「追踪里看到的文档」
    与「评测里算指标的文档」是同一个东西。
    """
    return node_id.split(":", 1)[0]

#: 稠密相似度下限（**相关性闸门**），仅对真实嵌入模型成立。
#:
#: 实测（appliance 语料 + bge-small-zh-v1.5，余弦相似度）：相关查询 top-1 落在
#: 0.70~0.80，不相关查询整体不超过 0.28，取 0.45 能干净分开。没有这道闸门时
#: 「永远返回 top-k」，模型总能拿到「依据」→ 会编造答案。降级模式下的取值见
#: ``embedder.FALLBACK_DENSE_MIN_SIMILARITY``。
DEFAULT_DENSE_MIN_SIMILARITY = LOCAL_DENSE_MIN_SIMILARITY


@dataclass(frozen=True)
class IndexBuildReport:
    """一次索引操作的结果（CLI 直接打印它，避免用户猜）。"""

    industry_id: str
    index_dir: str
    #: ``full`` / ``incremental`` / ``dry-run``
    mode: str
    embedding_mode: str
    embed_model_id: str
    embed_dim: int
    chunk_size: int
    chunk_overlap: int
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    chunk_total: int = 0
    rebuild_reasons: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, object]:
        """转为可 JSON 序列化的字典。"""
        return {
            "industry_id": self.industry_id,
            "index_dir": self.index_dir,
            "mode": self.mode,
            "embedding_mode": self.embedding_mode,
            "embed_model_id": self.embed_model_id,
            "embed_dim": self.embed_dim,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "added": self.added,
            "modified": self.modified,
            "removed": self.removed,
            "unchanged": self.unchanged,
            "chunk_total": self.chunk_total,
            "rebuild_reasons": self.rebuild_reasons,
            "elapsed_ms": self.elapsed_ms,
        }


class LlamaIndexRetriever(Retriever):
    """知识侧检索实现（唯一 import ``llama_index`` 的地方）。

    所有配置**由实例持有**（不用全局 ``Settings``）：``embedder`` / ``chunk_size`` /
    ``chunk_overlap`` / ``index_dir``。因此同一进程里可以并存多个行业包的实例而互不干扰。
    """

    def __init__(
        self,
        *,
        industry_id: str,
        knowledge_dir: Path | str,
        index_dir: Path | str,
        embedder: Embedder,
        embedding_mode: str = LOCAL_MODE,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        top_k: int = 5,
        dense_min_similarity: float | None = None,
        min_coverage: float | None = None,
        sparse_min_score: float = 0.0,
        mode: str = "hybrid",
        reranker: Any | None = None,
    ) -> None:
        if chunk_overlap >= chunk_size:
            raise RagIndexError(
                f"分块重叠必须小于分块大小（overlap={chunk_overlap}, size={chunk_size}）"
            )
        self.industry_id = industry_id
        self.knowledge_dir = Path(knowledge_dir)
        self.index_dir = Path(index_dir)
        self.top_k = top_k
        self._embedder = embedder
        self._embedding_mode = embedding_mode
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        # 闸门必须按嵌入模式标定：降级 hash 向量的相似度不是相关性信号（见 embedder.py 实测注释）
        self._dense_min_similarity = (
            dense_min_similarity
            if dense_min_similarity is not None
            else default_dense_min_similarity(embedding_mode)
        )
        # hash 向量的余弦相似度不可作相关性判据：降级模式改用关键词覆盖率（S1）
        self._min_coverage = (
            min_coverage
            if min_coverage is not None
            else (DEFAULT_MIN_COVERAGE if embedding_mode == FALLBACK_MODE else 0.0)
        )
        self._sparse_min_score = sparse_min_score
        self._mode = mode
        self._reranker = reranker
        #: 最近一次精排结果（用于上报是否降级）
        self.last_rerank: Any | None = None

        self._manifest: IndexManifest | None = None
        self._chunks: list[ChunkRecord] = []
        self._store: SimpleVectorStore | None = None
        self._meta: dict[str, DocMeta] = {}
        #: 型号 → 品牌（来自身份卡）：品牌由服务端按已知型号解析，不采信模型自报
        self._product_brand: dict[str, str] = {}

    # ------------------------------------------------------------------ 属性

    @property
    def embedding_mode(self) -> str:
        """``local_model`` / ``fallback_hash``——必须出现在任何指标报告里。"""
        return self._embedding_mode

    @property
    def embed_model_id(self) -> str:
        """当前嵌入实现标识（写入 manifest，用于判定是否需要重建）。"""
        return self._embedder.model_id

    @property
    def embed_dim(self) -> int:
        """当前向量维度。"""
        return self._embedder.dim

    @property
    def manifest(self) -> IndexManifest | None:
        """已加载的索引清单（未加载时为 ``None``）。"""
        return self._manifest

    # ------------------------------------------------------------------ 索引

    def index(self, *, rebuild: bool = False, dry_run: bool = False) -> IndexBuildReport:
        """构建 / 增量更新索引；写操作持有目录锁（S4），``--dry-run`` 不写盘也不抢锁。"""
        if dry_run:
            return self._index_unlocked(rebuild=rebuild, dry_run=True)
        with index_lock(self.index_dir):
            return self._index_unlocked(rebuild=rebuild, dry_run=False)

    def _index_unlocked(
        self, *, rebuild: bool = False, dry_run: bool = False
    ) -> IndexBuildReport:
        """构建或增量更新索引。

        :param rebuild: 强制全量重建（删掉既有索引重新嵌入）。
        :param dry_run: 只报告「将新增 / 修改 / 删除」的计数，**不写任何文件**。

        与 DESIGN §6.3.3 一致：参数不匹配（行业包 / 嵌入模型 / 维度 / 分块参数 / 分块器版本）
        时**拒绝增量**并要求 ``--rebuild``，宁可不做也不产出不可比的索引。
        """
        started = time.perf_counter()

        current = scan_documents(self.knowledge_dir) # 只保留 *txt、*md、*markdown 文件（去除meta.md)
        if not current:
            raise RagIndexError(f"知识包 {self.knowledge_dir} 为空")
        doc_metas = load_knowledge_meta(self.knowledge_dir, known_doc_ids=sorted(current))
        existing = None if rebuild else load_manifest(self.index_dir)

        reasons = check_incremental_compatible(
            existing,
            industry_id=self.industry_id,
            embed_model_id=self.embed_model_id,
            embed_dim=self.embed_dim,
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
        ) if existing is not None else None

        if reasons is not None and reasons.requires_rebuild:
            # 参数已变：不能拿旧索引做增量（会让指标不可比且极难察觉）
            existing = None

        diff = diff_documents(existing, current)
        base_mode = "full" if existing is None else "incremental"

        if dry_run:
            return self._report(
                mode="dry-run",
                diff=diff,
                chunk_total=self._chunk_count_for(existing, diff),
                rebuild_reasons=[reason for reason in (reasons.reasons if reasons else [])],
                started=started,
            )

        self._meta = doc_metas
        self._rebuild_product_brand()
        if existing is None:
            records, store = self._build_full(current, doc_metas)
        else:
            records, store = self._apply_incremental(existing, current, doc_metas, diff)

        self.index_dir.mkdir(parents=True, exist_ok=True)
        store.persist(str(self.index_dir / VECTOR_STORE_FILENAME))
        save_chunks(self.index_dir, records)

        manifest = IndexManifest(
            industry_id=self.industry_id,
            embed_model_id=self.embed_model_id,
            embed_dim=self.embed_dim,
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
            chunker_version=CHUNKER_VERSION,
            docs=self._entries_for(records),
            built_at=existing.built_at if existing is not None else utc_now(),
        )
        save_manifest(self.index_dir, manifest)
        self._manifest = manifest
        self._chunks = records
        self._store = store

        return self._report(
            mode=base_mode,
            diff=diff,
            chunk_total=len(records),
            rebuild_reasons=[reason for reason in (reasons.reasons if reasons else [])],
            started=started,
        )

    def load(self) -> None:
        """从磁盘加载索引（供检索使用）；索引缺失或结构损坏则**显式失败**。"""
        manifest = load_manifest(self.index_dir)
        if manifest is None:
            raise RagIndexError(
                f"索引不存在：{self.index_dir}（请先执行 `zkagent rag index --industry {self.industry_id}`）"
            )
        reasons = check_incremental_compatible(
            manifest,
            industry_id=self.industry_id,
            embed_model_id=self.embed_model_id,
            embed_dim=self.embed_dim,
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
        )
        if reasons.requires_rebuild:
            raise RagIndexError(f"索引与当前配置不兼容，需要 --rebuild：{reasons.describe()}")

        store_path = self.index_dir / VECTOR_STORE_FILENAME
        if not store_path.exists():
            raise RagIndexError(f"索引缺少向量存储文件：{store_path}（请执行 --rebuild）")

        self._manifest = manifest
        self._chunks = load_chunks(self.index_dir)
        self._store = SimpleVectorStore.from_persist_path(str(store_path))
        # 以**清单**里的 doc_id 为准补全元数据：新增文档即便没在 meta.yaml 登记，
        # 也必须能按目录推导出 journey 并可被检索（未登记 ≠ 不可见）。
        self._meta = load_knowledge_meta(self.knowledge_dir, known_doc_ids=manifest.doc_ids())
        self._rebuild_product_brand()

    @property
    def is_loaded(self) -> bool:
        """索引是否已加载到内存。"""
        return self._store is not None

    # ------------------------------------------------------------------ 检索

    def search(
        self,
        query: str,
        top_k: int = 5,
        filters: dict | None = None,
        mode: str | None = None,
        trace: dict | None = None,
    ) -> list[Chunk]:
        """混合检索：稠密 + BM25 经 RRF 融合，再按可见性过滤后返回。

        :param filters: ``{"acl_tags": [...], "doc_types": [...], "allowed_roles": [...],
            "journeys": [...], "appliances": [...], "product_ids": [...]}``。
            ``acl_tags`` 表示**服务端已确认**的调用方标签——本层不做信任判断，
            由 ``knowledge_tool.py`` 负责不采信模型自报的标签；其余键都是**收窄**，
            可安全地由调用方指定。
        :param trace: 传入空字典时，把**每个阶段的中间结果**写进去（召回 / 融合 / 闸门 /
            过滤 / 精排 / 最终结果）。默认 ``None`` 时不产生任何额外开销。
            这是数据流面板的数据来源：界面展示的必须是**真实中间值**，不能事后推测。

        过滤发生在**融合之后、返回之前**，保证任何一路召回都不会把越权文档带出去。
        """
        if not query.strip():
            raise RetrievalError("检索查询不能为空")
        if not self.is_loaded:
            self.load()
        assert self._store is not None  # load() 保证

        limit = top_k or self.top_k
        candidates = self._candidate_window(limit, filters)
        resolved_mode = (mode or self._mode).strip().lower()
        if resolved_mode not in _SUPPORTED_MODES:
            raise RetrievalError(
                f"未知的检索模式：{resolved_mode!r}（可选 {'、'.join(sorted(_SUPPORTED_MODES))}）"
            )

        if trace is not None:
            trace.clear()
            trace.update(
                {
                    "query": query,
                    "mode": resolved_mode,
                    "top_k": limit,
                    "candidates": candidates,
                    "dense": [],
                    "sparse": [],
                    "gate": {},
                    "fused": [],
                    "filter": {},
                    "rerank": None,
                    "final": [],
                    "timings_ms": {},
                }
            )

        def _hit(node_id: str, score: float) -> dict[str, object]:
            """把 ``(node_id, score)`` 转成 trace 用的字典。

            :param node_id: 分块标识（``doc_id:序号``）。
            :param score: 该阶段的原始分数（RRF 之前，各阶段量纲不同）。
            :returns: 含 ``node_id`` / ``doc_id`` / ``score`` 的字典，供面板展示。
            """
            return {"node_id": node_id, "doc_id": _doc_of_node(node_id), "score": round(score, 6)}

        # 纯稀疏模式：只按关键词召回，不设相关性闸门（调用方明确要「精确关键词」语义）
        started = time.perf_counter()
        dense_hits = [] if resolved_mode == "sparse" else self._dense_hits(query, candidates)
        if trace is not None:
            trace["timings_ms"]["dense"] = round((time.perf_counter() - started) * 1000, 1)
            trace["dense"] = [_hit(node_id, score) for node_id, score in dense_hits]

        started = time.perf_counter()
        sparse_hits = [] if resolved_mode == "dense" else self._sparse_hits(query, candidates)
        if trace is not None:
            trace["timings_ms"]["sparse"] = round((time.perf_counter() - started) * 1000, 1)
            trace["sparse"] = [_hit(node_id, score) for node_id, score in sparse_hits]

        # ---- 相关性闸门（决定「有没有依据」）----
        # 稠密相似度是主判据：低于下限说明语料与该查询基本无关。
        # 中文 BM25 用字 n-gram，偶发的单字重合会造成假阳性（实测：「红冲」命中「红楼梦」、
        # 不相关查询仍拿到 BM25 4.78 分），因此**稠密通路为空时不采信纯稀疏结果**——
        # 否则「无依据」会被伪装成「有依据」，模型随之编造答案。
        # 若确实需要纯关键词检索（如按型号/编号精确查），显式设置 ``sparse_min_score`` 才放行。
        gate_passed = not (
            resolved_mode != "sparse"
            and not dense_hits
            and (self._sparse_min_score <= 0 or not sparse_hits)
        )
        if trace is not None:
            trace["gate"] = {
                "passed": gate_passed,
                "dense_min_similarity": self._dense_min_similarity,
                "sparse_min_score": self._sparse_min_score,
                "reason": "ok"
                if gate_passed
                else "稠密候选全部低于相关性下限，且稀疏结果不足以单独作依据",
            }
        if not gate_passed:
            return []

        dense_ids = [node_id for node_id, _score in dense_hits]
        sparse_ids = [node_id for node_id, _score in sparse_hits]

        started = time.perf_counter()
        fused = reciprocal_rank_fusion([dense_ids, sparse_ids])
        if trace is not None:
            trace["timings_ms"]["fuse"] = round((time.perf_counter() - started) * 1000, 1)
        if not fused:
            if trace is not None:
                trace["gate"] = {"passed": False, "reason": "两路召回均无候选", **{
                    "dense_min_similarity": self._dense_min_similarity,
                    "sparse_min_score": self._sparse_min_score,
                }}
            return []

        dense_rank = {node_id: rank for rank, node_id in enumerate(dense_ids, start=1)}
        sparse_rank = {node_id: rank for rank, node_id in enumerate(sparse_ids, start=1)}
        dense_score = dict(dense_hits)
        sparse_score = dict(sparse_hits)
        by_id = {record.node_id: record for record in self._chunks}

        if trace is not None:
            trace["fused"] = [
                {
                    "node_id": node_id,
                    "doc_id": _doc_of_node(node_id),
                    "rrf": round(score, 6),
                    "dense_rank": dense_rank.get(node_id),
                    "sparse_rank": sparse_rank.get(node_id),
                    "dense_score": (
                        round(dense_score.get(node_id, 0.0), 6)
                        if node_id in dense_score
                        else None
                    ),
                    "sparse_score": (
                        round(sparse_score.get(node_id, 0.0), 6)
                        if node_id in sparse_score
                        else None
                    ),
                }
                for node_id, score in fused
            ]

        # ---- 降级模式：hash 相似度不可信，改用查询关键词覆盖率（S1）----
        coverage_dropped = 0
        if self._embedding_mode == FALLBACK_MODE and self._min_coverage > 0:
            coverage_rows = []
            kept: list[tuple[str, float]] = []
            for node_id, score in fused:
                record = by_id.get(node_id)
                covered = text_coverage(query, record.text if record is not None else "")
                coverage_rows.append(
                    {"node_id": node_id, "coverage": round(covered, 4)}
                )
                if covered >= self._min_coverage:
                    kept.append((node_id, score))
            coverage_dropped = len(fused) - len(kept)
            fused = kept
            if trace is not None:
                trace["gate"].update(
                    {
                        "coverage_min": self._min_coverage,
                        "coverage_dropped": coverage_dropped,
                        "coverage": coverage_rows,
                    }
                )
            if not fused:
                if trace is not None:
                    trace["gate"].update(
                        {
                            "passed": False,
                            "reason": "降级模式下候选关键词覆盖率不足（无法确认相关性）",
                        }
                    )
                return []

        # 过滤解释只针对「本轮真的被召回过的文档」，避免把整库 1600 篇都列出来
        focus = {_doc_of_node(node_id) for node_id, _score in fused}
        visible = self._visible_doc_ids(filters, trace=trace, focus=focus)

        # score 的语义随模式变化，必须随结果一起上报（M4）
        score_kind = (
            "bm25" if resolved_mode == "sparse"
            else "cosine" if resolved_mode == "dense"
            else "rrf"
        )

        results: list[Chunk] = []
        dropped_by_filter = 0
        for node_id, score in fused:
            record = by_id.get(node_id)
            if record is None:
                continue
            if record.doc_id not in visible:
                dropped_by_filter += 1
                continue
            results.append(
                Chunk(
                    text=record.text,
                    source=record.doc_id,
                    node_id=record.node_id,
                    score=score,
                    metadata={
                        "doc_id": record.doc_id,
                        "doc_type": record.doc_type,
                        "journey": record.journey,
                        "chunk_idx": record.chunk_idx,
                        "dense_rank": dense_rank.get(node_id),
                        "sparse_rank": sparse_rank.get(node_id),
                        "dense_score": dense_score.get(node_id),
                        "sparse_score": sparse_score.get(node_id),
                        "score_kind": score_kind,
                    },
                )
            )
            if len(results) >= limit:
                break

        if trace is not None:
            trace["filter"]["dropped_candidates"] = dropped_by_filter
            trace["final"] = [
                {"node_id": chunk.node_id, "doc_id": chunk.source, "score": round(chunk.score, 6)}
                for chunk in results
            ]

        if resolved_mode == "hybrid+rerank" and self._reranker is not None:
            # 精排：cross-encoder 对「查询 + 候选」成对打分后重排（降级时原样返回并留下原因）
            started = time.perf_counter()
            self.last_rerank = self._reranker.rerank(query, results, top_k=limit)
            if trace is not None:
                trace["timings_ms"]["rerank"] = round((time.perf_counter() - started) * 1000, 1)
                trace["rerank"] = {
                    "degraded": bool(self.last_rerank.degraded),
                    "detail": getattr(self.last_rerank, "detail", None),
                    "before": [
                        {
                            "node_id": chunk.node_id,
                            "doc_id": chunk.source,
                            "rrf": round(chunk.score, 6),
                        }
                        for chunk in results
                    ],
                    "after": [
                        {
                            "node_id": chunk.node_id,
                            "doc_id": chunk.source,
                            "score": round(chunk.score, 6),
                            "metadata": {
                                key: value
                                for key, value in chunk.metadata.items()
                                if key in {"rerank_score", "dense_rank", "sparse_rank"}
                            },
                        }
                        for chunk in self.last_rerank.chunks
                    ],
                }
                trace["final"] = [
                    {
                        "node_id": chunk.node_id,
                        "doc_id": chunk.source,
                        "score": round(chunk.score, 6),
                    }
                    for chunk in self.last_rerank.chunks
                ]
            kind = "rrf" if self.last_rerank.degraded else "cross_encoder"
            for chunk in self.last_rerank.chunks:
                chunk.metadata["score_kind"] = kind
            return self.last_rerank.chunks

        self.last_rerank = None
        return results

    def stats(self) -> dict[str, object]:
        """索引概览（CLI `rag info` 与排障用）。

        ``journeys`` 是**按用户链路的文档数分布**，用于一眼看出「哪条链路是空的」——
        这是按链路组织知识最直接的收益。
        """
        manifest = self._manifest
        journey_counts: dict[str, int] = {}
        appliance_counts: dict[str, int] = {}
        doc_type_counts: dict[str, int] = {}
        for meta in self._meta.values():
            key = meta.journey or "unclassified"
            journey_counts[key] = journey_counts.get(key, 0) + 1
            appliance = meta.appliance or "unspecified"
            appliance_counts[appliance] = appliance_counts.get(appliance, 0) + 1
            doc_type_counts[meta.doc_type] = doc_type_counts.get(meta.doc_type, 0) + 1
        return {
            "industry_id": self.industry_id,
            "index_dir": str(self.index_dir),
            "embedding_mode": self._embedding_mode,
            "embed_model_id": self.embed_model_id,
            "embed_dim": self.embed_dim,
            "chunk_size": self._chunk_size,
            "chunk_overlap": self._chunk_overlap,
            "docs": len(manifest.docs) if manifest else 0,
            "chunks": len(self._chunks),
            "journeys": dict(sorted(journey_counts.items())),
            "appliances": dict(sorted(appliance_counts.items())),
            "doc_types": dict(sorted(doc_type_counts.items())),
            "built_at": manifest.built_at if manifest else None,
        }

    # ------------------------------------------------------------------ 内部实现

    def _candidate_window(self, limit: int, filters: dict | None) -> int:
        """候选窗口：带收窄条件时按「可见文档占比」放大，保证过滤后仍能凑满 ``limit`` 条。

        过滤发生在融合之后、截断之前（见 :meth:`search`），被过滤掉的候选**不会补位**。
        固定窗口（``limit * 3``）在型号过滤下只剩十几篇可见时，经常返回不足 k 条，
        看上去像「语料里只有这几篇相关」。窗口上限取分块总数——语料本来就是千级分块，
        放大不会失控。
        """
        base = max(limit * _CANDIDATE_FACTOR, limit)
        options = filters or {}
        narrowing = any(
            options.get(key)
            for key in ("acl_tags", "doc_types", "journeys", "appliances", "product_ids",
                        "brands", "allowed_roles")
        )
        if not narrowing or not self._meta:
            return base
        ratio = len(self._visible_doc_ids(filters)) / len(self._meta)
        if ratio <= 0:
            return base
        # 目标：过滤后仍能凑满 limit 条 → 候选 ≈ limit / 可见占比，再留 1.5 倍余量
        needed = int(limit / ratio * 1.5)
        return min(len(self._chunks) or base, max(base, needed))

    def _dense_hits(self, query: str, candidates: int) -> list[tuple[str, float]]:
        """稠密检索：自研嵌入算查询向量，交给向量存储做相似度检索。

        返回 ``[(node_id, similarity)]``，**已按相关性下限过滤**——低于下限的候选
        不是「差一点的答案」，而是「语料里根本没有」。
        """
        assert self._store is not None
        query_vector = self._embedder.embed([query])[0]
        result = self._store.query(
            VectorStoreQuery(query_embedding=query_vector, similarity_top_k=candidates)
        )
        ids = list(result.ids or [])
        similarities = [float(score) for score in (result.similarities or [])]
        return [
            (node_id, similarity)
            for node_id, similarity in zip(ids, similarities, strict=False)
            if similarity >= self._dense_min_similarity
        ]

    def _sparse_hits(self, query: str, candidates: int) -> list[tuple[str, float]]:
        """稀疏检索（BM25）；``sparse_min_score > 0`` 时才启用绝对下限。

        默认关闭绝对下限：BM25 分数随语料规模与文档长度漂移，写死阈值换一个行业包就会失效；
        因此只要求「有词面重合」（``sparse.py`` 内部已丢弃 0 分）。
        """
        hits = SparseIndex(self._chunks).search(query, candidates)
        if self._sparse_min_score > 0:
            hits = [(node_id, score) for node_id, score in hits if score >= self._sparse_min_score]
        return hits

    def _visible_doc_ids(
        self,
        filters: dict | None,
        *,
        trace: dict | None = None,
        focus: set[str] | None = None,
    ) -> set[str]:
        """按可见性过滤 ``doc_id``（默认最小可见集：仅公开文档）。

        判定统一走 ``metadata.exclusion_reason``：实际过滤与「界面解释为什么被丢弃」
        共用同一份规则，避免两边各写一套。
        """
        options = filters or {}
        caller_tags = [str(tag) for tag in (options.get("acl_tags") or [])]
        doc_types = [str(item) for item in (options.get("doc_types") or [])]
        journeys = [str(item) for item in (options.get("journeys") or [])]
        appliances = [str(item) for item in (options.get("appliances") or [])]
        product_ids = [str(item) for item in (options.get("product_ids") or [])]
        brands = [str(item) for item in (options.get("brands") or [])]
        allowed_roles = {str(role) for role in (options.get("allowed_roles") or [])}

        def _reason(meta: DocMeta) -> str | None:
            """判断某个文档是否不可见，并给出原因（与真实过滤共用同一套规则）。

            :param meta: 文档元数据。
            :returns: ``None`` 表示可见；否则返回原因标签（如 ``acl`` / ``journey`` / ``role``）。
            """
            reason = exclusion_reason(
                meta,
                caller_tags=caller_tags,
                doc_types=doc_types,
                journeys=journeys,
                appliances=appliances,
                product_ids=product_ids,
                brands=brands,
            )
            if reason is None and allowed_roles and not allowed_roles.intersection(meta.roles):
                return "role"
            return reason

        visible = {
            doc_id for doc_id, meta in self._meta.items() if _reason(meta) is None
        }

        if trace is not None:
            dropped = []
            for doc_id in sorted(focus or ()):
                meta = self._meta.get(doc_id)
                if meta is None:
                    continue
                reason = _reason(meta)
                if reason is not None:
                    dropped.append(
                        {
                            "doc_id": doc_id,
                            "reason": reason,
                            "journey": meta.journey,
                            "doc_type": meta.doc_type,
                            "appliance": meta.appliance,
                            "product_id": meta.product_id,
                        }
                    )
            trace["filter"] = {
                "applied": {
                    "journeys": journeys,
                    "appliances": appliances,
                    "product_ids": product_ids,
                    "brands": brands,
                    "doc_types": doc_types,
                    "allowed_roles": sorted(allowed_roles),
                    "acl_tags": caller_tags,
                },
                "total_docs": len(self._meta),
                "visible_docs": len(visible),
                "focus_docs": len(focus or ()),
                "dropped": dropped,
            }
        return visible

    def doc_meta(self, doc_id: str) -> DocMeta | None:
        """取某文档的元数据（供工具/面板展示出处与依据等级）。"""
        return self._meta.get(doc_id)

    def _rebuild_product_brand(self) -> None:
        """从身份卡重建「型号 → 品牌」映射。

        品牌政策的 ``product_id`` 为空（品牌级），只有 ``brand``；因此按已知型号收窄
        品牌时，必须由服务端查这张表，而不是让模型在参数里自报品牌。
        """
        self._product_brand = {
            str(meta.product_id): str(meta.brand)
            for meta in self._meta.values()
            if meta.doc_type == "identity" and meta.product_id and meta.brand
        }

    def brand_of_product(self, product_id: str) -> str | None:
        """查某型号的品牌（没有身份卡或身份卡未写 brand 时返回 ``None``）。"""
        return self._product_brand.get(str(product_id))

    def _splitter(self) -> SentenceSplitter:
        """每次新建分块器：参数来自实例，**不写全局 Settings**。"""
        return SentenceSplitter(
            chunk_size=self._chunk_size, chunk_overlap=self._chunk_overlap
        )

    def _make_records(
        self, doc_ids: list[str], doc_metas: dict[str, DocMeta], *, text_of: dict[str, str]
    ) -> tuple[list[ChunkRecord], list[TextNode]]:
        """对给定文档分块、嵌入，产出「分块记录 + 框架节点」两份产物。"""
        splitter = self._splitter()
        records: list[ChunkRecord] = []
        nodes: list[TextNode] = []

        for doc_id in doc_ids:
            meta = doc_metas[doc_id]
            document = Document(text=text_of[doc_id], doc_id=doc_id, metadata={"doc_id": doc_id})
            doc_nodes = splitter.get_nodes_from_documents([document])
            texts = [node.text for node in doc_nodes]
            vectors = self._embedder.embed(texts)
            for chunk_idx, (node_text, vector) in enumerate(zip(texts, vectors, strict=True)):
                node_id = f"{doc_id}:{chunk_idx}"
                node = TextNode(
                    text=node_text,
                    id_=node_id,
                    metadata={
                        "doc_id": doc_id,
                        "doc_type": meta.doc_type,
                        "journey": meta.journey or "",
                        "chunk_idx": chunk_idx,
                    },
                )
                node.embedding = list(vector)
                nodes.append(node)
                records.append(
                    ChunkRecord(
                        node_id=node_id,
                        doc_id=doc_id,
                        chunk_idx=chunk_idx,
                        text=node_text,
                        doc_type=meta.doc_type,
                        acl_tags=tuple(meta.acl_tags),
                        roles=tuple(meta.roles),
                        journey=meta.journey or "",
                    )
                )
        return records, nodes

    def _build_full(
        self, current: dict[str, str], doc_metas: dict[str, DocMeta]
    ) -> tuple[list[ChunkRecord], SimpleVectorStore]:
        """全量构建：清空既有产物后重新分块嵌入。"""
        text_of = self._read_documents(current)
        records, nodes = self._make_records(sorted(current), doc_metas, text_of=text_of)
        store = SimpleVectorStore()
        if nodes:
            store.add(nodes)
        return records, store

    def _apply_incremental(
        self,
        existing: IndexManifest,
        current: dict[str, str],
        doc_metas: dict[str, DocMeta],
        diff,
    ) -> tuple[list[ChunkRecord], SimpleVectorStore]:
        """增量更新：删除 → 重插 → 保留未变动文档。"""
        store_path = self.index_dir / VECTOR_STORE_FILENAME
        if not store_path.exists():
            raise RagIndexError(f"索引缺少向量存储文件：{store_path}（请执行 --rebuild）")

        store = SimpleVectorStore.from_persist_path(str(store_path))
        stale = set(diff.removed) | set(diff.modified)
        # 先删：按 doc_id（节点 ref_doc_id）整篇移除，避免同 id 重插时新旧混杂
        for doc_id in sorted(stale):
            store.delete(doc_id)

        kept = [record for record in load_chunks(self.index_dir) if record.doc_id not in stale]

        changed = sorted(diff.added + diff.modified)
        if changed:
            text_of = self._read_documents({doc_id: current[doc_id] for doc_id in changed})
            new_records, nodes = self._make_records(changed, doc_metas, text_of=text_of)
            if nodes:
                store.add(nodes)
            kept.extend(new_records)
        return kept, store

    def _read_documents(self, current: dict[str, str]) -> dict[str, str]:
        """读取文档正文（键为 ``doc_id``），**去掉 front-matter**。

        元数据只用于过滤与溯源；若把它一起送进分块与嵌入，每篇文档都会共享同一批
        ``chunk_id`` / ``scenario`` / ``source`` 之类的词，稀释真实内容的权重。
        """
        texts: dict[str, str] = {}
        for doc_id in sorted(current):
            raw = (self.knowledge_dir / doc_id).read_text(encoding="utf-8")
            _meta, body = parse_front_matter(raw)
            texts[doc_id] = body.strip()
        return texts

    def _entries_for(self, records: list[ChunkRecord]) -> list[DocEntry]:
        """由分块记录汇总出清单里的文档条目（含 chunk 数）。"""
        counts: dict[str, int] = {}
        digests: dict[str, str] = {}
        for record in records:
            counts[record.doc_id] = counts.get(record.doc_id, 0) + 1
        for doc_id in counts:
            digests[doc_id] = scan_documents(self.knowledge_dir).get(doc_id, "")
        return [
            DocEntry(
                doc_id=doc_id,
                path=doc_id,
                content_sha256=digests.get(doc_id, ""),
                chunk_count=counts[doc_id],
            )
            for doc_id in sorted(counts)
        ]

    def _chunk_count_for(self, existing: IndexManifest | None, diff) -> int:
        """``--dry-run`` 时预估 chunk 总数（不重新分块，避免只读命令做重活）。"""
        if existing is None:
            return 0
        kept = sum(
            entry.chunk_count
            for entry in existing.docs
            if entry.doc_id not in set(diff.removed) | set(diff.modified)
        )
        return kept

    def _report(
        self,
        *,
        mode: str,
        diff,
        chunk_total: int,
        rebuild_reasons: list[str],
        started: float,
    ) -> IndexBuildReport:
        """组装报告对象。"""
        return IndexBuildReport(
            industry_id=self.industry_id,
            index_dir=str(self.index_dir),
            mode=mode,
            embedding_mode=self._embedding_mode,
            embed_model_id=self.embed_model_id,
            embed_dim=self.embed_dim,
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
            added=diff.added,
            modified=diff.modified,
            removed=diff.removed,
            unchanged=diff.unchanged,
            chunk_total=chunk_total,
            rebuild_reasons=rebuild_reasons,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
