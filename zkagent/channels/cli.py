"""CLI 渠道：同步拉取式输入 + 纯文本输出。

能力声明为 ``card=False``：因此渲染时**必须**降级为纯文本——Core 与渠道都不得
假设所有渠道都支持结构化卡片（DESIGN §5.3）。
"""

from __future__ import annotations

import sys
from typing import IO, Any

from zkagent.core.interfaces import ChannelAdapter
from zkagent.core.types import AgentTurnResult, InboundMessage, OutboundMessage

__all__ = ["CliChannel"]


class CliChannel(ChannelAdapter):
    """命令行渠道实现（拉取式）。"""

    channel_id = "cli"
    _DEFAULT_CAPABILITIES = {"card": False, "typing": False, "async_callback": False}

    def __init__(
        self,
        *,
        session_id: str = "cli-session",
        user_id: str | None = None,
        input_stream: IO[str] | None = None,
        output_stream: IO[str] | None = None,
    ) -> None:
        """初始化。

        :param input_stream: 默认 ``sys.stdin``；测试可注入 ``io.StringIO``。
        :param output_stream: 默认 ``sys.stdout``。
        """
        super().__init__(self.channel_id)
        self.session_id = session_id
        self.user_id = user_id
        self._in = input_stream if input_stream is not None else sys.stdin
        self._out = output_stream if output_stream is not None else sys.stdout

    # ------------------------------------------------------------------ 收发

    def receive(self) -> InboundMessage:
        """阻塞读取一行输入；EOF 或 ``exit`` / ``quit`` 时抛出 ``EOFError`` 以结束会话。"""
        line = self._in.readline()
        if not line:
            raise EOFError("输入已结束")
        text = line.strip()
        if text in {"exit", "quit"}:
            raise EOFError("用户主动结束")
        return InboundMessage(
            session_id=self.session_id,
            text=text,
            user_id=self.user_id,
            raw={"channel": self.channel_id},
        )

    def send(self, msg: OutboundMessage) -> None:
        """输出到标准输出；本渠道不支持卡片，卡片内容降级为纯文本。"""
        self._out.write(self.render(msg) + "\n")
        self._out.flush()

    # ------------------------------------------------------------------ 渲染

    def render(self, msg: OutboundMessage) -> str:
        """按 ``capabilities`` 渲染出站消息。"""
        parts = [msg.text]
        if msg.cards and not self.capabilities.get("card", False):
            for card in msg.cards:
                parts.append(self._render_card_as_text(card))
        if msg.awaiting_confirmation:
            parts.append("（该操作需要人工确认，已挂起等待审批）")
        return "\n".join(part for part in parts if part)

    def render_turn(self, result: AgentTurnResult) -> str:
        """把一轮编排结果渲染为文本；如实呈现失败与挂起，不改写成成功。"""
        lines = [result.final_text or "（本轮无输出）"]
        lines.append(f"[status={result.status}]")
        if result.tool_calls:
            summary = ", ".join(
                f"{call.get('name')}={'ok' if call.get('ok') else call.get('error_code')}"
                for call in result.tool_calls
            )
            lines.append(f"[工具] {summary}")
        if result.status == "awaiting_confirmation":
            lines.append("[提示] 已挂起，等待人工审批后继续（HITL 在 P4 实现）")
        return "\n".join(lines)

    @staticmethod
    def _render_card_as_text(card: dict[str, Any]) -> str:
        """把卡片降级成「键: 值」多行文本。"""
        title = card.get("title")
        fields = card.get("fields") or {}
        lines = [str(title)] if title else []
        lines.extend(f"{key}: {value}" for key, value in fields.items())
        return "\n".join(lines)
