"""编排结果的**框架无关**映射层。

图状态里含框架消息对象，因此本模块只接受「已归一化」的普通映射（``Mapping``），
把线程标识推导与 ``AgentTurnResult`` 组装这两件事留在 Core 侧，便于单测。

真正的图构造与调用在接缝 ``langgraph_adapter.py``。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zkagent.core.types import AgentTurnResult, TurnStatus

__all__ = ["thread_id_for", "to_turn_result", "VALID_STATUSES"]

#: ``AgentTurnResult.status`` 的合法取值。
VALID_STATUSES: frozenset[str] = frozenset(
    {"completed", "awaiting_confirmation", "denied", "failed", "max_steps"}
)


def thread_id_for(session_id: str) -> str:
    """由会话 id 推导检查点线程 id（进程重启后仍能续跑的关键）。"""
    if not session_id or not session_id.strip():
        raise ValueError("session_id 不能为空")
    return f"session:{session_id.strip()}"


def to_turn_result(
    state: Mapping[str, Any],
    *,
    trace_id: str | None = None,
    fallback_text: str = "",
) -> AgentTurnResult:
    """把图输出的归一化字典映射为 :class:`AgentTurnResult`。

    ``status`` 非法或缺失时降级为 ``failed``——**不猜测成功**。
    """
    raw_status = str(state.get("status") or "")
    pending = state.get("pending_confirmation")
    if not raw_status and isinstance(pending, Mapping):
        # 被 interrupt 挂起时不会走到 respond，图输出里没有 status。
        # 这不是「成功」，而是明确的「等待人工审批」——必须如实映射。
        raw_status = "awaiting_confirmation"
    status: TurnStatus = (  # type: ignore[assignment]
        raw_status if raw_status in VALID_STATUSES else "failed"
    )

    calls = state.get("tool_calls") or []
    tool_calls: list[dict[str, Any]] = [dict(call) for call in calls if isinstance(call, Mapping)]

    final_text = str(state.get("final_text") or fallback_text)
    if status == "awaiting_confirmation" and not final_text and isinstance(pending, Mapping):
        final_text = (
            f"该操作需要人工审批（{pending.get('decision')}）：{pending.get('reason')}。"
            f"审批单号 {pending.get('approval_id')}，批准后才会执行。"
        )

    if status == "completed" and not final_text:
        # 完成但没有内容属于异常输出，不得伪装成成功
        status = "failed"
        final_text = final_text or "本轮未产生可回复内容。"

    return AgentTurnResult(
        final_text=final_text,
        status=status,
        tool_calls=tool_calls,
        pending_confirmation=dict(pending) if isinstance(pending, Mapping) else None,
        trace_id=trace_id or state.get("trace_id"),
        usage=dict(state.get("usage") or {}),
        latency_ms=state.get("latency_ms"),
        policy=list((state.get("policy") or {}).get("decisions") or []),
    )
