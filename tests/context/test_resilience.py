"""P5 韧性：压缩/摘要失败时降级，而不是让本轮回答崩掉。

熔断、重试与超时已分别由 ``tests/orchestrator`` 与 ``tests/tools/test_executor.py``
覆盖；这里只补「上下文管线自身的故障」。
"""

from __future__ import annotations

import json

from zkagent.core.context.compress import compress_tool_payload
from zkagent.core.context.engine import ContextEngine
from zkagent.core.types import ChatMessage, StatusBar


def _status() -> StatusBar:
    return StatusBar(
        industry_id="homeapp", session_id="s1", user_id="H1", channel_id="cli", step=1
    )


def _history(turns: int) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for index in range(turns):
        messages.extend(
            [
                ChatMessage(role="user", content=f"第{index}问"),
                ChatMessage(role="assistant", content=f"第{index}答"),
            ]
        )
    return messages


def test_oversized_llm_summary_is_still_bounded() -> None:
    """LLM 返回超长摘要时，仍要服从字符预算。"""
    engine = ContextEngine(
        system_prompt="你是客服助手。",
        max_chars=1000,
        keep_recent_turns=1,
        summarizer=lambda _: "摘要" * 5000,
    )

    bundle = engine.build(status=_status(), history=_history(6))

    assert bundle.stats["chars_after"] <= 1000
    assert bundle.history, "不能为了压预算把历史全部丢掉"


def test_broken_payload_does_not_raise() -> None:
    """非 JSON / 截断的载荷只能被安全截断，绝不能抛异常穿透编排层。"""
    for value in ("{not-json" * 100, "\x00\x01" * 200, "x" * 5000):
        compressed, changed = compress_tool_payload(f"[工具 t 返回] {value}", max_chars=100)
        assert isinstance(compressed, str)
        assert changed is True
        assert len(compressed) <= 120


def test_tool_payload_stays_json_after_repeated_compression() -> None:
    """验证：连续两次压缩后仍是合法 JSON——重复准备上下文不会把载荷压坏。"""
    payload = json.dumps({"ok": True, "items": [{"text": "x" * 400}] * 10}, ensure_ascii=False)
    once, _ = compress_tool_payload(payload, max_chars=400)
    twice, _ = compress_tool_payload(once, max_chars=400)
    assert json.loads(twice)["ok"] is True
