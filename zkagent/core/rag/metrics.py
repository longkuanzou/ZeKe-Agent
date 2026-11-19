"""检索指标：Recall@k / MRR@k / nDCG@k——**框架无关**（纯计算 + YAML 加载）。

口径（DESIGN §6.3.7）：

| 指标 | 公式 | 回答的问题 |
| --- | --- | --- |
| ``Recall@k`` | ``|检索到的相关文档| / |全部相关文档|`` | 该找到的有没有全找到 |
| ``MRR@k`` | ``1 / 第一个相关结果的排名``（无相关结果记 0） | 用户多久看到第一个有用结果 |
| ``nDCG@k`` | ``DCG@k / IDCG@k``，``DCG@k = Σ (2^rel(i) - 1) / log2(i + 1)`` | 最相关的有没有排在前面 |

**评测粒度是文档级**：检索返回的是分块，先把分块按 ``doc_id`` 去重成文档序列再算指标
（这与 gold 标注的 ``doc_id`` 直接对齐，也让标注不必依赖分块边界）。

**报告纪律**：指标必须与 ``embedding_mode`` / ``rerank`` / ``k`` 一起出现；
降级口径（``fallback_hash``）的数字**不得**与真实模型口径混在同一条基线里比较。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

__all__ = [
    "RetrievalEvalError",
    "GoldHit",
    "RetrievalCase",
    "RetrievalEvalSet",
    "load_eval_set",
    "recall_at_k",
    "mrr_at_k",
    "ndcg_at_k",
    "document_ranking",
    "evaluate_case",
    "evaluate_set",
]


class RetrievalEvalError(ValueError):
    """评测用例文件缺失或结构非法——显式失败，避免「零用例也算通过」。"""


class GoldHit(BaseModel):
    """一条 gold 标注：相关文档与相关性等级（``rel`` 越大越相关，供 nDCG 使用）。"""

    doc_id: str
    rel: int = Field(default=1, ge=1)
    #: 为什么把它列为 gold（M13：原文 + 等价 FAQ 时逐条写理由，避免为指标放宽）
    note: str = ""


class RetrievalCase(BaseModel):
    """一条检索评测用例。"""

    id: str
    query: str
    gold: list[GoldHit] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    #: **证据链路** = gold 分片所在目录；用于分组指标（直接暴露「哪条链路的知识没被检索到」）。
    journey: str | None = None
    #: 题目自身的链路（上游 scenario）。与 ``journey`` 不同 = 证据在别的链路里，
    #: 此时若生产链路按 ``journey`` 收窄，这条 gold 会被过滤掉。
    intent_journey: str | None = None
    note: str = ""


class RetrievalEvalSet(BaseModel):
    """``evals/retrieval.yaml`` 的强类型表示。"""

    version: int = 1
    queries: list[RetrievalCase] = Field(default_factory=list)


def load_eval_set(path: Path | str) -> RetrievalEvalSet:
    """加载检索评测用例；文件不存在或结构非法即报错。"""
    file = Path(path)
    if not file.exists():
        raise RetrievalEvalError(f"检索评测用例不存在：{file}")
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RetrievalEvalError(f"评测用例 YAML 解析失败：{file}：{exc}") from exc
    if not isinstance(raw, dict):
        raise RetrievalEvalError(f"评测用例顶层必须是映射：{file}")
    try:
        return RetrievalEvalSet.model_validate(raw)
    except Exception as exc:
        raise RetrievalEvalError(f"评测用例结构非法：{file}：{exc}") from exc


# --------------------------------------------------------------------------- 指标


def document_ranking(node_ids: list[str]) -> list[str]:
    """把分块序列按 ``doc_id`` 去重成文档序列（保留首次出现的顺序）。

    ``node_id`` 形如 ``doc_id:chunk_idx``；同一个文档的多个分块只算一次，
    否则「一篇文档切了很多块」会凭空拉高召回。
    """
    seen: list[str] = []
    for node_id in node_ids:
        doc_id = node_id.split(":", 1)[0]
        if doc_id not in seen:
            seen.append(doc_id)
    return seen


def recall_at_k(ranking: list[str], gold: dict[str, int], k: int) -> float:
    """Recall@k：命中 gold 文档占全部 gold 的比例。"""
    if not gold:
        return 0.0
    top = set(ranking[:k])
    return len(top & set(gold)) / len(gold)


def mrr_at_k(ranking: list[str], gold: dict[str, int], k: int) -> float:
    """MRR@k：第一个相关结果的排名倒数（无相关结果记 0）。"""
    for rank, doc_id in enumerate(ranking[:k], start=1):
        if doc_id in gold:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranking: list[str], gold: dict[str, int], k: int) -> float:
    """nDCG@k：分级相关性下的排序质量。"""
    if not gold:
        return 0.0
    dcg = 0.0
    for rank, doc_id in enumerate(ranking[:k], start=1):
        rel = gold.get(doc_id, 0)
        if rel:
            dcg += (2**rel - 1) / math.log2(rank + 1)
    ideal = sorted(gold.values(), reverse=True)[:k]
    idcg = sum((2**rel - 1) / math.log2(rank + 1) for rank, rel in enumerate(ideal, start=1))
    return dcg / idcg if idcg else 0.0


def evaluate_case(
    *, node_ids: list[str], gold: list[GoldHit], k: int
) -> dict[str, float]:
    """对单条用例计算三个指标（输入是检索返回的 ``node_id`` 序列）。"""
    ranking = document_ranking(node_ids)
    gold_map = {hit.doc_id: hit.rel for hit in gold}
    return {
        f"recall@{k}": recall_at_k(ranking, gold_map, k),
        f"mrr@{k}": mrr_at_k(ranking, gold_map, k),
        f"ndcg@{k}": ndcg_at_k(ranking, gold_map, k),
    }


def evaluate_set(
    retriever: Any,
    cases: list[RetrievalCase],
    *,
    k: int = 5,
    mode: str | None = None,
) -> dict[str, Any]:
    """对全部用例跑一遍，返回「逐条结果 + 宏平均」两段。

    :param mode: 传给检索器的检索模式（``dense`` / ``sparse`` / ``hybrid`` / ``hybrid+rerank``）。
    """
    metric_names = [f"recall@{k}", f"mrr@{k}", f"ndcg@{k}"]
    per_case: list[dict[str, Any]] = []
    totals = dict.fromkeys(metric_names, 0.0)
    #: 链路 -> 指标累加与用例数（分组指标：哪条链路弱一眼可见）
    grouped: dict[str, dict[str, Any]] = {}

    for case in cases:
        chunks = retriever.search(case.query, top_k=k, filters=case.filters or None, mode=mode)
        metrics = evaluate_case(node_ids=[chunk.node_id for chunk in chunks], gold=case.gold, k=k)
        for name, value in metrics.items():
            totals[name] += value
        bucket = grouped.setdefault(
            case.journey or "unlabeled", {"cases": 0, **dict.fromkeys(metric_names, 0.0)}
        )
        bucket["cases"] += 1
        for name, value in metrics.items():
            bucket[name] += value
        per_case.append(
            {
                "id": case.id,
                "query": case.query,
                "journey": case.journey,
                "intent_journey": case.intent_journey,
                "retrieved": document_ranking([chunk.node_id for chunk in chunks]),
                "gold": [hit.doc_id for hit in case.gold],
                **{name: round(value, 4) for name, value in metrics.items()},
            }
        )

    count = len(cases) or 1
    by_journey = {
        journey: {
            "cases": bucket["cases"],
            **{name: round(bucket[name] / bucket["cases"], 4) for name in metric_names},
        }
        for journey, bucket in sorted(grouped.items())
    }
    return {
        "k": k,
        "mode": mode or "default",
        "cases": len(cases),
        "summary": {name: round(total / count, 4) for name, total in totals.items()},
        "by_journey": by_journey,
        "per_case": per_case,
    }
