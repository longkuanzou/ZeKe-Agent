"""评估报告：Markdown 给人看，JSON 给回归对比。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from zkagent.core.evals.models import CaseResult, UsageSummary

__all__ = ["render_markdown", "write_report"]


def _rate(values: Iterable[bool]) -> float:
    """计算通过率（保留 4 位小数）。

    :param values: 逐条通过标记的可迭代对象。
    :returns: 通过比例；输入为空时返回 ``0.0``（宁可视作未通过，也不给满分）。
    """
    items = list(values)
    return round(sum(1 for item in items if item) / len(items), 4) if items else 0.0


def render_markdown(
    results: list[CaseResult],
    *,
    usage: UsageSummary,
    industry: str,
    provider: str,
    model_id: str,
    k: int,
    embedding_mode: str = "",
    db_path: str = "",
) -> str:
    """生成报告正文：总体 → 分类 → 失败明细 → 用量。"""
    graded = [item for item in results if not item.case_id.startswith("manual-")]
    pass1 = _rate(item.attempts[0] for item in graded if item.attempts)
    passk = _rate(item.passed for item in graded)

    lines = [
        f"# 评估报告：{industry}",
        "",
        f"- provider：`{provider}` ｜ model：`{model_id}` ｜ 重复次数 k：{k}",
        f"- embedding_mode：`{embedding_mode or 'n/a'}`",
        f"- 评估库：`{db_path or 'n/a'}`",
        f"- 用例数：{len(graded)} ｜ **Pass@1 = {pass1:.2%}** ｜ **Pass^{k} = {passk:.2%}**",
        "",
        "## 分类结果",
        "",
        "| 分类 | 用例数 | 通过 | Pass@1 |",
        "| --- | --- | --- | --- |",
    ]

    by_category: dict[str, list[CaseResult]] = {}
    for item in graded:
        by_category.setdefault(item.category, []).append(item)
    for category in ("normal", "boundary", "failure", "forbidden", "adversarial"):
        items = by_category.get(category, [])
        if not items:
            continue
        lines.append(
            f"| {category} | {len(items)} | {sum(1 for i in items if i.passed)} | "
            f"{_rate(i.attempts[0] for i in items if i.attempts):.2%} |"
        )

    failures = [item for item in graded if not item.passed]
    lines.extend(["", "## 失败明细", ""])
    if not failures:
        lines.append("无。")
    else:
        lines.extend(
            [
                "| 用例 | 分类 | 归因 | 详情 | trace_id |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in failures:
            detail = "；".join(
                check.detail for check in item.assertions if not check.passed and check.detail
            )
            lines.append(
                f"| {item.case_id} | {item.category} | {', '.join(item.failures)} | "
                f"{detail or '-'} | `{item.trace_id or '-'}` |"
            )

    attribution = Counter(reason for item in failures for reason in item.failures)
    lines.extend(["", "## 失败归因汇总", ""])
    if attribution:
        for reason, count in attribution.most_common():
            lines.append(f"- `{reason}`：{count}")
    else:
        lines.append("无。")

    lines.extend(
        [
            "",
            "## 用量与延迟",
            "",
            "| 指标 | 值 |",
            "| --- | --- |",
            f"| input tokens | {usage.input_tokens} |",
            f"| output tokens | {usage.output_tokens} |",
            f"| total tokens | {usage.total_tokens} |",
            f"| 成本（CNY） | {usage.cost_cny} |",
            f"| 累计延迟（ms） | {usage.latency_ms} |",
            f"| 是否估算 | {'是（Fake / 无 usage）' if usage.estimated else '否'} |",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(
    path: Path | str,
    markdown: str,
    results: list[CaseResult],
    *,
    usage: UsageSummary,
    meta: dict[str, Any],
) -> tuple[Path, Path]:
    """写 ``report.md`` 与同名 ``report.json``，返回两个路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")

    payload = {
        "meta": meta,
        "usage": usage.model_dump(),
        "summary": {
            "cases": len(results),
            "passed": sum(1 for item in results if item.passed),
            "pass_at_1": _rate(item.attempts[0] for item in results if item.attempts),
            "pass_at_k": _rate(item.passed for item in results),
        },
        "cases": [item.model_dump() for item in results],
    }
    json_path = target.with_suffix(".json")
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target, json_path
