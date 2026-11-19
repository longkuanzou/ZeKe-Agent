"""第二业务后端：通过 HTTP 调用字段风格不同的 legacy 业务系统。

**为什么需要它**：MockBackend 与 HttpBackend 共用同一套归一化 ``ToolSpec``，
Core 只看到 ``ToolResult``；字段方言（``code/msg/data``）与错误码映射都在本文件内完成。
换后端时 ``zkagent/core/`` 不应有任何改动。
"""

from __future__ import annotations

from typing import Any

import httpx

from zkagent.backends.specs import TOOL_SPECS
from zkagent.core.interfaces import BusinessBackend
from zkagent.core.tools.schema import ErrorCode, make_error, make_ok
from zkagent.core.types import ToolContext, ToolResult, ToolSpec

__all__ = ["HttpBackend", "LEGACY_CODE_TO_ERROR"]

#: 归一化错误码 → legacy 整数码（legacy 系统不使用字符串枚举）。
LEGACY_CODE_TO_ERROR: dict[int, ErrorCode] = {
    1001: ErrorCode.INVALID_ARGUMENT,
    1002: ErrorCode.NOT_FOUND,
    1003: ErrorCode.PERMISSION_DENIED,
    1004: ErrorCode.CONFLICT_STATE,
    1005: ErrorCode.UPSTREAM_ERROR,
    1006: ErrorCode.IDEMPOTENCY_KEY_CONFLICT,
    1007: ErrorCode.RATE_LIMITED,
    1008: ErrorCode.TIMEOUT,
    1009: ErrorCode.IDEMPOTENT_REPLAY,
    1010: ErrorCode.UNKNOWN,
}
#: 反向映射：本后端只依赖 ErrorCode，不把 legacy 整数码泄漏给 Core。
ERROR_TO_LEGACY_CODE: dict[str, int] = {
    code.value: number for number, code in LEGACY_CODE_TO_ERROR.items()
}


class HttpBackend(BusinessBackend):
    """把归一化工具调用转发到 legacy HTTP 服务。"""

    backend_id = "http"

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout_s)
        self._owns_client = client is None

    def close(self) -> None:
        """释放连接池。

        只有在客户端是**自己创建**的时候才关闭；外部注入的 ``httpx.Client``
        由调用方负责生命周期，这里不能替别人关。
        """
        if self._owns_client:
            self._client.close()

    def list_tools(self) -> list[ToolSpec]:
        """返回本后端能处理的工具声明（深拷贝，调用方改不坏单例）。

        :returns: :data:`zkagent.backends.specs.TOOL_SPECS` 的逐项副本。
        """
        return [spec.model_copy(deep=True) for spec in TOOL_SPECS]

    def invoke(self, tool_name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """调用 legacy ``/legacy/invoke``，把 ``{code,msg,data}`` 翻译成 ``ToolResult``。"""
        payload = {
            "tool": tool_name,
            "params": dict(args or {}),
            "actor": ctx.model_dump(mode="json"),
        }
        try:
            response = self._client.post("/legacy/invoke", json=payload)
        except (httpx.HTTPError, OSError) as exc:
            return make_error(
                ErrorCode.UPSTREAM_ERROR,
                f"legacy 业务后端不可用：{type(exc).__name__}: {exc}",
                trace_id=ctx.trace_id,
            )

        if response.status_code >= 400:
            return make_error(
                ErrorCode.UPSTREAM_ERROR,
                f"legacy 业务后端返回 HTTP {response.status_code}",
                trace_id=ctx.trace_id,
            )
        try:
            body = response.json()
        except ValueError:
            return make_error(
                ErrorCode.UPSTREAM_ERROR,
                "legacy 业务后端返回了非 JSON 响应",
                trace_id=ctx.trace_id,
            )
        if not isinstance(body, dict):
            return make_error(
                ErrorCode.UPSTREAM_ERROR,
                "legacy 业务后端响应格式非法",
                trace_id=ctx.trace_id,
            )

        code = body.get("code")
        message = str(body.get("msg") or "")
        data = body.get("data")
        if code in (0, "0", None) and body.get("data") is not None:
            return make_ok(data, message=message or None, trace_id=ctx.trace_id)
        if code in (0, "0") and body.get("data") is None:
            # 成功但无数据（例如 escalate_to_human 的某些实现）：仍按成功返回。
            return make_ok({}, message=message or None, trace_id=ctx.trace_id)

        error = LEGACY_CODE_TO_ERROR.get(int(code) if isinstance(code, int) else -1)
        if error is None:
            error = ErrorCode.UPSTREAM_ERROR
        return make_error(
            error,
            message or f"legacy 业务后端返回错误码 {code}",
            data=data,
            trace_id=ctx.trace_id,
        )
