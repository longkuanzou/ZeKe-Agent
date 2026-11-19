"""P5 上下文压缩：可解析、配对、幂等、可回退。

这些断言直接对应 P5 Gate 的第 ① 条（两种压缩策略均有单测），并守住 P2 踩过的坑：
截断工具 JSON 会让下游把成功结果读成「非结构化内容」。
"""

from __future__ import annotations

import json

from zkagent.core.context.compress import (
    HISTORY_SUMMARY_PREFIX,
    compress_tool_payload,
    summarize_old_turns,
)
from zkagent.core.context.engine import ContextEngine
from zkagent.core.types import ChatMessage, StatusBar


def _status() -> StatusBar:
    return StatusBar(
        industry_id="homeapp", session_id="s1", user_id="H1", channel_id="cli", step=1
    )


def _engine(**kwargs) -> ContextEngine:
    return ContextEngine(system_prompt="你是客服助手。", **kwargs)


def _turn(index: int, *, payload_chars: int = 0) -> list[ChatMessage]:
    """一轮：用户提问 → 模型调工具 → 工具返回 → 模型回答。"""
    body = {"ok": True, "order_id": f"H26{index:02d}", "status": "delivered"}
    if payload_chars:
        body["chunks"] = [{"text": "x" * payload_chars, "source": f"doc-{i}"} for i in range(6)]
    return [
        ChatMessage(role="user", content=f"第{index}问：订单到哪了"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "name": "get_order",
                    "args": {"order_id": f"H26{index:02d}"},
                    "id": f"call-{index}",
                }
            ],
        ),
        ChatMessage(
            role="tool",
            name="get_order",
            tool_call_id=f"call-{index}",
            content=json.dumps(body, ensure_ascii=False),
        ),
        ChatMessage(role="assistant", content=f"第{index}答：已签收"),
    ]


def _strip_prefix(content: str) -> str:
    return content.split("] ", 1)[1] if content.startswith("[工具 ") and "] " in content else content


def test_payload_compression_keeps_json_and_critical_fields() -> None:
    """验证：JSON 载荷压缩后**仍是合法 JSON**，且关键字段（``ok`` / ``count``）一个不少。

    长列表用 ``_items`` + ``_omitted`` 表达省略，而不是直接截断字符串——
    P2 踩过的坑就是截断 JSON 让下游把「成功」读成「非结构化内容」。
    """
    payload = {
        "ok": True,
        "count": 6,
        "note": "共 6 条",
        "chunks": [{"text": "x" * 400, "source": f"doc-{i}"} for i in range(6)],
    }
    compressed, changed = compress_tool_payload(
        json.dumps(payload, ensure_ascii=False), max_chars=600
    )

    assert changed is True
    data = json.loads(compressed)
    assert data["ok"] is True
    assert data["count"] == 6
    assert data["chunks"]["_omitted"] == 4
    assert len(data["chunks"]["_items"]) == 2


def test_payload_compression_is_idempotent() -> None:
    """验证：已压缩过的载荷再压一次不再变化（``changed=False``）。

    每轮准备上下文都会过一遍压缩，若不幂等，同一段内容会被反复削弱、结果不可复现。
    """
    payload = {"ok": True, "chunks": [{"text": "x" * 500}] * 5}
    once, _ = compress_tool_payload(json.dumps(payload), max_chars=400)
    twice, changed = compress_tool_payload(once, max_chars=400)
    assert changed is False
    assert twice == once


def test_non_json_is_truncated_with_explicit_marker() -> None:
    """验证：非 JSON 文本只能按字符截断，且必须以 ``…`` 明示被截断。

    静默截断比不截断更危险——模型会把半句话当成完整结论。
    """
    compressed, changed = compress_tool_payload("x" * 5000, max_chars=200)
    assert changed is True
    assert len(compressed) <= 200
    assert compressed.endswith("…")


def test_old_turns_summarized_and_recent_kept() -> None:
    """验证：超出 ``keep_recent_turns`` 的旧轮次**整轮**压成一条 system 摘要，最近两轮原样保留。

    摘要里要留住「用户问了什么 + 调了什么工具 + 成败」，这是后续推理的最小信息集。
    """
    history = [message for index in range(1, 7) for message in _turn(index)]

    summarized, count = summarize_old_turns(history, keep_recent_turns=2, max_chars=800)

    assert count == 4
    assert summarized[0].role == "system"
    assert summarized[0].content.startswith(HISTORY_SUMMARY_PREFIX)
    assert "第1问" in summarized[0].content
    assert "get_order=ok" in summarized[0].content
    # 最近两轮原样保留
    assert [m.content for m in summarized if m.role == "user"] == [
        "第5问：订单到哪了",
        "第6问：订单到哪了",
    ]


