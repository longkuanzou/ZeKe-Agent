"""上下文压缩：只改内容，不拆配对。

P2 曾用「截断」冒充压缩，结果把工具返回的 JSON 截成不可解析文本，下游把成功结果
误读成「非结构化内容」。因此这里的纪律是：

1. **可解析**：JSON 载荷压缩后仍是合法 JSON，且保留 ``ok`` / ``error_code`` / ``message``
   这类决定后续行为的关键字段；
2. **不拆配对**：只替换 ``tool`` 消息的内容，或整轮替换为一条摘要，绝不单独删掉
   ``tool`` 消息而留下前面的 ``tool_calls``；
3. **幂等**：已压缩的内容带标记，重复调用不会二次丢信息。
"""

from __future__ import annotations

import json
from typing import Any

from zkagent.core.types import ChatMessage

__all__ = [
    "HISTORY_SUMMARY_PREFIX",
    "compress_tool_payload",
    "summarize_old_turns",
    "split_turns",
]

#: 已压缩工具载荷的前缀（同时作为幂等标记）。
TOOL_PREFIX = "[工具 {name} 返回] "
COMPRESSED_TOOL_PREFIX = "[工具 {name} 返回·已压缩] "

#: 历史摘要消息的前缀。
HISTORY_SUMMARY_PREFIX = "[历史摘要] "

#: 无论如何都要保留的顶层字段：少了它们，上层无法判断成功/失败。
_CRITICAL_KEYS = ("ok", "error_code", "message", "count", "note", "source", "status")


def _truncate(text: str, limit: int, *, marker: str = "…") -> str:
    """按字符数截断文本，超长时在末尾留省略标记。

    :param text: 原文。
    :param limit: 最大长度（含省略标记）；``<= 0`` 表示不截断。
    :param marker: 省略标记，默认 ``…``。
    :returns: 截断后的文本；未超长时原样返回。
    """
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - len(marker))] + marker


