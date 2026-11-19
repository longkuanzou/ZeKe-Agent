"""答案级引用校验（S2 / M17）。

口径与面板一致：引用编号必须落在**本轮**知识检索返回的片段范围内；
未检索的红线/越界场景出现任何引用都算失败。
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["citation_numbers", "validate_citations"]

_CITATION = re.compile(r"\[(\d+)\]")
_SOURCE = re.compile(r"(?:source|来源)\s*[:：]\s*([^\s，。；;]+)", re.IGNORECASE)


def citation_numbers(text: str) -> list[int]:
    """抽出回答里的引用编号（形如 ``[1]``、``[2]``）。

    :param text: 模型最终回答。
    :returns: 按出现顺序排列的编号列表，可能含重复。
    """
    return [int(match.group(1)) for match in _CITATION.finditer(text)]


def _knowledge_entries(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """挑出本轮**成功**的知识检索调用，作为引用的合法来源。

    :param tool_calls: :class:`AgentTurnResult` 里的工具调用记录。
    :returns: ``knowledge_search`` 且 ``ok`` 的调用记录列表。
    """
    return [
        call
        for call in tool_calls
        if call.get("name") == "knowledge_search" and call.get("ok")
    ]


def validate_citations(
    text: str, tool_calls: list[dict[str, Any]], mode: str
) -> list[str]:
    """校验回答中的引用是否「有据可依」。

    :param text: 模型最终回答。
    :param tool_calls: 本轮的调用记录，用来确定片段的合法范围。
    :param mode: ``subset`` 表示引用必须落在本轮检索结果内；``none`` 表示
        未检索场景下不得出现任何引用编号或来源。
    :returns: 失败原因列表；空列表表示通过。
    """
    numbers = citation_numbers(text)
    sources = [match.group(1) for match in _SOURCE.finditer(text)]
    entries = _knowledge_entries(tool_calls)

    if mode == "none":
        failures: list[str] = []
        if numbers:
            failures.append(f"未检索场景出现了引用编号：{numbers}")
        if sources:
            failures.append(f"未检索场景出现了来源：{sources[:3]}")
        return failures

    if not entries:
        return ["回答使用了引用，但本轮没有任何知识检索结果"] if (numbers or sources) else []

    allowed_count = max(int(entry.get("chunk_count") or 0) for entry in entries)
    allowed_sources = {
        str(source)
        for entry in entries
        for source in (entry.get("sources") or [])
    }

    failures = []
    out_of_range = [number for number in numbers if number < 1 or number > allowed_count]
    if out_of_range:
        failures.append(f"引用编号超出本轮片段范围（1..{allowed_count}）：{out_of_range}")
    unknown = [source for source in sources if source not in allowed_sources]
    if unknown:
        failures.append(f"引用来源不在本轮检索结果内：{unknown[:3]}")
    return failures
