"""审批仓储：一次性消费、状态机不可逆、执行结果与审批状态正交。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from zkagent.backends.mock.approvals import SqliteApprovalStore
from zkagent.backends.mock.db import Database
from zkagent.core.policy.approvals import (
    ApprovalRecord,
    approval_id_for,
    fingerprint_of,
    is_expired,
)
from zkagent.core.policy.errors import ApprovalError


def _record(
    approval_id: str = "AP-TEST", *, 
    created_at: str | None = None
) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=approval_id,
        thread_id="thread-1",
        session_id="sess-1",
        action="create_refund_ticket",
        payload_json='{"order_id": "A1", "amount_cents": 100}',
        params_fingerprint=fingerprint_of(
            "create_refund_ticket", {"order_id": "A1", "amount_cents": 100}
        ),
        requester_id="U1",
        reason="大额",
        rule_id="refund.escalate",
        created_at=created_at or datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    )


@pytest.fixture()
def store(tmp_path) -> SqliteApprovalStore:
    db = Database(tmp_path / "business.db")
    db.initialize()
    return SqliteApprovalStore(db)


def test_create_is_idempotent(store: SqliteApprovalStore) -> None:
    """验证：同一 ``approval_id`` 重复建单只保留一条（``INSERT OR IGNORE`` 幂等）。

    LangGraph 的 ``interrupt`` 恢复会**重放**建单节点，若建单不幂等，一次挂起
    就会变出两条待审批记录。幂等由「确定性 id（见 ``approval_id_for``）+ 建单幂等」
    两半共同保证，缺一半都会漏。
    """
    first = store.create(_record())
    second = store.create(_record())
    assert first.approval_id == second.approval_id
    assert store.list_pending() == [first] or len(store.list_pending()) == 1


def test_resolve_once_then_terminal(store: SqliteApprovalStore) -> None:
    """验证：审批一次即终态——批准后再想驳回抛 ``APPROVAL_ALREADY_RESOLVED``。

    防两个审批人同时操作、或同一人重复提交，导致一条审批被判两次。
    """ 
    store.create(_record())
    approved = store.resolve(
        "AP-TEST", approve=True, approver_id="alice", approver_mode="token",
        approved_payload_json='{"order_id": "A1"}',
    )
    assert approved.status == "approved"
    assert approved.approver_mode == "token"

    with pytest.raises(ApprovalError, match="APPROVAL_ALREADY_RESOLVED") as excinfo:
        store.resolve("AP-TEST", approve=False, approver_id="bob", approver_mode="token")
    print(repr(str(excinfo.value)))   # 'APPROVAL_ALREADY_RESOLVED: AP-TEST 当前为 approved'
    print(excinfo.value.code)         # 'APPROVAL_ALREADY_RESOLVED'
    print(excinfo.type, excinfo.value)        


def test_consume_is_one_shot(store: SqliteApprovalStore) -> None:
    """验证：消费一次性——未批准不能消费，已消费再消费必须失败。

    这是**防重复退款的最后一道闸**：并发两个 ``resume`` 同时恢复时，
    ``WHERE consumed_at IS NULL`` 保证只有一个能拿到消费权。
    """
    store.create(_record())
    with pytest.raises(ApprovalError, match="APPROVAL_NOT_APPROVED"):
        store.consume("AP-TEST")

    store.resolve("AP-TEST", approve=True, approver_id="alice", approver_mode="token")
    consumed = store.consume("AP-TEST")
    assert consumed.consumed_at

    with pytest.raises(ApprovalError, match="APPROVAL_NOT_CONSUMABLE"):
        store.consume("AP-TEST")


def test_execution_status_is_separate(store: SqliteApprovalStore) -> None:
    """验证：执行结果与审批状态**正交**——业务失败只写 ``execution_status``，不改 ``status``。

    「人同不同意」和「业务成没成」是两件事。分开存才能回答「审批通过了但退款失败」，
    而不是把执行失败回滚成「没批」那样丢失事实。
    """
    store.create(_record())
    store.resolve("AP-TEST", approve=True, approver_id="alice", approver_mode="token")
    store.consume("AP-TEST")
    store.finish("AP-TEST", execution_status="failed", error_code="CONFLICT_STATE")

    record = store.get("AP-TEST")
    assert record.status == "approved", "业务失败不得改写审批状态"
    assert record.execution_status == "failed"
    assert record.execution_error_code == "CONFLICT_STATE"


def test_list_pending_only_pending(store: SqliteApprovalStore) -> None:
    """验证：``list_pending`` 只回 pending 记录——CLI ``approvals list`` 的数据来源。

    已批准/已驳回的不得再出现在待办里，否则审批人会重复处理。
    """
    store.create(_record("AP-1"))
    store.create(_record("AP-2"))
    store.resolve("AP-1", approve=True, approver_id="alice", approver_mode="token")
    pending = store.list_pending()
    assert [item.approval_id for item in pending] == ["AP-2"]


def test_expiry_is_time_based(store: SqliteApprovalStore) -> None:
    """验证：过期按 ``created_at`` + TTL 小时数计算，且是**惰性判定**。

    没有后台任务扫表改写状态，只在查询（CLI）与恢复（runner）时现算；
    TTL 来自行业包 ``settings.approval_ttl_hours``。
    """
    old = (datetime.now() - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S")
    record = store.create(_record("AP-OLD", created_at=old))
    assert is_expired(record, 24) is True
    assert is_expired(record, 48) is False


def test_approval_id_is_deterministic() -> None:
    """验证：``approval_id`` 由 ``thread_id|step|fingerprint`` 确定性推导。

    同输入必得同 id（重放不产生第二条）；step 变了 id 必须变，
    否则第 3 步和第 4 步的挂起会共用同一张审批单。
    """
    first = approval_id_for("thread-1", 3, "fingerprint")
    second = approval_id_for("thread-1", 3, "fingerprint")
    assert first == second
    assert first.startswith("AP-")
    assert approval_id_for("thread-1", 4, "fingerprint") != first
