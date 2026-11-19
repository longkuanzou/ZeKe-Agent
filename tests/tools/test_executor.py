"""ToolExecutor 的幂等三情形、重试三档矩阵与超时行为。"""

from __future__ import annotations

import time
from typing import Any

import pytest

from zkagent.core.interfaces import BusinessBackend
from zkagent.core.tools.executor import (
    InMemoryIdempotencyStore,
    ToolExecutor,
    canonical_json,
    request_fingerprint,
)
from zkagent.core.tools.schema import ErrorCode, make_error, make_ok
from zkagent.core.types import ToolContext, ToolResult, ToolSpec

READ_SPEC = ToolSpec(name="get_order", description="查询订单", tags=["order"], timeout_s=5.0)
WRITE_SPEC = ToolSpec(
    name="create_refund_ticket",
    description="创建退款单",
    tags=["refund", "write"],
    requires_idempotency_key=True,
    timeout_s=5.0,
)


class _StubBackend(BusinessBackend):
    """按「第几次调用」返回预设结果的替身后端。"""

    backend_id = "stub"

    def __init__(
        self,
        results: list[ToolResult] | None = None,
        handler: Any = None,
    ) -> None:
        self._results = list(results or [])
        self._handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[ToolSpec]:
        return []

    def invoke(self, tool_name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append((tool_name, dict(args)))
        if self._handler is not None:
            return self._handler(tool_name, args, ctx)
        if not self._results:
            return make_ok({"n": len(self.calls)})
        index = min(len(self.calls) - 1, len(self._results) - 1)
        return self._results[index].model_copy(deep=True)


def _executor(backend: BusinessBackend, store=None, sleeper=None) -> ToolExecutor:
    return ToolExecutor(
        backend,
        store or InMemoryIdempotencyStore(),
        sleeper=sleeper or (lambda _s: None),
        max_retries=3,
        backoff_base_s=0.01,
    )


# --------------------------------------------------------------------- 指纹


def test_canonical_json_strips_empty_map_entries_and_sorts_keys() -> None:
    """验证：规范化 JSON 排序键、剥掉空值，但**列表元素保位**。

    列表里的 null 不能删——删了会把 ``[None, 2]`` 与 ``[2]`` 误判成同一个请求。
    """
    assert canonical_json({"b": 1, "a": None, "c": [], "d": "", "e": {}}) == '{"b":1}'
    # 列表元素保位：删元素会把 [None, 2] 与 [2] 误判为同一请求
    assert canonical_json({"b": [None, 2], "a": 1}) == '{"a":1,"b":[null,2]}'
    assert canonical_json({"items": [{"x": 1, "y": None}]}) == '{"items":[{"x":1}]}'


def test_fingerprint_is_stable_and_tool_specific() -> None:
    """验证：同一工具同一参数指纹稳定（空值不影响），不同工具名即使参数相同指纹也不同。

    指纹是幂等与 HITL 审批的共同地基：HITL 用它防「审批后改参数」。
    """
    assert request_fingerprint("t", {"a": 1, "b": None}) == request_fingerprint("t", {"a": 1})
    assert request_fingerprint("t", {"a": 1}) != request_fingerprint("u", {"a": 1})


# --------------------------------------------------------------------- 幂等三情形


def test_write_without_idempotency_key_is_rejected(ctx: ToolContext) -> None:
    """验证：写工具缺幂等键直接拒绝，且**不调用后端**。

    防「静默降级为一次性执行」——那会在重试时产生重复扣款。
    """
    backend = _StubBackend()
    result = _executor(backend).call(WRITE_SPEC, {"order_id": "A1001"}, ctx)

    assert result.ok is False
    assert result.error_code == ErrorCode.INVALID_ARGUMENT.value
    assert backend.calls == [], "缺少幂等键时不得调用后端（更不得静默降级为一次性执行）"


def test_same_key_same_args_returns_history_without_reexecuting(ctx: ToolContext) -> None:
    """验证：同键同参重放返回首次结果（``IDEMPOTENT_REPLAY``）且不重复执行。

    换执行器实例仍命中同一份记录，模拟进程重启后依然幂等。
    """
    backend = _StubBackend([make_ok({"refund_id": "R1"})])
    store = InMemoryIdempotencyStore()
    args = {"order_id": "A1001", "amount_cents": 100}
    ctx = ctx.model_copy(update={"idempotency_key": "idem-1"})

    first = _executor(backend, store).call(WRITE_SPEC, args, ctx)
    second = _executor(backend, store).call(WRITE_SPEC, args, ctx)

    assert first.ok and first.error_code is None
    assert second.ok and second.error_code == ErrorCode.IDEMPOTENT_REPLAY.value
    assert second.data == first.data
    # 换一个执行器实例（模拟进程重启）仍命中同一份落库记录
    assert len(backend.calls) == 1


def test_same_key_different_args_is_rejected(ctx: ToolContext) -> None:
    """验证：同键不同参视为冲突（``IDEMPOTENCY_KEY_CONFLICT``），后端只被调用一次。

    防「复用同一个键提交另一笔金额」这种越权改写。
    """
    backend = _StubBackend([make_ok({"refund_id": "R1"})])
    store = InMemoryIdempotencyStore()
    ctx = ctx.model_copy(update={"idempotency_key": "idem-2"})
    executor = _executor(backend, store)

    executor.call(WRITE_SPEC, {"order_id": "A1001", "amount_cents": 100}, ctx)
    conflict = executor.call(WRITE_SPEC, {"order_id": "A1001", "amount_cents": 999}, ctx)

    assert conflict.ok is False
    assert conflict.error_code == ErrorCode.IDEMPOTENCY_KEY_CONFLICT.value
    assert len(backend.calls) == 1


def test_failed_result_is_not_cached_so_retry_stays_possible(ctx: ToolContext) -> None:
    """验证：失败结果不进幂等表，同键重试仍可成功。

    只固化成功结果，否则一次上游抖动就把这个键永久钉死在失败上。
    """
    backend = _StubBackend([
        make_error(ErrorCode.NOT_FOUND, "订单不存在"),
        make_ok({"refund_id": "R2"}),
    ])
    store = InMemoryIdempotencyStore()
    ctx = ctx.model_copy(update={"idempotency_key": "idem-3"})
    executor = _executor(backend, store)

    failed = executor.call(WRITE_SPEC, {"order_id": "A1001"}, ctx)
    succeeded = executor.call(WRITE_SPEC, {"order_id": "A1001"}, ctx)

    assert failed.ok is False
    assert succeeded.ok is True, "失败结果不应被幂等表固化"


# --------------------------------------------------------------------- 重试三档


def test_read_tool_retries_on_rate_limit_then_succeeds(ctx: ToolContext) -> None:
    """验证：限流与上游抖动都属可重试档，第三次成功即返回成功。"""
    backend = _StubBackend([
        make_error(ErrorCode.RATE_LIMITED, "限流"),
        make_error(ErrorCode.UPSTREAM_ERROR, "上游抖动"),
        make_ok({"order_id": "A1001"}),
    ])
    result = _executor(backend).call(READ_SPEC, {"order_id": "A1001"}, ctx)

    assert result.ok is True
    assert len(backend.calls) == 3


def test_retries_are_exhausted_and_last_error_returned(ctx: ToolContext) -> None:
    """验证：重试耗尽后返回**最后一次**的错误码，且总调用数为「首次 + max_retries」。"""
    backend = _StubBackend([make_error(ErrorCode.UPSTREAM_ERROR, "上游故障")])
    result = _executor(backend).call(READ_SPEC, {"order_id": "A1001"}, ctx)

    assert result.ok is False
    assert result.error_code == ErrorCode.UPSTREAM_ERROR.value
    assert len(backend.calls) == 4, "max_retries=3 表示首次 + 3 次重试"


def test_non_retryable_error_is_not_retried(ctx: ToolContext) -> None:
    """验证：不可重试错误（``NOT_FOUND``）只调一次——重试也只会得到同样结果。"""
    backend = _StubBackend([make_error(ErrorCode.NOT_FOUND, "订单不存在")])
    result = _executor(backend).call(READ_SPEC, {"order_id": "A1001"}, ctx)

    assert result.error_code == ErrorCode.NOT_FOUND.value
    assert len(backend.calls) == 1


def test_write_tool_retries_only_with_idempotency_key(ctx: ToolContext) -> None:
    """验证：写工具**带幂等键**才允许重试——无键重试会造成重复写。"""
    backend = _StubBackend([
        make_error(ErrorCode.UPSTREAM_ERROR, "上游抖动"),
        make_ok({"refund_id": "R9"}),
    ])
    keyed = ctx.model_copy(update={"idempotency_key": "idem-4"})
    result = _executor(backend).call(WRITE_SPEC, {"order_id": "A1001"}, keyed)

    assert result.ok is True
    assert len(backend.calls) == 2


def test_sleep_is_used_between_retries(ctx: ToolContext) -> None:
    """验证：重试间隔走指数退避（1s / 2s / 4s），sleep 由注入的 ``sleeper`` 记录。"""
    delays: list[float] = []
    backend = _StubBackend([make_error(ErrorCode.RATE_LIMITED, "限流")])
    executor = ToolExecutor(
        backend,
        InMemoryIdempotencyStore(),
        sleeper=delays.append,
        max_retries=2,
        backoff_base_s=1.0,
    )
    executor.call(READ_SPEC, {}, ctx)

    assert delays == [1.0, 2.0], "退避应为指数增长（1s / 2s / 4s）"


def test_timeout_returns_timeout_without_retrying(ctx: ToolContext) -> None:
    """验证：超时立即返回 ``TIMEOUT``（不等慢调用结束），且**绝不自动重试**。

    超时重试最危险：可能第一次其实已成功落库，再试一次就是重复扣款。
    """
    def slow(_name: str, _args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
        time.sleep(1.0)
        return make_ok({"late": True})

    backend = _StubBackend(handler=slow)
    spec = READ_SPEC.model_copy(update={"timeout_s": 0.05})

    executor = _executor(backend)
    started = time.perf_counter()
    result = executor.call(spec, {"order_id": "A1001"}, ctx)
    elapsed = time.perf_counter() - started
    executor.close()

    assert result.ok is False
    assert result.error_code == ErrorCode.TIMEOUT.value
    assert elapsed < 0.6, "超时必须立即返回，不得等待慢调用自己结束"
    assert len(backend.calls) == 1, "TIMEOUT 绝不允许自动重试"


def test_success_result_carries_trace_id(ctx: ToolContext) -> None:
    """验证：成功结果带回 ``trace_id``，保证每次工具调用都能追到那一次会话。"""
    backend = _StubBackend([make_ok({"ok": True})])
    result = _executor(backend).call(READ_SPEC, {}, ctx)
    assert result.trace_id == ctx.trace_id


@pytest.mark.parametrize("code", [ErrorCode.PERMISSION_DENIED, ErrorCode.CONFLICT_STATE])
def test_permission_and_state_conflicts_are_terminal(ctx: ToolContext, code: ErrorCode) -> None:
    """验证：``PERMISSION_DENIED`` 与 ``CONFLICT_STATE`` 都是终态错误，不重试。

    权限不会在几毫秒内变有，状态冲突也不会自己解开——重试只是浪费额度。
    """
    backend = _StubBackend([make_error(code, "拒绝")])
    result = _executor(backend).call(READ_SPEC, {}, ctx)
    assert result.error_code == code.value
    assert len(backend.calls) == 1
