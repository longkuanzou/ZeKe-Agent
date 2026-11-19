"""API 渠道：推送式（由 FastAPI 请求驱动），支持结构化卡片。"""

from __future__ import annotations

from typing import Any

from zkagent.core.interfaces import ChannelAdapter
from zkagent.core.types import AgentTurnResult, InboundMessage, OutboundMessage

__all__ = ["ApiChannel"]


class ApiChannel(ChannelAdapter):
    """HTTP / 未来 IM 的推送式渠道。

    ``receive()`` 不成立：消息由 ``POST /chat`` 归一化后交给 AgentRunner。
    ``send()`` 只收集出站消息，供测试或上层网关消费。
    """

    channel_id = "api"
    _DEFAULT_CAPABILITIES = {"card": True, "typing": False, "async_callback": True}

    def __init__(self, *, session_id: str = "api-session", user_id: str | None = None) -> None:
        super().__init__(self.channel_id)
        self.session_id = session_id
        self.user_id = user_id
        self.sent: list[OutboundMessage] = []

    def receive(self) -> InboundMessage:
        """API 渠道是推送式，不支持主动拉取。

        :raises NotImplementedError: 总是抛出——请求由 ``POST /chat`` 驱动，
            而不是由后端轮询。
        """
        raise NotImplementedError("API 渠道是推送式，不使用 receive()；请调用 POST /chat")

    def send(self, msg: OutboundMessage) -> None:
        """记录一条出站消息（供测试与调试查看，不真正发送）。

        :param msg: 待发送的消息。
        """
        self.sent.append(msg)

    @staticmethod
    def payload(result: AgentTurnResult) -> dict[str, Any]:
        """把一轮结果转成 API 响应（不把内部对象直接泄漏给调用方）。"""
        return {
            "status": result.status,
            "text": result.final_text,
            "awaiting_confirmation": result.status == "awaiting_confirmation",
            "pending_confirmation": result.pending_confirmation,
            "tool_calls": result.tool_calls,
            "policy": result.policy,
            "trace_id": result.trace_id,
            "usage": result.usage,
            "latency_ms": result.latency_ms,
        }
