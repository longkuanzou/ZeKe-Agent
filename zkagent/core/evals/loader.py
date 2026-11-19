"""评估用例加载与结构校验。"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml

from zkagent.core.evals.models import CATEGORIES, EvalCase

__all__ = ["EvalCaseError", "load_cases", "validate_cases"]


class EvalCaseError(ValueError):
    """用例文件不合法（缺字段、重复 id、分类不全）。"""


def load_cases(path: Path | str) -> list[EvalCase]:
    """读取 ``cases.yaml``；任何结构问题都在这里报错，不静默跳过。"""
    source = Path(path)
    if not source.is_file():
        raise EvalCaseError(f"用例文件不存在：{source}")
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    raw = data.get("cases")
    if not isinstance(raw, list) or not raw:
        raise EvalCaseError(f"{source.name} 的 cases 必须是非空列表")
    try:
        cases = [EvalCase.model_validate(item) for item in raw]
    except Exception as exc:  # pydantic 的报错信息已经足够定位
        raise EvalCaseError(f"{source.name} 用例字段非法：{exc}") from exc

    duplicated = [item for item, count in Counter(case.id for case in cases).items() if count > 1]
    if duplicated:
        raise EvalCaseError(f"{source.name} 存在重复用例 id：{duplicated}")
    return cases


def validate_cases(cases: list[EvalCase], *, minimum: int = 20) -> None:
    """校验规模与分类覆盖：P6 Gate 要求 ≥20 条且五类齐全。"""
    if len(cases) < minimum:
        raise EvalCaseError(f"用例数不足：{len(cases)} < {minimum}")
    present = {case.category for case in cases}
    missing = [name for name in CATEGORIES if name not in present]
    if missing:
        raise EvalCaseError(f"缺少分类：{missing}")
