"""CliChannel：能力声明、卡片降级、诚实渲染与输入边界。"""

from __future__ import annotations

import io

import pytest

from zkagent.channels.cli import CliChannel
from zkagent.core.types import AgentTurnResult, OutboundMessage


def _channel(text: str = "") -> tuple[CliChannel, io.StringIO]:
    out = io.StringIO()
    channel = CliChannel(
        session_id="s1", user_id="U1", input_stream=io.StringIO(text), output_stream=out
    )
    return channel, out


def test_capabilities_declare_no_card_support() -> None:
    """CLI 不支持卡片 —— 这是能力声明，不是猜测。"""
    channel, _ = _channel()
    assert channel.channel_id == "cli"
    assert channel.capabilities["card"] is False
    assert channel.capabilities["async_callback"] is False


def test_receive_returns_normalized_message() -> None:
    """验证：CLI 输入被归一化成 ``InboundMessage``（去行尾、补齐 session / user、标注渠道）。"""
    channel, _ = _channel("查订单 A1001\n")
    inbound = channel.receive()
    assert inbound.text == "查订单 A1001"
    assert inbound.session_id == "s1"
    assert inbound.user_id == "U1"
    assert inbound.raw == {"channel": "cli"}


def test_receive_raises_eof_on_exit_and_empty_input() -> None:
    """验证：``exit`` 与输入结束都以 ``EOFError`` 收场——上层据此退出循环，不必特判字符串。"""
    channel, _ = _channel("exit\n")
    with pytest.raises(EOFError):
        channel.receive()

    empty, _ = _channel("")
    with pytest.raises(EOFError):
        empty.receive()


def test_card_is_degraded_to_text_when_unsupported() -> None:
    """验证：渠道不支持卡片时降级为纯文本（标题 + 字段逐行铺开），不能把结构化内容整段丢掉。"""
    channel, out = _channel()
    channel.send(
        OutboundMessage(
            session_id="s1",
            text="订单详情：",
            cards=[{"title": "订单 A1001", "fields": {"状态": "已签收", "金额": "6999.00"}}],
        )
    )
    rendered = out.getvalue()
    assert "订单详情：" in rendered
    assert "订单 A1001" in rendered
    assert "状态: 已签收" in rendered, "不支持卡片时必须降级为纯文本"


def test_render_turn_shows_status_and_tool_summary() -> None:
    """验证：渲染一轮结果时带上状态与工具摘要，**失败的工具也必须可见**，否则用户以为全成功。"""
    channel, _ = _channel()
    result = AgentTurnResult(
        final_text="已查到订单",
        status="completed",
        tool_calls=[{"name": "get_order", "ok": True, "error_code": None},
                    {"name": "get_logistics", "ok": False, "error_code": "NOT_FOUND"}],
    )
    rendered = channel.render_turn(result)
    assert "已查到订单" in rendered
    assert "[status=completed]" in rendered
    assert "get_order=ok" in rendered
    assert "get_logistics=NOT_FOUND" in rendered, "工具失败必须在输出中可见"


def test_render_turn_marks_awaiting_confirmation_explicitly() -> None:
    """挂起 ≠ 完成：必须显式标注，不能被当成办理成功。"""
    channel, _ = _channel()
    result = AgentTurnResult(final_text="需人工确认", status="awaiting_confirmation")
    rendered = channel.render_turn(result)
    assert "[status=awaiting_confirmation]" in rendered
    assert "等待人工审批" in rendered
    assert "已成功" not in rendered


def test_send_marks_awaiting_confirmation_flag() -> None:
    """验证：``awaiting_confirmation`` 的出站消息显式提示「需要人工确认」，不渲染成普通回复。"""
    channel, out = _channel()
    channel.send(OutboundMessage(session_id="s1", text="挂起中", awaiting_confirmation=True))
    assert "需要人工确认" in out.getvalue()


def test_channel_instances_do_not_share_capabilities() -> None:
    """验证：``capabilities`` 是实例级字典——改一个实例不影响同类实例与类级默认值。"""
    first, _ = _channel()
    second, _ = _channel()
    first.capabilities["card"] = True
    assert second.capabilities["card"] is False
    assert CliChannel._DEFAULT_CAPABILITIES["card"] is False
