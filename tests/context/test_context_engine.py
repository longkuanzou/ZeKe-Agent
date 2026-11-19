"""ContextEngine：静态前缀恒定、状态栏独立、轨迹隔离与按轮裁剪。

这些性质直接决定「Prompt Cache 能否命中」与「长对话是否稳定」，
因此用断言固化下来（P5 的压缩策略必须继续满足它们）。
"""

from __future__ import annotations

from zkagent.core.context.engine import ContextEngine
from zkagent.core.types import ChatMessage, StatusBar


def _engine(**kwargs) -> ContextEngine:
    return ContextEngine(
        system_prompt="你是客服助手。\n\n## 纪律\n如实回答。",
        fewshots=[{"user": "问题", "assistant_action": "knowledge_search", "note": "先检索"}],
        **kwargs,
    )


def _status(step: int = 0) -> StatusBar:
    return StatusBar(
        industry_id="appliance", session_id="s1", user_id="U1", channel_id="cli", step=step
    )


def test_static_prefix_is_byte_identical_across_turns() -> None:
    """前缀必须逐字不变，否则 Prompt Cache 会失效。"""
    engine = _engine()
    first = engine.static_prefix()
    second = engine.static_prefix()
    assert [m.model_dump() for m in first] == [m.model_dump() for m in second]
    assert first[0].role == "system"
    assert "客服助手" in first[0].content
    assert "示例" in first[0].content, "少样本示例应进入静态前缀"


def test_status_bar_does_not_pollute_prefix() -> None:
    """验证：状态栏单独成一条消息，静态前缀逐字不变（Prompt Cache 才可能命中）。"""
    engine = _engine()
    prefix = engine.static_prefix()

    bundle = engine.build(status=_status(step=3), history=[])

    assert len(bundle.prefix) == len(prefix) + 1
    assert bundle.prefix[0].model_dump() == prefix[0].model_dump(), "静态前缀被状态污染了"
    status_message = bundle.prefix[-1]
    assert "已执行步数：3" in status_message.content
    assert "存在待人工确认的动作：否" in status_message.content


def test_status_message_reflects_pending_confirmation() -> None:
    """验证：有挂起动作时状态栏显式写「是」，让模型知道当前处于等待审批。"""
    status = _status().model_copy(update={"pending_confirmation": True})
    engine = _engine()
    assert "存在待人工确认的动作：是" in engine.status_message(status).content


def test_trims_history_by_user_turns() -> None:
    """验证：``max_turns`` 按**用户轮**裁剪，保留最近 N 轮的完整对话。"""
    engine = _engine(max_turns=2)
    history = [
        ChatMessage(role="user", content="第1问"),
        ChatMessage(role="assistant", content="第1答"),
        ChatMessage(role="user", content="第2问"),
        ChatMessage(role="assistant", content="第2答"),
        ChatMessage(role="user", content="第3问"),
        ChatMessage(role="assistant", content="第3答"),
    ]
    trimmed = engine.trim_history(history)
    assert [m.content for m in trimmed] == ["第2问", "第2答", "第3问", "第3答"]


def test_tool_messages_are_marked_and_stay_parseable() -> None:
    """轨迹隔离：工具消息加来源标记、配对信息保留，且**载荷仍可解析**。

    这里刻意断言「可解析」：曾经为了省上下文把 JSON 截断，结果下游把成功结果
    误读成「非结构化内容」，属于比浪费 token 更严重的问题。
    """
    import json

    engine = _engine()
    payload = {"ok": True, "data": {"order_id": "A1001", "amount_cents": 699900}}
    history = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[{"name": "get_order", "args": {"order_id": "A1001"}, "id": "call-1"}],
        ),
        ChatMessage(
            role="tool",
            name="get_order",
            tool_call_id="call-1",
            content=json.dumps(payload, ensure_ascii=False),
        ),
    ]
    trimmed = engine.trim_history(history)

    tool_message = trimmed[-1]
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.name == "get_order"
    assert tool_message.content.startswith("[工具 get_order 返回]")
    assert json.loads(tool_message.content.split("] ", 1)[1]) == payload, "载荷必须完整可解析"
    assert trimmed[0].tool_calls[0]["id"] == "call-1", "配对信息必须保留"


def test_build_orders_prefix_before_history() -> None:
    """验证：消息顺序固定为「静态前缀 → 状态栏 → 历史」，顺序错会同时破坏缓存与语义。"""
    engine = _engine()
    bundle = engine.build(
        status=_status(),
        history=[
            ChatMessage(role="user", content="历史问"),
            ChatMessage(role="assistant", content="历史答"),
        ],
    )
    messages = bundle.messages()
    assert messages[0].role == "system"
    assert messages[1].role == "system"  # 状态栏紧随静态前缀
    assert messages[2].content == "历史问"
    assert messages[-1].content == "历史答"


def test_empty_history_is_allowed() -> None:
    """验证：首轮（无历史）也能正常构建，前缀恰为两条（静态 + 状态）。"""
    engine = _engine()
    bundle = engine.build(status=_status(), history=[])
    assert bundle.history == []
    assert len(bundle.prefix) == 2