def test_pairing_is_preserved_after_summary() -> None:
    """验证：压缩后每条 ``tool`` 消息仍有前置的 assistant ``tool_calls``。

    这对配对一旦被破坏，下一次模型调用会被 OpenAI 兼容接口直接 400 拒掉。
    """
    history = [message for index in range(1, 8) for message in _turn(index)]
    prepared = _engine(keep_recent_turns=3).build(status=_status(), history=history).history

    seen_calls: set[str] = set()
    for message in prepared:
        if message.role == "assistant":
            seen_calls.update(str(call.get("id")) for call in message.tool_calls)
        if message.role == "tool":
            assert message.tool_call_id in seen_calls, "tool 消息缺少前置 tool_calls"


def test_llm_summary_failure_falls_back_to_deterministic() -> None:
    """验证：LLM 摘要抛异常时回退到确定性摘要，本轮回答不受影响。

    摘要属可选增强——它挂了不能拖垮对话，所以只捕获异常、不向上抛。
    """
    def boom(_: str) -> str:
        raise RuntimeError("模型超时")

    history = [message for index in range(1, 7) for message in _turn(index)]
    prepared = _engine(keep_recent_turns=2, summarizer=boom).build(
        status=_status(), history=history
    ).history

    assert prepared[0].role == "system"
    assert prepared[0].content.startswith(HISTORY_SUMMARY_PREFIX)
    assert "用户：" in prepared[0].content


def test_llm_summary_replaces_when_available() -> None:
    """验证：LLM 摘要返回文本时**替换**确定性摘要，而不是追加一条新的 system 消息。

    替换保证前缀里始终只有一条摘要，否则越压越长、缓存也命中不了。
    """
    history = [message for index in range(1, 7) for message in _turn(index)]
    prepared = _engine(
        keep_recent_turns=2, summarizer=lambda _: "[历史摘要] 更短的摘要"
    ).build(status=_status(), history=history).history

    assert prepared[0].content == "[历史摘要] 更短的摘要"


def test_budget_keeps_tool_payload_parseable() -> None:
    """验证：硬预算收缩到 1000 字符时，工具载荷依然能被 ``json.loads`` 解析，且工具消息不被整条删除。

    收缩的优先级是「删旧轮次 → 压载荷」，不能为了省字数把 JSON 拦腰截断。
    """
    history = [
        ChatMessage(role="user", content="问" * 3000),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[{"name": "search_products", "args": {}, "id": "call-big"}],
        ),
        ChatMessage(
            role="tool",
            name="search_products",
            tool_call_id="call-big",
            content=json.dumps(
                {"ok": True, "items": [{"name": "y" * 500} for _ in range(20)]},
                ensure_ascii=False,
            ),
        ),
    ]
    bundle = _engine(max_chars=1000, keep_recent_turns=1).build(
        status=_status(), history=history
    )

    tool_messages = [m for m in bundle.history if m.role == "tool"]
    assert tool_messages, "工具消息不能被整条删掉"
    payload = json.loads(_strip_prefix(tool_messages[0].content))
    assert payload["ok"] is True
    assert bundle.stats["chars_after"] <= bundle.stats["chars_before"]


def test_stats_report_compression_effect() -> None:
    """验证：``bundle.stats`` 如实报告摘要轮数与压缩条数，且字符数确实下降。

    P6 的面板与 trace 直接消费这组数字，报错了会让人误判「压缩没生效」。
    """
    history = [message for index in range(1, 8) for message in _turn(index, payload_chars=500)]
    bundle = _engine(keep_recent_turns=2, tool_payload_max_chars=400).build(
        status=_status(), history=history
    )

    assert bundle.stats["summarized_turns"] == 5
    assert bundle.stats["compressed_tool_messages"] > 0
    assert bundle.stats["chars_after"] < bundle.stats["chars_before"]


def test_knowledge_search_text_is_compressed_to_160_chars() -> None:
    """M6：``knowledge_search`` 的整块原文必须被 P5 压到 160 字符以内。"""
    payload = {
        "ok": True,
        "count": 1,
        "chunks": [
            {
                "source": "after_sales/policy/warranty.md",
                "score": 0.91,
                "text": "整块知识原文" * 300,
            }
        ],
    }
    raw = "[工具 knowledge_search 返回] " + json.dumps(payload, ensure_ascii=False)
    compressed, changed = compress_tool_payload(raw, max_chars=1200)

    assert changed is True
    body = compressed.split("] ", 1)[1]
    data = json.loads(body)
    assert data["ok"] is True
    assert len(data["chunks"][0]["text"]) <= 160
    assert data["chunks"][0]["source"] == "after_sales/policy/warranty.md"
