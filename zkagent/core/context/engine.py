"""ContextEngine：静态前缀 + 状态栏 + 轨迹隔离（P5 再叠加压缩与渐进披露）。

三条设计约束：

1. **静态前缀恒定**：系统提示与少样本示例组成的 ``prefix`` 在会话内逐字不变，
   这样 Prompt Cache 才能命中；每轮变化的信息（步骤数、待确认状态）**单独成块**。
2. **轨迹隔离**：历史消息与工具原始返回**不进入前缀**；工具返回在回放时被压缩成
   一行摘要，避免把整段 JSON 反复塞回模型。
3. **可裁剪**：按「轮」保留最近 ``max_turns`` 轮，为 P5 的压缩策略留出接口。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from zkagent.core.context.compress import (
    HISTORY_SUMMARY_PREFIX,
    compress_tool_payload,
    split_turns,
    summarize_old_turns,
)
from zkagent.core.types import ChatMessage, StatusBar

__all__ = ["ContextEngine", "ContextBundle"]


@dataclass
class ContextBundle:
    """一次上下文组装的结果：恒定前缀 + 压缩后的历史。"""

    prefix: list[ChatMessage] = field(default_factory=list)
    history: list[ChatMessage] = field(default_factory=list)
    #: 压缩统计（字符数 / 轮数 / 摘要与工具载荷压缩次数），供 P6 对照与排查。
    stats: dict[str, int] = field(default_factory=dict)

    def messages(self) -> list[ChatMessage]:
        """按「前缀 → 历史」顺序拼出完整上下文。"""
        return [*self.prefix, *self.history]


class ContextEngine:
    """组装每一轮送给模型的上下文。"""

    def __init__(
        self,
        *,
        system_prompt: str,
        fewshots: list[dict] | None = None,
        max_turns: int = 10,
        keep_recent_turns: int = 4,
        max_chars: int = 24000,
        tool_payload_max_chars: int = 1200,
        summarizer: Callable[[str], str] | None = None,
    ) -> None:
        """初始化。

        :param system_prompt: 行业包 ``prompts/system.md`` 的内容。
        :param fewshots: 行业包少样本示例（会被渲染成固定文本，保证前缀恒定）。
        :param max_turns: 回放的历史轮数上限（以 user 消息为轮界）。
        :param keep_recent_turns: 原样保留的最近轮数；更旧的轮次压成摘要。
        :param max_chars: 历史部分的字符预算（中文按 1 字符≈1 token 保守估算）。
        :param tool_payload_max_chars: 单条工具载荷压缩后的字符上限。
        :param summarizer: 可选 LLM 摘要（入参与返回都是摘要文本）；失败自动回退到
                           确定性摘要，不影响本轮回答。
        """
        self._system_prompt = system_prompt
        self._fewshots = list(fewshots or [])
        self._max_turns = max(1, max_turns)
        self._keep_recent_turns = max(1, keep_recent_turns)
        self._max_chars = max(1000, max_chars)
        self._tool_payload_max_chars = max(200, tool_payload_max_chars)
        self._summarizer = summarizer
        self._static_prefix: list[ChatMessage] = self._render_static_prefix()

    # ------------------------------------------------------------------ 前缀

    def _render_static_prefix(self) -> list[ChatMessage]:
        """构造**逐字不变**的静态前缀（系统提示 + 少样本示例）。"""
        parts = [self._system_prompt.strip()]
        if self._fewshots:
            lines = ["## 示例"]
            for example in self._fewshots:
                user = example.get("user", "")
                action = example.get("assistant_action", "")
                note = example.get("note", "")
                lines.append(f"- 用户：{user} → 工具：{action}（{note}）")
            parts.append("\n".join(lines))
        return [ChatMessage(role="system", content="\n\n".join(p for p in parts if p))]

    def static_prefix(self) -> list[ChatMessage]:
        """返回静态前缀（副本，调用方修改不影响内部状态）。"""
        return [message.model_copy(deep=True) for message in self._static_prefix]

    def status_message(self, status: StatusBar) -> ChatMessage:
        """把每轮变化的状态渲染成独立的系统消息（不污染静态前缀）。"""
        pending = "是" if status.pending_confirmation else "否"
        content = (
            "## 当前状态\n"
            f"- 行业包：{status.industry_id}\n"
            f"- 会话：{status.session_id}\n"
            f"- 用户：{status.user_id or '未知'}\n"
            f"- 渠道：{status.channel_id}\n"
            f"- 已执行步数：{status.step}/{status.max_steps}\n"
            f"- 本人身份核验：{'已完成（无需再问尾号，也不得查询他人订单）' if status.verified else '未完成（查个人信息前必须先核验）'}\n"
            f"- 存在待人工确认的动作：{pending}"
        )
        return ChatMessage(role="system", content=content)

    # ------------------------------------------------------------------ 组装

    def build(self, *, status: StatusBar, history: list[ChatMessage]) -> ContextBundle:
        """组装本轮上下文：恒定前缀 + 状态栏 + 压缩后的历史。"""
        prepared, stats = self.prepare_history(history)
        return ContextBundle(
            prefix=[*self.static_prefix(), self.status_message(status)],
            history=prepared,
            stats=stats,
        )

    # ------------------------------------------------------------------ 压缩管线

    def prepare_history(
        self, history: list[ChatMessage]
    ) -> tuple[list[ChatMessage], dict[str, int]]:
        """压缩管线：轨迹隔离 → 工具载荷压缩 → 旧轮摘要 → 硬预算收敛。

        顺序不能反：先摘要会把结构化载荷压成不可解析文本，再压缩就无从判断字段。
        """
        isolated = self._isolate_tool_messages(history)
        chars_before = self._chars(isolated)

        compressed: list[ChatMessage] = []
        compressed_tools = 0
        for message in isolated:
            if message.role != "tool":
                compressed.append(message)
                continue
            content, changed = compress_tool_payload(
                message.content, max_chars=self._tool_payload_max_chars
            )
            if changed:
                compressed_tools += 1
                message = message.model_copy(update={"content": content})
            compressed.append(message)

        keep = min(self._keep_recent_turns, self._max_turns)
        prepared, summarized = self._summarize(compressed, keep)
        # 硬预算：优先牺牲更旧的轮次，最近一轮始终保留；压缩后仍超再按消息收缩
        while self._chars(prepared) > self._max_chars and keep > 1:
            keep -= 1
            prepared, summarized = self._summarize(compressed, keep)
        if self._chars(prepared) > self._max_chars:
            prepared = self._shrink_to_budget(prepared, self._max_chars)

        return prepared, {
            "chars_before": chars_before,
            "chars_after": self._chars(prepared),
            "turns": len(split_turns(compressed)),
            "summarized_turns": summarized,
            "compressed_tool_messages": compressed_tools,
        }

    def _summarize(
        self, messages: list[ChatMessage], keep_recent_turns: int
    ) -> tuple[list[ChatMessage], int]:
        """确定性摘要，可选 LLM 改写；LLM 失败保留确定性结果。"""
        prepared, summarized = summarize_old_turns(
            messages, keep_recent_turns=keep_recent_turns
        )
        if summarized <= 0 or self._summarizer is None or not prepared:
            return prepared, summarized

        first = prepared[0]
        if first.role != "system" or not first.content.startswith(HISTORY_SUMMARY_PREFIX):
            return prepared, summarized
        try:
            better = self._summarizer(first.content)
        except Exception:
            # 摘要失败不能影响本轮回答：保留确定性摘要即可
            better = ""
        if better and better.strip():
            prepared = [first.model_copy(update={"content": better.strip()}), *prepared[1:]]
        return prepared, summarized

    def _shrink_to_budget(
        self, messages: list[ChatMessage], budget: int
    ) -> list[ChatMessage]:
        """最后一道预算：按消息数均分，工具消息仍走 JSON 感知压缩。"""
        if not messages:
            return messages
        share = max(200, budget // len(messages))
        shrunk: list[ChatMessage] = []
        for message in messages:
            if message.role == "tool":
                content, _changed = compress_tool_payload(
                    message.content, max_chars=share, max_items=1, max_str=80
                )
                shrunk.append(message.model_copy(update={"content": content}))
            elif len(message.content) > share:
                shrunk.append(
                    message.model_copy(update={"content": message.content[: share - 1] + "…"})
                )
            else:
                shrunk.append(message)
        return shrunk

    @staticmethod
    def _chars(messages: list[ChatMessage]) -> int:
        """字符口径：正文 + tool_calls 的序列化长度。"""
        total = 0
        for message in messages:
            total += len(message.content)
            if message.tool_calls:
                total += len(json.dumps(message.tool_calls, ensure_ascii=False))
        return total

    def trim_history(self, history: list[ChatMessage]) -> list[ChatMessage]:
        """保留最近 ``max_turns`` 轮，并对工具消息做轨迹隔离。

        「轮」以 ``user`` 消息为界：避免把某一轮的工具结果与其用户输入拆散。
        """
        conversation = self._isolate_tool_messages(history)
        turn_starts = [
            index for index, message in enumerate(conversation) if message.role == "user"
        ]
        if len(turn_starts) <= self._max_turns:
            return conversation
        return conversation[turn_starts[-self._max_turns] :]

    def _isolate_tool_messages(self, history: list[ChatMessage]) -> list[ChatMessage]:
        """轨迹隔离：给工具消息加来源标记，便于模型区分「工具返回」与「用户输入」。

        **本阶段有意不做内容压缩**：截断结构化载荷会使其无法解析，反而制造错误信息
        （曾经因此让 Fake 把成功结果渲染成「非结构化内容」）。压缩与摘要属 P5，
        且必须保证压缩后的内容仍可被解析。
        """
        isolated: list[ChatMessage] = []
        for message in history:
            if message.role != "tool":
                isolated.append(message.model_copy(deep=True))
                continue
            isolated.append(
                ChatMessage(
                    role="tool",
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                    content=f"[工具 {message.name} 返回] {message.content}",
                )
            )
        return isolated
