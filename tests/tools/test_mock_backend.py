"""MockBackend 的工具行为：每个工具覆盖「正常 / 异常 / 幂等」三类。

设计要点：
- 归属与状态校验**必须在服务端完成**，因此越权与状态冲突的用例是重点；
- 幂等由 ``ToolExecutor`` 负责，故幂等用例通过执行器验证；
  同时额外验证「绕过执行器时数据库层也不会重复落库」这一兜底防护。
"""

from __future__ import annotations

import json
import re

import pytest

from zkagent.backends.mock.backend import MockBackend
from zkagent.backends.mock.db import Database
from zkagent.core.tools.executor import ToolExecutor
from zkagent.core.tools.schema import ErrorCode
from zkagent.core.types import ToolContext, ToolResult

OWNER = "U1"
OTHER_USER = "U2"


def _invoke(backend: MockBackend, name: str, ctx: ToolContext, **args) -> ToolResult:
    return backend.invoke(name, args, ctx)


def _executor(backend: MockBackend, store) -> ToolExecutor:
    return ToolExecutor(backend, store, sleeper=lambda _s: None, max_retries=0)


def _audit_rows(db: Database) -> list[dict]:
    return db.rows("audit_logs")


# --------------------------------------------------------------------- 读工具


def test_get_order_ok(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：读订单正常返回，且结果带 ``trace_id``（每次调用可追踪）。"""
    result = _invoke(backend, "get_order", ctx, order_id="A1001")
    assert result.ok is True
    assert result.data["order_id"] == "A1001"
    assert result.data["amount_cents"] == 699900
    assert result.trace_id == ctx.trace_id


def test_get_order_missing_is_reported_truthfully(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：查无此单如实报 ``NOT_FOUND``——不得用别的订单或默认值顶替。"""
    result = _invoke(backend, "get_order", ctx, order_id="A9999")
    assert result.ok is False
    assert result.error_code == ErrorCode.NOT_FOUND.value


def test_get_order_of_other_user_is_denied(backend: MockBackend, ctx: ToolContext) -> None:
    """A1004 属于 U2，U1 不得读取。"""
    result = _invoke(backend, "get_order", ctx, order_id="A1004")
    assert result.ok is False
    assert result.error_code == ErrorCode.PERMISSION_DENIED.value


def test_get_order_without_identity_is_denied(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：``user_id`` 为空（匿名）时读订单被拒——归属校验不能因身份缺失而跳过。"""
    anonymous = ctx.model_copy(update={"user_id": None})
    result = _invoke(backend, "get_order", anonymous, order_id="A1001")
    assert result.error_code == ErrorCode.PERMISSION_DENIED.value


def test_get_order_missing_argument(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：必填参数缺失返回 ``INVALID_ARGUMENT``，而不是拿第一条订单糊弄过去。"""
    result = _invoke(backend, "get_order", ctx)
    assert result.error_code == ErrorCode.INVALID_ARGUMENT.value


def test_get_user_ok_and_denied(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：``get_user`` 三态——本人可读、他人 ``PERMISSION_DENIED``、不存在 ``NOT_FOUND``。

    越权与「查无此人」必须是不同错误码，否则模型无法区分该道歉还是该引导。
    """
    ok = _invoke(backend, "get_user", ctx, user_id=OWNER)
    assert ok.ok and ok.data["user_id"] == OWNER
    denied = _invoke(backend, "get_user", ctx, user_id=OTHER_USER)
    assert denied.error_code == ErrorCode.PERMISSION_DENIED.value
    missing = _invoke(backend, "get_user", ctx, user_id="U9")
    assert missing.error_code == ErrorCode.NOT_FOUND.value


def test_verify_user_ok_and_wrong_factor(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：核验三态——因子正确通过、因子错误拒绝、**替他人核验**也拒绝。"""
    ok = _invoke(backend, "verify_user", ctx, user_id=OWNER, factor="1234")
    assert ok.ok and ok.data["verified"] is True
    bad = _invoke(backend, "verify_user", ctx, user_id=OWNER, factor="0000")
    assert bad.ok is False and bad.error_code == ErrorCode.PERMISSION_DENIED.value
    cross = _invoke(backend, "verify_user", ctx, user_id=OTHER_USER, factor="5678")
    assert cross.error_code == ErrorCode.PERMISSION_DENIED.value


def test_list_orders_filters_and_denies_other_user(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：列表按状态过滤生效、未知状态被拒、查他人列表被拒。"""
    all_orders = _invoke(backend, "list_orders", ctx, user_id=OWNER)
    assert all_orders.ok and all_orders.data["count"] == 5

    delivered = _invoke(backend, "list_orders", ctx, user_id=OWNER, status="delivered")
    assert delivered.data["count"] == 3

    bad_status = _invoke(backend, "list_orders", ctx, user_id=OWNER, status="unknown")
    assert bad_status.error_code == ErrorCode.INVALID_ARGUMENT.value

    denied = _invoke(backend, "list_orders", ctx, user_id=OTHER_USER)
    assert denied.error_code == ErrorCode.PERMISSION_DENIED.value


def test_get_logistics_ok_and_no_record(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：物流三态——有轨迹正常返回、未发货如实报 ``NOT_FOUND``、他人订单被拒。"""
    ok = _invoke(backend, "get_logistics", ctx, order_id="A1001")
    assert ok.ok and ok.data["carrier"]
    # A1002 未发货，没有物流记录 —— 必须如实告知，不得编造轨迹
    missing = _invoke(backend, "get_logistics", ctx, order_id="A1002")
    assert missing.ok is False and missing.error_code == ErrorCode.NOT_FOUND.value
    denied = _invoke(backend, "get_logistics", ctx, order_id="A1004")
    assert denied.error_code == ErrorCode.PERMISSION_DENIED.value


# --------------------------------------------------------------------- 写工具（服务端校验）


def test_cancel_order_ok_and_writes_audit(
    backend: MockBackend, business_db: Database, ctx: ToolContext
) -> None:
    """验证：取消订单落库并写审计（含 before/after 与 actor）——写操作必须可追溯。"""
    result = _invoke(backend, "cancel_order", ctx, order_id="A1002", reason="不想要了")
    assert result.ok is True
    assert result.data["status"] == "cancelled"

    orders = {row["order_id"]: row for row in business_db.rows("orders")}
    assert orders["A1002"]["status"] == "cancelled"

    audits = [row for row in _audit_rows(business_db) if row["action"] == "cancel_order"]
    assert len(audits) == 1
    before = json.loads(audits[0]["before_json"])
    after = json.loads(audits[0]["after_json"])
    assert before["status"] == "created" and after["status"] == "cancelled"
    assert audits[0]["actor"] == "user"


def test_cancel_delivered_order_conflicts(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：已签收订单不可取消，返回 ``CONFLICT_STATE``——状态机在服务端守，不靠提示词。"""
    result = _invoke(backend, "cancel_order", ctx, order_id="A1001", reason="想取消")
    assert result.error_code == ErrorCode.CONFLICT_STATE.value


def test_cancel_other_users_order_is_denied_without_side_effect(
    backend: MockBackend, business_db: Database, ctx: ToolContext
) -> None:
    """验证：取消他人订单被拒，且库内状态与审计都没变化——拒绝必须无副作用。"""
    result = _invoke(backend, "cancel_order", ctx, order_id="A1004", reason="越权尝试")
    assert result.error_code == ErrorCode.PERMISSION_DENIED.value

    orders = {row["order_id"]: row for row in business_db.rows("orders")}
    assert orders["A1004"]["status"] == "delivered", "越权被拒后不得产生任何业务写"
    assert _audit_rows(business_db) == [], "被拒绝的调用不应留下“已执行”的审计"


def test_create_refund_ticket_ok(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：退款单创建后状态为 ``pending_confirmation``，金额与传入一致（不打折、不取整）。"""
    result = _invoke(
        backend, "create_refund_ticket", ctx, order_id="A1003", amount_cents=9900, reason="不想要了"
    )
    assert result.ok is True
    assert result.data["status"] == "pending_confirmation"
    assert result.data["amount_cents"] == 9900


def test_create_refund_on_unshipped_order_conflicts(backend: MockBackend, ctx: ToolContext) -> None:
    """A1002 未发货：不满足创建退款单的数据条件（引导话术由 PolicyEngine 决定）。"""
    result = _invoke(
        backend, "create_refund_ticket", ctx, order_id="A1002", amount_cents=100, reason="退款"
    )
    assert result.ok is False
    assert result.error_code == ErrorCode.CONFLICT_STATE.value


def test_create_refund_amount_exceeding_order_is_rejected(
    backend: MockBackend, ctx: ToolContext
) -> None:
    """验证：退款金额超过订单金额被拒——不允许「退得比付的多」。"""
    result = _invoke(
        backend, "create_refund_ticket", ctx, order_id="A1003", amount_cents=999999, reason="超额"
    )
    assert result.error_code == ErrorCode.INVALID_ARGUMENT.value


def test_create_refund_on_other_users_order_is_denied(
    backend: MockBackend, business_db: Database, ctx: ToolContext
) -> None:
    """验证：给他人订单建退款单被拒（``PERMISSION_DENIED``），不落任何退款记录。"""
    result = _invoke(
        backend, "create_refund_ticket", ctx, order_id="A1004", amount_cents=100, reason="越权"
    )
    assert result.error_code == ErrorCode.PERMISSION_DENIED.value
    assert business_db.count("refund_requests") == 0


def test_refund_ticket_lifecycle(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：退款单完整生命周期——创建 → 查询 → 审批，且**二次审批**必须冲突。

    重复审批会重复打款，所以状态已变时只能报 ``CONFLICT_STATE``。
    """
    created = _invoke(
        backend, "create_refund_ticket", ctx, order_id="A1003", amount_cents=9900, reason="退货"
    )
    ticket_id = created.data["refund_id"]

    fetched = _invoke(backend, "get_refund_ticket", ctx, ticket_id=ticket_id)
    assert fetched.ok and fetched.data["status"] == "pending_confirmation"

    approved = _invoke(backend, "approve_refund", ctx, ticket_id=ticket_id)
    assert approved.ok and approved.data["status"] == "approved"

    # 二次审批：状态已变，必须冲突而不是再次执行
    again = _invoke(backend, "approve_refund", ctx, ticket_id=ticket_id)
    assert again.error_code == ErrorCode.CONFLICT_STATE.value

    missing = _invoke(backend, "get_refund_ticket", ctx, ticket_id="R000")
    assert missing.error_code == ErrorCode.NOT_FOUND.value


def test_create_service_ticket_and_schedule(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：建工单 → 预约的主流程，并覆盖三类拒绝：未知类型、已完成工单改约、工单不存在。"""
    created = _invoke(
        backend, "create_service_ticket", ctx, order_id="A1001", type="repair", description="不制冷"
    )
    assert created.ok and created.data["status"] == "created"
    ticket_id = created.data["ticket_id"]

    scheduled = _invoke(
        backend, "schedule_service", ctx, ticket_id=ticket_id, time="2026-10-20T10:00:00"
    )
    assert scheduled.ok and scheduled.data["status"] == "scheduled"

    bad_type = _invoke(backend, "create_service_ticket", ctx, order_id="A1001", type="unknown")
    assert bad_type.error_code == ErrorCode.INVALID_ARGUMENT.value

    # T5001 已完成，不可改约
    conflict = _invoke(
        backend, "schedule_service", ctx, ticket_id="T5001", time="2026-10-21T10:00:00"
    )
    assert conflict.error_code == ErrorCode.CONFLICT_STATE.value

    missing = _invoke(
        backend, "schedule_service", ctx, ticket_id="T0000", time="2026-10-21T10:00:00"
    )
    assert missing.error_code == ErrorCode.NOT_FOUND.value


def test_escalate_to_human(backend: MockBackend, business_db: Database, ctx: ToolContext) -> None:
    """验证：转人工写入审计且 ``decision=escalate``——人工介入要留痕，便于事后复盘。"""
    result = _invoke(backend, "escalate_to_human", ctx, reason="用户要求主管处理")
    assert result.ok is True
    assert result.data["status"] == "queued"

    audits = [row for row in _audit_rows(business_db) if row["action"] == "escalate_to_human"]
    assert len(audits) == 1 and audits[0]["decision"] == "escalate"


def test_unknown_tool_returns_error_not_exception(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：调用不存在的工具返回 ``NOT_FOUND`` 结果而非抛异常——模型幻觉不能打挂进程。"""
    result = backend.invoke("not_a_tool", {}, ctx)
    assert result.ok is False
    assert result.error_code == ErrorCode.NOT_FOUND.value


# --------------------------------------------------------------------- 幂等（经执行器）


def test_refund_created_once_under_repeated_calls(
    backend: MockBackend, business_db: Database, idem_store, ctx: ToolContext
) -> None:
    """验证：经执行器重复调用只落**一笔**退款，第二次是 ``IDEMPOTENT_REPLAY``。"""
    executor = _executor(backend, idem_store)
    keyed = ctx.model_copy(update={"idempotency_key": "idem-refund-1"})
    args = dict(order_id="A1003", amount_cents=9900, reason="退货")
    spec = next(s for s in backend.list_tools() if s.name == "create_refund_ticket")

    first = executor.call(spec, args, keyed)
    second = executor.call(spec, args, keyed)

    assert first.ok is True
    assert second.ok is True and second.error_code == ErrorCode.IDEMPOTENT_REPLAY.value
    assert business_db.count("refund_requests") == 1


def test_cancel_replay_does_not_touch_database_again(
    backend: MockBackend, idem_store, ctx: ToolContext
) -> None:
    """验证：取消订单重放不再触碰数据库——状态已终态，重复执行无意义且有副作用。"""
    executor = _executor(backend, idem_store)
    spec = next(s for s in backend.list_tools() if s.name == "cancel_order")
    keyed = ctx.model_copy(update={"idempotency_key": "idem-cancel-1"})
    args = dict(order_id="A1002", reason="不想要了")

    first = executor.call(spec, args, keyed)
    second = executor.call(spec, args, keyed)

    assert first.ok and first.data["status"] == "cancelled"
    assert second.error_code == ErrorCode.IDEMPOTENT_REPLAY.value


def test_write_without_key_is_refused_by_executor(
    backend: MockBackend, business_db: Database, idem_store, ctx: ToolContext
) -> None:
    """验证：写工具无幂等键被执行器拦下，库里一条都没多——拦截发生在调用后端之前。"""
    executor = _executor(backend, idem_store)
    spec = next(s for s in backend.list_tools() if s.name == "create_refund_ticket")

    result = executor.call(spec, dict(order_id="A1003", amount_cents=9900, reason="退货"), ctx)

    assert result.error_code == ErrorCode.INVALID_ARGUMENT.value
    assert business_db.count("refund_requests") == 0


def test_database_layer_blocks_duplicate_refund_even_without_executor(
    backend: MockBackend, business_db: Database, ctx: ToolContext
) -> None:
    """兜底防护：绕过执行器重复调用时，数据库层也不会落下第二笔退款单。"""
    keyed = ctx.model_copy(update={"idempotency_key": "idem-refund-2"})
    args = dict(order_id="A1003", amount_cents=9900, reason="退货")

    first = backend.invoke("create_refund_ticket", args, keyed)
    second = backend.invoke("create_refund_ticket", args, keyed)

    assert first.ok is True
    assert second.ok is True and second.error_code == ErrorCode.IDEMPOTENT_REPLAY.value
    assert business_db.count("refund_requests") == 1


# --------------------------------------------------------------------- 脱敏与元信息


@pytest.mark.parametrize("table", ["users", "orders"])
def test_stored_pii_is_masked(business_db: Database, table: str) -> None:
    """库里不得出现 11 位手机号或未脱敏地址。"""
    blob = json.dumps(business_db.rows(table), ensure_ascii=False)
    assert not re.search(r"\b1[3-9]\d{9}\b", blob), "出现了明文手机号"
    for row in business_db.rows(table):
        for key, value in row.items():
            if key.endswith("_masked"):
                assert "*" in str(value), f"{table}.{key} 未脱敏"


def test_every_result_carries_latency_and_trace(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：每个结果都带 ``latency_ms`` 与 ``trace_id``——可观测性不靠调用方补。"""
    result = _invoke(backend, "get_order", ctx, order_id="A1001")
    assert result.latency_ms >= 0
    assert result.trace_id == ctx.trace_id
