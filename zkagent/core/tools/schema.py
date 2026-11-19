"""统一错误码取值域与工具元数据校验辅助。

``ToolResult.error_code`` 只能取 :class:`ErrorCode` 中的值——禁止裸字符串，
否则重试策略与失败归因都无法可靠判定。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from zkagent.core.types import ToolResult, ToolSpec

__all__ = [
    "ErrorCode",
    "RETRYABLE_CODES",
    "APPROVAL_CODES",
    "is_write_tool",
    "validate_error_code",
    "is_retryable",
    "make_error",
    "make_ok",
]


class ErrorCode(StrEnum):
    """统一错误码（DESIGN §7.3 取值域 + §7.7.8 审批域扩展）。"""

    # —— 通用工具错误 ——
    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    CONFLICT_STATE = "CONFLICT_STATE"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    UNKNOWN = "UNKNOWN"

    # —— 审批域（P4 落地逻辑；此处先固定取值，避免各处自造字符串）——
    APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
    APPROVAL_ALREADY_RESOLVED = "APPROVAL_ALREADY_RESOLVED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_ALREADY_CONSUMED = "APPROVAL_ALREADY_CONSUMED"
    APPROVAL_PARAMS_CHANGED = "APPROVAL_PARAMS_CHANGED"
    APPROVER_UNAUTHORIZED = "APPROVER_UNAUTHORIZED"
    SELF_APPROVAL_FORBIDDEN = "SELF_APPROVAL_FORBIDDEN"


#: 允许退避重试的错误码（是否真正重试还要看操作类型，见 ``is_retryable``）。
RETRYABLE_CODES: frozenset[ErrorCode] = frozenset(
    {ErrorCode.RATE_LIMITED, ErrorCode.UPSTREAM_ERROR}
)

#: 审批域错误码集合（便于调用方区分「审批问题」与「业务问题」）。
APPROVAL_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.APPROVAL_NOT_FOUND,
        ErrorCode.APPROVAL_ALREADY_RESOLVED,
        ErrorCode.APPROVAL_EXPIRED,
        ErrorCode.APPROVAL_ALREADY_CONSUMED,
        ErrorCode.APPROVAL_PARAMS_CHANGED,
        ErrorCode.APPROVER_UNAUTHORIZED,
        ErrorCode.SELF_APPROVAL_FORBIDDEN,
    }
)


def is_write_tool(spec: ToolSpec) -> bool:
    """判断工具是否为写操作。

    判定依据是 ``requires_idempotency_key``——它同时决定了
    「重试前提是提供幂等键」与「必须写审计」两条规则。
    """
    return bool(spec.requires_idempotency_key)


def validate_error_code(value: str | None) -> str | None:
    """校验错误码是否在取值域内；不在则抛 ``ValueError``（开发期尽早暴露）。"""
    if value is None:
        return None
    if value not in ErrorCode.__members__ and value not in {c.value for c in ErrorCode}:
        raise ValueError(f"未知错误码：{value!r}（取值域见 ErrorCode）")
    return value


def is_retryable(
    error_code: str | None,
    *,
    spec: ToolSpec,
    has_idempotency_key: bool,
) -> bool:
    """按「重试三档矩阵」判断本次失败是否允许自动重试。

    - 可自动重试：读操作遇 ``RATE_LIMITED`` / ``UPSTREAM_ERROR``；
    - 需退避重试：**写操作**且提供幂等键时，同样只允许上面两个错误码；
    - 绝不自动重试：``TIMEOUT`` / ``PERMISSION_DENIED`` / ``INVALID_ARGUMENT`` /
      ``CONFLICT_STATE`` / ``NOT_FOUND``，以及**任何无幂等键的写操作**。
    """
    if error_code is None:
        return False
    try:
        code = ErrorCode(error_code)
    except ValueError:
        return False
    if code not in RETRYABLE_CODES:
        return False
    if is_write_tool(spec) and not has_idempotency_key:
        return False
    return True


def make_error(
    code: ErrorCode,
    message: str,
    *,
    data: Any | None = None,
    latency_ms: int = 0,
    trace_id: str | None = None,
) -> ToolResult:
    """构造失败结果（``ok=False``）；错误码必须是 :class:`ErrorCode`。"""
    return ToolResult(
        ok=False,
        data=data,
        error_code=code.value,
        message=message,
        latency_ms=latency_ms,
        trace_id=trace_id,
    )


def make_ok(
    data: Any,
    *,
    message: str | None = None,
    latency_ms: int = 0,
    trace_id: str | None = None,
    error_code: ErrorCode | None = None,
) -> ToolResult:
    """构造成功结果；幂等重放属于「成功但带标记」，用 ``error_code`` 承载该标记。"""
    return ToolResult(
        ok=True,
        data=data,
        error_code=error_code.value if error_code else None,
        message=message,
        latency_ms=latency_ms,
        trace_id=trace_id,
    )
