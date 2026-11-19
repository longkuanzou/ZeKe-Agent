"""工具执行器：**唯一**允许做幂等、重试与超时的地方。

设计要点（DESIGN §7.3）：

- 调用点**不得自行重试**，一律经 :meth:`ToolExecutor.call`；
- 幂等键三种情形：命中且参数一致 → 返回历史结果（``IDEMPOTENT_REPLAY``）；
  命中但参数不一致 → **拒绝执行**（``IDEMPOTENCY_KEY_CONFLICT``）；
  写操作未提供幂等键 → **拒绝**（``INVALID_ARGUMENT``），不得静默降级为一次性执行；
- 重试三档：读操作遇限流 / 上游故障可自动退避重试；写操作**仅在提供幂等键时**才允许；
  其余错误码（含 ``TIMEOUT``）一律不自动重试；
- 超时采用真实超时（工作线程 + ``future.result(timeout=...)``）。注意：超时后**底层调用可能仍在执行**，
  因此 ``TIMEOUT`` 不允许盲目重试，应由上层先查询业务状态。
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable, Protocol, runtime_checkable

from zkagent.core.interfaces import BusinessBackend
from zkagent.core.observability.tracer import Tracer
from zkagent.core.tools.schema import ErrorCode, is_retryable, make_error, make_ok
from zkagent.core.types import ToolContext, ToolResult, ToolSpec

__all__ = [
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "ToolExecutor",
    "canonical_json",
    "request_fingerprint",
]


# --------------------------------------------------------------------- 幂等存储


@runtime_checkable
class IdempotencyStore(Protocol):
    """幂等记录存储协议（Core 定义契约，具体持久化由实现方决定）。"""

    def get(self, key: str) -> tuple[str, dict[str, Any]] | None:
        """返回 ``(request_hash, result_dict)``；键不存在时返回 ``None``。"""

    def put(self, key: str, tool_name: str, request_hash: str, result: dict[str, Any]) -> None:
        """写入一条幂等记录（键已存在时不得覆盖）。"""


class InMemoryIdempotencyStore:
    """内存实现：仅用于单元测试；生产与跨进程验证请用 SQLite 实现。"""

    def __init__(self) -> None:
        self._records: dict[str, tuple[str, str, dict[str, Any]]] = {}

    def get(self, key: str) -> tuple[str, dict[str, Any]] | None:
        """取幂等记录。

        :param key: 幂等键（写工具必填）。
        :returns: ``(request_hash, result_dict)``；键不存在返回 ``None``。
        """
        record = self._records.get(key)
        return (record[1], record[2]) if record is not None else None

    def put(self, key: str, tool_name: str, request_hash: str, result: dict[str, Any]) -> None:
        """写入幂等记录，**已存在的键不覆盖**（先到者为准）。

        :param key: 幂等键。
        :param tool_name: 工具名，仅用于排查。
        :param request_hash: 入参指纹，用于识别「同键不同参」的冲突。
        :param result: 首次成功的结果，重放时原样返回。
        """
        self._records.setdefault(key, (tool_name, request_hash, result))


# --------------------------------------------------------------------- 指纹


def _strip_empty(value: Any) -> Any:
    """剔除「等价空」字段，保证同一语义的参数产生同一指纹。

    **映射**中的 ``None`` / ``""`` / ``[]`` / ``{}`` 会被剔除（未传参 == 传空值）；
    **列表元素不会被删除**——列表位置有语义（例如退货商品清单），
    删元素会把 ``[None, 2]`` 误判为 ``[2]``，属于危险的等价化。
    """
    if isinstance(value, dict):
        return {
            key: _strip_empty(item)
            for key, item in value.items()
            if item is not None and item != "" and item != [] and item != {}
        }
    if isinstance(value, list):
        return [_strip_empty(item) for item in value]
    return value


def canonical_json(payload: Any) -> str:
    """规范化 JSON：键排序、去等价空值、紧凑分隔符（用于指纹与幂等比对）。"""
    return json.dumps(
        _strip_empty(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def request_fingerprint(tool_name: str, args: dict[str, Any]) -> str:
    """计算「工具名 + 规范化参数」的 SHA-256 指纹。"""
    material = f"{tool_name}:{canonical_json(args)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- 执行器


class ToolExecutor:
    """工具执行入口：幂等、超时、重试与错误码归一的唯一实现。"""

    def __init__(
        self,
        backend: BusinessBackend,
        idem_store: IdempotencyStore,
        tracer: Tracer | None = None,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        backoff_base_s: float = 1.0,
        backoff_factor: float = 2.0,
    ) -> None:
        """初始化。

        :param sleeper: 睡眠函数，测试时注入空实现以避免真实等待。
        :param max_retries: 退避重试的最大次数（1s / 2s / 4s 对应 base=1、factor=2）。
        """
        self._backend = backend
        self._store = idem_store
        self._tracer = tracer
        self._sleeper = sleeper
        self._max_retries = max(0, max_retries)
        self._backoff_base_s = backoff_base_s
        self._backoff_factor = backoff_factor
        # 共享线程池（**不使用** with 语句）：超时必须立刻返回，不能等慢调用自己结束
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool-exec")

    def close(self) -> None:
        """释放线程池（进程退出或测试清理时调用）。"""
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ 公开入口

    def call(self, spec: ToolSpec, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行一次工具调用（内含幂等判定、超时与退避重试）。"""
        payload = dict(args or {})
        fingerprint = request_fingerprint(spec.name, payload)
        key = ctx.idempotency_key

        # 1) 写操作必须提供幂等键 —— 不得静默降级
        if spec.requires_idempotency_key and not key:
            return self._fail(
                spec, ctx, ErrorCode.INVALID_ARGUMENT,
                f"{spec.name} 属写操作，必须提供幂等键（ctx.idempotency_key）",
            )

        # 2) 幂等命中判定
        if key:
            replay = self._check_replay(spec, ctx, key, fingerprint)
            if replay is not None:
                return replay

        # 3) 执行 + 退避重试
        result = self._execute_with_retry(spec, payload, ctx)

        # 4) 成功结果写入幂等表（失败不写：避免失败被永久固化，后续修复后仍无法重试）
        if key and result.ok:
            self._store.put(key, spec.name, fingerprint, result.model_dump())

        return result

    # ------------------------------------------------------------------ 内部实现

    def _check_replay(
        self,
        spec: ToolSpec,
        ctx: ToolContext,
        key: str,
        fingerprint: str,
    ) -> ToolResult | None:
        """幂等命中检查；未命中返回 ``None``。"""
        record = self._store.get(key)
        if record is None:
            return None
        stored_hash, stored_result = record
        if stored_hash != fingerprint:
            return self._fail(
                spec, ctx, ErrorCode.IDEMPOTENCY_KEY_CONFLICT,
                f"幂等键 {key} 已用于不同参数的调用，拒绝执行",
            )
        replayed = ToolResult.model_validate(stored_result)
        replayed.error_code = ErrorCode.IDEMPOTENT_REPLAY.value
        replayed.trace_id = ctx.trace_id
        self._record(ctx, "tool_idempotent_replay", tool=spec.name, idem_key=key)
        return replayed

    def _execute_with_retry(
        self,
        spec: ToolSpec,
        payload: dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        """按重试矩阵执行；只有 :func:`is_retryable` 判定允许时才退避重试。"""
        attempt = 0
        result: ToolResult | None = None
        while True:
            self._record(
                ctx, "tool_call_start", tool=spec.name, attempt=attempt,
                args=self._safe_args(payload),
            )
            result = self._invoke_once(spec, payload, ctx)
            self._record(
                ctx, "tool_call_end", tool=spec.name, attempt=attempt,
                ok=result.ok, error_code=result.error_code, latency_ms=result.latency_ms,
            )
            if result.ok:
                return result
            allowed = is_retryable(
                result.error_code, spec=spec, has_idempotency_key=bool(ctx.idempotency_key)
            )
            if not allowed or attempt >= self._max_retries:
                return result
            delay = self._backoff_base_s * (self._backoff_factor**attempt)
            self._record(
                ctx, "tool_retry_scheduled", tool=spec.name, attempt=attempt, delay_s=delay
            )
            self._sleeper(delay)
            attempt += 1

    def _invoke_once(
        self,
        spec: ToolSpec,
        payload: dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        """单次调用后端，并施加 ``ToolSpec.timeout_s`` 超时。"""
        started = time.perf_counter()
        future = self._pool.submit(self._backend.invoke, spec.name, payload, ctx)
        try:
            result = future.result(timeout=spec.timeout_s)
        except FutureTimeout:
            # 注意：工作线程可能仍在写数据，因此 TIMEOUT 绝不允许盲目重试；
            # 这里立即返回，不等待慢调用结束（因此也无需 cancel 成功）。
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return make_error(
                ErrorCode.TIMEOUT,
                f"{spec.name} 调用超过 {spec.timeout_s}s 未返回",
                latency_ms=elapsed_ms,
                trace_id=ctx.trace_id,
            )
        if result.trace_id is None:
            result.trace_id = ctx.trace_id
        return result

    def _fail(
        self,
        spec: ToolSpec,
        ctx: ToolContext,
        code: ErrorCode,
        message: str,
    ) -> ToolResult:
        """构造失败结果并记录 trace（不进入重试路径）。"""
        result = make_error(code, message, trace_id=ctx.trace_id)
        self._record(ctx, "tool_rejected", tool=spec.name, error_code=code.value, message=message)
        return result

    def _record(self, ctx: ToolContext, event: str, **fields: Any) -> None:
        """写一条 trace 事件；未配置 tracer 时静默跳过（不因可观测性缺失而失败）。

        :param ctx: 工具上下文，提供 ``trace_id``。
        :param event: 事件名（如 ``tool_call_end`` / ``tool_rejected``）。
        :param fields: 附加字段，键名命中敏感词时由 Tracer 脱敏。
        """
        if self._tracer is not None:
            self._tracer.record(ctx.trace_id, event, **fields)

    @staticmethod
    def _safe_args(payload: dict[str, Any]) -> dict[str, Any]:
        """trace 中记录入参（Tracer 会按敏感键名脱敏）。"""
        return payload


def build_success_result(data: Any, ctx: ToolContext, message: str | None = None) -> ToolResult:
    """辅助：构造成功结果（供实现方复用，保证 ``trace_id`` 不遗漏）。"""
    return make_ok(data, message=message, trace_id=ctx.trace_id)
