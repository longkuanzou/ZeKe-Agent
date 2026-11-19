"""P7 HttpBackend：方言翻译、错误码与失败降级。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.legacy_api import create_app
from zkagent.backends.http import HttpBackend
from zkagent.core.tools.schema import ErrorCode
from zkagent.core.types import ToolContext


def _ctx() -> ToolContext:
    return ToolContext(trace_id="http-test", session_id="s1", user_id="U1", channel_id="http")


def _backend(tmp_path: Path) -> HttpBackend:
    app = create_app(tmp_path / "legacy.db")
    return HttpBackend("http://legacy.test", client=TestClient(app))


def test_http_backend_translates_success(tmp_path: Path) -> None:
    """验证：legacy 的 ``{code,msg,data}`` 成功响应被翻译成 ``ToolResult.ok=True`` 且 data 原样保留。"""
    backend = _backend(tmp_path)
    result = backend.invoke("get_order", {"order_id": "A1001"}, _ctx())

    assert result.ok is True
    assert result.data["order_id"] == "A1001"
    assert result.data["user_id"] == "U1"


def test_http_backend_translates_business_error(tmp_path: Path) -> None:
    """验证：legacy 整数错误码（1002）被映射回归一化 ``NOT_FOUND``——Core 只看字符串枚举。"""
    backend = _backend(tmp_path)
    result = backend.invoke("get_order", {"order_id": "NOPE"}, _ctx())

    assert result.ok is False
    assert result.error_code == ErrorCode.NOT_FOUND.value


def test_http_backend_maps_non_2xx_to_upstream(tmp_path: Path) -> None:
    """验证：HTTP 503 归为 ``UPSTREAM_ERROR``（可重试类），而不是当成业务失败。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="maintenance")

    client = httpx.Client(base_url="http://legacy.test", transport=httpx.MockTransport(handler))
    backend = HttpBackend("http://legacy.test", client=client)
    result = backend.invoke("get_order", {"order_id": "A1001"}, _ctx())

    assert result.ok is False
    assert result.error_code == ErrorCode.UPSTREAM_ERROR.value


def test_http_backend_maps_bad_json_to_upstream(tmp_path: Path) -> None:
    """验证：200 但响应体不是合法 JSON 时也报 ``UPSTREAM_ERROR``。

    只看状态码会把「返回了 HTML 错误页」当成成功，解析阶段才炸。
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="{not-json")

    client = httpx.Client(base_url="http://legacy.test", transport=httpx.MockTransport(handler))
    backend = HttpBackend("http://legacy.test", client=client)
    result = backend.invoke("list_orders", {}, _ctx())

    assert result.ok is False
    assert result.error_code == ErrorCode.UPSTREAM_ERROR.value


def test_http_backend_does_not_raise_on_transport_error(tmp_path: Path) -> None:
    """验证：连接失败被收敛成 ``ToolResult``，**不向编排层抛异常**。

    后端挂了应该让模型说「暂时查不了」，而不是打断整轮对话。
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(base_url="http://legacy.test", transport=httpx.MockTransport(handler))
    backend = HttpBackend("http://legacy.test", client=client)
    result = backend.invoke("get_order", {"order_id": "A1001"}, _ctx())

    assert result.ok is False
    assert result.error_code == ErrorCode.UPSTREAM_ERROR.value
