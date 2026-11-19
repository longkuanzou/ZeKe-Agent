"""分块仓储、BM25 稀疏检索与 RRF 融合——**框架无关**（只用 ``rank_bm25`` 与标准库）。

为什么不直接用框架的 BM25 检索器：

1. **必须能离线持久化与重建**：``rank_bm25`` 是纯 Python 库，索引可由分块文本随时重建，
   不需要额外依赖，也不会把框架对象带出接缝；
2. **必须与权限过滤共用同一份元数据**：分块记录（文本 + ``doc_id`` + ``doc_type`` +
   ``acl_tags``）在这里是唯一来源，稠密 / 稀疏两条通路命中后都用同一套过滤；
3. **RRF 融合是纯排序逻辑**，与框架无关，放在接缝外更易测试。

``chunks.json`` 与 manifest 同目录，属运行期产物（不进版本库）。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rank_bm25 import BM25Okapi

from zkagent.core.rag.errors import RagError

__all__ = [
    "ChunkRecord",
    "SparseIndexError",
    "tokenize",
    "reciprocal_rank_fusion",
    "SparseIndex",
    "chunks_path",
    "save_chunks",
    "load_chunks",
]

#: 分块记录文件名（与 manifest.json 同目录）。
CHUNKS_FILENAME = "chunks.json"

#: 中文/英文/数字的连续片段；其余字符视为分隔符。
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")

#: CJK 单字 n-gram 的最大长度（中文不依赖外部分词器，用 n-gram 近似）。
_CJK_NGRAM = 2


class SparseIndexError(RagError):
    """BM25 索引构建失败（如没有任何分块）——显式失败，不返回空结果掩盖问题。"""


@dataclass(frozen=True)
class ChunkRecord:
    """一个分块：检索与过滤所需的最小信息集合。"""

    node_id: str
    doc_id: str
    chunk_idx: int
    text: str
    doc_type: str = "faq"
    acl_tags: tuple[str, ...] = field(default_factory=tuple)
    roles: tuple[str, ...] = field(default_factory=tuple)
    #: 用户链路（``presale`` / ``install`` / …）；空串表示未分类。随分块一起持久化，
    #: 保证稠密、稀疏两条通路过滤时用的是**同一份**归位结果。
    journey: str = ""

    def to_dict(self) -> dict[str, object]:
        """转为可 JSON 序列化的字典（元组转列表）。"""
        data = asdict(self)
        data["acl_tags"] = list(self.acl_tags)
        data["roles"] = list(self.roles)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ChunkRecord":
        """从磁盘记录恢复。"""
        return cls(
            node_id=str(data["node_id"]),
            doc_id=str(data["doc_id"]),
            chunk_idx=int(data.get("chunk_idx", 0)),
            text=str(data.get("text", "")),
            doc_type=str(data.get("doc_type", "faq")),
            acl_tags=tuple(str(tag) for tag in (data.get("acl_tags") or [])),
            roles=tuple(str(role) for role in (data.get("roles") or [])),
            journey=str(data.get("journey") or ""),
        )


def tokenize(text: str) -> list[str]:
    """把文本切成检索用 token：ASCII 片段整词 + CJK 单字与双字组合。

    中文没有空格分词，用字符 n-gram 近似：既保留「保修」这类词的区分度，
    又不引入 jieba 这类额外依赖（MVP 语料规模下足够）。
    """
    tokens: list[str] = []
    for match in _TOKEN_PATTERN.finditer(text.lower()):
        piece = match.group(0)
        if piece.isascii():
            tokens.append(piece)
            continue
        chars = list(piece)
        tokens.extend(chars)
        for size in range(2, _CJK_NGRAM + 1):
            tokens.extend(
                "".join(chars[index : index + size]) for index in range(len(chars) - size + 1)
            )
    return tokens


def reciprocal_rank_fusion(
    rankings: list[list[str]], *, k: int = 60, weights: list[float] | None = None
) -> list[tuple[str, float]]:
    """RRF 融合多个排序列表（DESIGN §6.3.5：rerank 关闭时的替代方案）。

    ``score(d) = Σ_i w_i / (k + rank_i(d))``；只出现在一个列表里的文档也能获得分数，
    因此不会因为某一路召回为空而整体失效。
    """
    if not rankings:
        return []
    if weights is not None and len(weights) != len(rankings):
        raise ValueError("weights 长度必须与 rankings 一致")

    scores: dict[str, float] = {}
    for index, ranking in enumerate(rankings):
        weight = 1.0 if weights is None else weights[index]
        for rank, node_id in enumerate(ranking, start=1):
            scores[node_id] = scores.get(node_id, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


class SparseIndex:
    """基于 ``rank_bm25`` 的稀疏检索（由分块记录构建，可随时重建）。"""

    def __init__(self, records: list[ChunkRecord], *, k1: float = 1.5, b: float = 0.75) -> None:
        self._records = list(records)
        self._by_id = {record.node_id: record for record in self._records}
        if self._records:
            self._bm25 = BM25Okapi([tokenize(record.text) for record in self._records], k1=k1, b=b)
        else:
            self._bm25 = None

    def __len__(self) -> int:
        """分块数量。"""
        return len(self._records)

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """返回 ``[(node_id, score)]``，按分数降序；无分块时返回空列表。"""
        if self._bm25 is None or top_k <= 0:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(
            zip(self._records, scores, strict=True), key=lambda pair: -pair[1]
        )
        return [
            (record.node_id, float(score))
            for record, score in ranked[:top_k]
            if score > 0.0  # 0 分表示完全没有词面重叠，返回它只会稀释融合结果
        ]

    def record(self, node_id: str) -> ChunkRecord | None:
        """按 ``node_id`` 取分块记录。"""
        return self._by_id.get(node_id)


# --------------------------------------------------------------------------- 持久化


def chunks_path(index_dir: Path | str) -> Path:
    """分块记录文件路径。"""
    return Path(index_dir) / CHUNKS_FILENAME


def save_chunks(index_dir: Path | str, records: list[ChunkRecord]) -> Path:
    """原子写入分块记录（临时文件 + replace）。"""
    directory = Path(index_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = chunks_path(directory)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps([record.to_dict() for record in records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def load_chunks(index_dir: Path | str) -> list[ChunkRecord]:
    """读取分块记录；不存在返回空列表，结构损坏则显式报错。"""
    path = chunks_path(index_dir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SparseIndexError(f"分块记录不是合法 JSON：{path}：{exc}") from exc
    if not isinstance(raw, list):
        raise SparseIndexError(f"分块记录顶层必须是列表：{path}")
    return [ChunkRecord.from_dict(item) for item in raw]
