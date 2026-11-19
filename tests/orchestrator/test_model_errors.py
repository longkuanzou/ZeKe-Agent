"""M5：供应商 SDK 异常必须变成可读的 ``ProviderCallError``，而不是裸栈。"""

from __future__ import annotations

import pytest

from zkagent.core.llm import ProviderCallError
from zkagent.core.orchestrator import langgraph_adapter
from zkagent.core.types import ChatModelHandle


class _BoomModel:
    """模拟 429 / 网络中断：抛供应商风格的异常。"""

    def invoke(self, *_args, **_kwargs):
        raise RuntimeError("429 Too Many Requests")

    def stream(self, *_args, **_kwargs):
        raise RuntimeError("connection reset by peer")


def _handle() -> ChatModelHandle:
    return ChatModelHandle(provider_id="deepseek", model_id="deepseek-chat")


def test_chat_once_wraps_provider_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证：供应商 SDK 抛的 429 被包装成 ``ProviderCallError``，且保留原文关键词。

    防裸栈穿透到用户对话里——错误必须可读、可分类（``MODEL_CALL_FAILED``）。
    """
    monkeypatch.setattr(langgraph_adapter, "resolve_chat_model", lambda _handle: _BoomModel())

    with pytest.raises(ProviderCallError) as excinfo:
        langgraph_adapter.chat_once(_handle(), [{"role": "user", "content": "你好"}])

    assert "429" in str(excinfo.value)
    assert excinfo.value.code == "MODEL_CALL_FAILED"


def test_stream_chat_wraps_provider_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证：流式调用的网络中断同样被包装，不因走 stream 分支而漏掉。"""
    monkeypatch.setattr(langgraph_adapter, "resolve_chat_model", lambda _handle: _BoomModel())

    with pytest.raises(ProviderCallError) as excinfo:
        list(langgraph_adapter.stream_chat(_handle(), [{"role": "user", "content": "你好"}]))

    assert "connection reset" in str(excinfo.value)