def _compact_value(value: Any, *, max_items: int, max_str: int, depth: int = 0) -> Any:
    """递归压缩单个值：长字符串截断，长列表只留前 ``max_items`` 条并记录省略数。"""
    if depth >= 3:
        return _truncate(str(value), max_str)
    if isinstance(value, dict):
        return {
            key: _compact_value(item, max_items=max_items, max_str=max_str, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        kept = [
            _compact_value(item, max_items=max_items, max_str=max_str, depth=depth + 1)
            for item in value[:max_items]
        ]
        if len(value) > max_items:
            # 用显式字段表达「还有多少条」，避免上层把 2 条当成全部
            return {"_items": kept, "_omitted": len(value) - max_items}
        return kept
    if isinstance(value, str):
        return _truncate(value, max_str)
    return value


def _envelope(data: Any, original_chars: int) -> dict[str, Any]:
    """兜底信封：压缩后仍超预算时，保留关键字段 + 顶层键名，保证可解析。"""
    payload = {key: data[key] for key in _CRITICAL_KEYS if isinstance(data, dict) and key in data}
    payload["_compressed"] = True
    payload["_original_chars"] = original_chars
    payload["_keys"] = sorted(data)[:12] if isinstance(data, dict) else []
    return payload


def compress_tool_payload(
    content: str,
    *,
    max_chars: int = 1200,
    max_items: int = 2,
    max_str: int = 160,
) -> tuple[str, bool]:
    """压缩一条工具消息的内容。

    :return: ``(新内容, 是否发生变化)``。非 JSON 载荷退回字符截断并加显式标记；
             已压缩过的内容原样返回（幂等）。
    """
    prefix = ""
    body = content
    if content.startswith("[工具 ") and "] " in content:
        head, body = content.split("] ", 1)
        prefix = head + "] "

    if "已压缩" in prefix:
        return content, False

    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        compressed = _truncate(body, max_chars)
        if compressed == body:
            return content, False
        return f"{prefix}{compressed}" if prefix else compressed, True

    # 已在预算内 → 不再压缩，避免「压缩产物被再次压缩」造成的二次信息丢失
    if len(body) <= max_chars:
        return content, False
    if isinstance(data, dict) and data.get("_compressed") is True:
        return content, False

    compact = _compact_value(data, max_items=max_items, max_str=max_str)
    text = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(text) > max_chars:
        text = json.dumps(_envelope(data, len(body)), ensure_ascii=False, separators=(",", ":"))

    if text == body:
        return content, False

    if prefix:
        # 保留原前缀里的工具名，把「返回」升级为「返回·已压缩」
        prefix = prefix.replace(" 返回]", " 返回·已压缩]")
    return f"{prefix}{text}", True


def split_turns(messages: list[ChatMessage]) -> list[list[ChatMessage]]:
    """按 ``user`` 消息切轮；没有 user 的头部消息归入第一轮（不丢消息）。"""
    turns: list[list[ChatMessage]] = []
    for message in messages:
        if message.role == "user" or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns


def _tool_outcome(message: ChatMessage) -> str:
    """从工具消息里提取「工具名=ok/错误码」，解析失败就标 unknown。"""
    name = message.name or "tool"
    body = message.content
    if "] " in body:
        body = body.split("] ", 1)[1]
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return f"{name}=unknown"
    if isinstance(data, dict):
        if data.get("ok") is True:
            return f"{name}=ok"
        code = data.get("error_code") or "failed"
        return f"{name}={code}"
    return f"{name}=ok"


def _summarize_turn(turn: list[ChatMessage], *, max_chars: int) -> str:
    """把一整轮对话压成一行确定性摘要（不调模型，可复现）。

    保留「用户问了什么 / 调用了哪些工具 / 结果成败 / 最终回答」四要素——
    这些是后续轮次继续推理的最小信息集。工具**载荷**不保留，只留结论。

    :param turn: 同一轮内的消息（user → assistant → tool → assistant）。
    :param max_chars: 摘要总长度上限。
    :returns: 形如 ``用户：…；工具：get_order；结果：get_order=ok；回答：…`` 的单行文本。
    """
    user_text = ""
    tool_names: list[str] = []
    outcomes: list[str] = []
    answer = ""

    for message in turn:
        if message.role == "user" and not user_text:
            user_text = _truncate(message.content.replace("\n", " "), max_chars // 3)
        elif message.role == "assistant":
            tool_names.extend(str(call.get("name")) for call in message.tool_calls)
            if message.content:
                answer = _truncate(message.content.replace("\n", " "), max_chars // 3)
        elif message.role == "tool":
            outcomes.append(_tool_outcome(message))

    parts = [f"用户：{user_text or '（空）'}"]
    if tool_names:
        parts.append(f"工具：{', '.join(tool_names)}")
    if outcomes:
        parts.append(f"结果：{', '.join(outcomes)}")
    if answer:
        parts.append(f"回答：{answer}")
    return _truncate("；".join(parts), max_chars)


def summarize_old_turns(
    messages: list[ChatMessage],
    *,
    keep_recent_turns: int,
    max_chars: int = 600,
) -> tuple[list[ChatMessage], int]:
    """把最旧的若干轮压成一条摘要，最近 ``keep_recent_turns`` 轮原样保留。

    :return: ``(新消息列表, 被摘要的轮数)``。整轮替换，因此不会出现
             「有 tool 消息、没有前置 tool_calls」的断裂。
    """
    turns = split_turns(messages)
    keep = max(1, keep_recent_turns)
    if len(turns) <= keep:
        return messages, 0

    old, recent = turns[:-keep], turns[-keep:]
    lines = [
        f"{index}. {_summarize_turn(turn, max_chars=max_chars // max(1, len(old)))}"
        for index, turn in enumerate(old, start=1)
    ]
    summary = _truncate(
        HISTORY_SUMMARY_PREFIX + "；".join(lines),
        max_chars,
    )
    return [
        ChatMessage(role="system", content=summary),
        *[m for turn in recent for m in turn],
    ], len(old)
