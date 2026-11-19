"""token / 成本 / 延迟汇总（P6）。

真实供应商返回 ``usage_metadata`` 时用真实值；Fake 或缺失时按字符估算，
并在报告里标 ``estimated=true``——估算数字不得与真实账单混比。
"""

from __future__ import annotations

from typing import Any, Iterable

from zkagent.core.evals.models import CaseResult, UsageSummary

__all__ = ["PRICING_CNY_PER_MILLION", "estimate_usage", "summarize"]

#: 每百万 token 价格（人民币）：(输入, 输出)。仅用于演示量级判断。
PRICING_CNY_PER_MILLION: dict[str, tuple[float, float]] = {
    "deepseek-chat": (2.0, 8.0),
    "deepseek-reasoner": (4.0, 16.0),
    "qwen-plus": (0.8, 2.0),
    "gpt-4o-mini": (1.0, 4.0),
}


def estimate_usage(text: str) -> dict[str, int]:
    """按字符数保守估算 token：中文 1 字符≈1 token。"""
    tokens = max(1, len(text))
    return {
        "input_tokens": tokens // 2,
        "output_tokens": tokens - tokens // 2,
        "total_tokens": tokens,
    }


def summarize(
    results: Iterable[CaseResult],
    *,
    model_id: str | None = None,
    estimated: bool | None = None,
) -> UsageSummary:
    """汇总多条用例的 token、成本与耗时。

    供应商没回传 usage 时，按最终回答的字符数**保守估算**，并把 ``estimated``
    置为 ``True``——估算值不能冒充真实计数。

    :param results: 用例结果列表。
    :param model_id: 用于查价目表的模型标识；未知型号按 0 成本计。
    :param estimated: 强制指定「是否估算」；默认由数据决定（任一条缺 usage 即为估算）。
    :returns: 汇总后的 :class:`UsageSummary`。
    """
    items = list(results)
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    used_estimate = False
    for item in items:
        current = dict(item.usage or {})
        if not current:
            current = estimate_usage(item.final_text)
            used_estimate = True
        for key in usage:
            usage[key] += int(current.get(key) or 0)

    price_in, price_out = PRICING_CNY_PER_MILLION.get(model_id or "", (0.0, 0.0))
    cost = (
        usage["input_tokens"] / 1_000_000 * price_in
        + usage["output_tokens"] / 1_000_000 * price_out
    )
    return UsageSummary(
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        total_tokens=usage["total_tokens"],
        cost_cny=round(cost, 4),
        latency_ms=sum(int(item.latency_ms or 0) for item in items),
        estimated=used_estimate if estimated is None else estimated,
    )


def as_dict(summary: UsageSummary) -> dict[str, Any]:
    """把用量汇总转成普通 dict（便于直接塞进 JSON 报告）。

    :param summary: 用量汇总。
    :returns: 字段与 :class:`UsageSummary` 一致的字典。
    """
    return summary.model_dump()
