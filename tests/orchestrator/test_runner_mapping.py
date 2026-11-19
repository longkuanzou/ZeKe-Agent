"""编排结果的框架无关映射（``runner.py``）——不依赖任何框架。"""

from __future__ import annotations

import pytest

from zkagent.core.orchestrator.runner import thread_id_for, to_turn_result


def test_thread_id_is_derived_from_session_and_namespaced() -> None:
    """验证：thread_id 由 session 推导并加 ``session:`` 前缀（去空白），不同会话必不同。

    前缀隔离是为了将来接入别的命名空间时不撞车。
    """
    assert thread_id_for("s1") == "session:s1"
    assert thread_id_for("  s1  ") == "session:s1"
    assert thread_id_for("s1") != thread_id_for("s2")


@pytest.mark.parametrize("bad", ["", "   "])
def test_thread_id_rejects_blank_session(bad: str) -> None:
    """验证：空会话 id 直接抛错——空 id 会让所有会话共用同一个 checkpoint。"""
    with pytest.raises(ValueError):
        thread_id_for(bad)


def test_maps_completed_state() -> None:
    """验证：正常完成的图状态被映射成 ``AgentTurnResult``（含 tool_calls 与 trace_id）。"""
    result = to_turn_result(
        {
            "status": "completed",
            "final_text": "好了",
            "tool_calls": [{"name": "get_order", "ok": True}],
        },
        trace_id="t1",
    )
    assert result.status == "completed"
    assert result.final_text == "好了"
    assert result.tool_calls == [{"name": "get_order", "ok": True}]
    assert result.trace_id == "t1"


def test_unknown_status_degrades_to_failed() -> None:
    """状态非法时降级为 failed —— 绝不猜测成功。"""
    result = to_turn_result({"status": "something-new", "final_text": "x"})
    assert result.status == "failed"


def test_missing_status_degrades_to_failed() -> None:
    """验证：图状态缺 ``status`` 字段时降级为 failed（与上一条同理，不得猜成功）。"""
    assert to_turn_result({"final_text": "x"}).status == "failed"


def test_completed_without_text_is_not_success() -> None:
    """「完成」但没有内容属于异常输出，不得伪装成成功。"""
    result = to_turn_result({"status": "completed", "final_text": ""})
    assert result.status == "failed"
    assert result.final_text


def test_fallback_text_is_used_when_state_has_none() -> None:
    """验证：没有终态文本时用兜底文案——用户至少能看到一句话，而不是空白。"""
    result = to_turn_result({"status": "failed", "tool_calls": []}, fallback_text="兜底文案")
    assert result.final_text == "兜底文案"


def test_awaiting_confirmation_keeps_pending_payload() -> None:
    """验证：挂起态透传 ``pending_confirmation``（含 approval_id）——上层要靠它恢复。"""
    result = to_turn_result(
        {
            "status": "awaiting_confirmation",
            "final_text": "需要人工确认",
            "pending_confirmation": {"approval_id": "AP1"},
        }
    )
    assert result.status == "awaiting_confirmation"
    assert result.pending_confirmation == {"approval_id": "AP1"}


def test_max_steps_status_is_preserved() -> None:
    """验证：``max_steps`` 是**合法**终态，不被降级成 failed——超步要能被告知，而非伪装失败。"""
    assert to_turn_result({"status": "max_steps", "final_text": "超步"}).status == "max_steps"
