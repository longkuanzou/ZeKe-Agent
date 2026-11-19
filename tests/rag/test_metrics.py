"""检索指标的形状与口径（框架无关）。

重点是**口径正确**，不是数值好看：换行、重复分块、gold 缺失等边界都必须有确定行为。
"""

from __future__ import annotations

import pytest

from zkagent.core.rag.metrics import (
    RetrievalEvalError,
    GoldHit,
    document_ranking,
    evaluate_case,
    load_eval_set,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
)


def test_document_ranking_dedupes_chunks_of_same_document() -> None:
    """同一文档的多个分块只算一次，否则「切得越碎召回越高」。"""
    ranking = document_ranking(["a.md:0", "a.md:1", "b.md:0", "a.md:2", "c.md:0"])
    assert ranking == ["a.md", "b.md", "c.md"]


def test_recall_at_k_counts_gold_coverage() -> None:
    """验证：Recall 按 gold 覆盖比例算，且只统计**截断窗口内**的命中。"""
    gold = {"a.md": 2, "b.md": 1}
    assert recall_at_k(["a.md", "x.md"], gold, k=5) == 0.5
    assert recall_at_k(["a.md", "b.md"], gold, k=5) == 1.0
    assert recall_at_k(["b.md", "a.md", "c.md"], gold, k=1) == 0.5, "只统计截断窗口内"
    assert recall_at_k([], gold, k=5) == 0.0


def test_mrr_at_k_uses_first_relevant_rank() -> None:
    """验证：MRR 取首个相关的名次倒数，窗口外命中不算（k=1 时第二位即 0）。"""
    gold = {"b.md": 1}
    assert mrr_at_k(["a.md", "b.md"], gold, k=5) == pytest.approx(0.5)
    assert mrr_at_k(["b.md"], gold, k=5) == 1.0
    assert mrr_at_k(["a.md", "c.md"], gold, k=5) == 0.0
    assert mrr_at_k(["a.md", "b.md"], gold, k=1) == 0.0, "截断窗口外不算命中"


def test_ndcg_at_k_rewards_higher_relevance_first() -> None:
    """验证：nDCG 对排序敏感——强相关排后面要扣分，理想排序为 1，全不相关为 0。"""
    gold = {"a.md": 2, "b.md": 1}
    good = ndcg_at_k(["a.md", "b.md"], gold, k=5)
    bad = ndcg_at_k(["b.md", "a.md"], gold, k=5)

    assert good == pytest.approx(1.0), "理想排序应为 1"
    assert bad < good, "强相关排在后面应当扣分"
    assert ndcg_at_k(["x.md"], gold, k=5) == 0.0


def test_metrics_are_zero_without_gold() -> None:
    """验证：gold 为空时三个指标都是 0——缺 gold 不能靠默认 1 分把分数抬上去。"""
    assert recall_at_k(["a.md"], {}, k=5) == 0.0
    assert mrr_at_k(["a.md"], {}, k=5) == 0.0
    assert ndcg_at_k(["a.md"], {}, k=5) == 0.0


def test_evaluate_case_returns_all_three_metrics() -> None:
    """验证：单条用例一次给出 recall/mrr/ndcg 三个指标，键名带 ``@k``（口径自解释）。"""
    metrics = evaluate_case(
        node_ids=["a.md:0", "a.md:1"],
        gold=[GoldHit(doc_id="a.md", rel=2)],
        k=5,
    )
    assert metrics == {"recall@5": 1.0, "mrr@5": 1.0, "ndcg@5": 1.0}


# --------------------------------------------------------------------------- 用例文件


def test_load_eval_set_reads_real_industry_pack() -> None:
    """真实行业包的评测集必须可加载且非空（零用例不允许当作通过）。"""
    from zkagent.config import default_industry_packs_dir

    path = default_industry_packs_dir() / "appliance" / "evals" / "retrieval.yaml"
    eval_set = load_eval_set(path)

    assert len(eval_set.queries) >= 10
    assert all(case.query and case.gold for case in eval_set.queries), "每条用例都要有 query 与 gold"
    assert len({case.id for case in eval_set.queries}) == len(eval_set.queries), "用例 id 不得重复"


def test_missing_eval_set_fails_loudly(tmp_path) -> None:
    """验证：评测文件不存在时报错——静默返回空集会让人误以为「全通过」。"""
    with pytest.raises(RetrievalEvalError, match="不存在"):
        load_eval_set(tmp_path / "nope.yaml")


def test_broken_eval_set_fails_loudly(tmp_path) -> None:
    """验证：评测文件结构非法时报错（gold 不是列表），不让坏数据悄悄变成 0 分用例。"""
    path = tmp_path / "retrieval.yaml"
    path.write_text("queries:\n  - id: x\n    query: y\n    gold: not-a-list\n", encoding="utf-8")

    with pytest.raises(RetrievalEvalError, match="结构非法"):
        load_eval_set(path)
