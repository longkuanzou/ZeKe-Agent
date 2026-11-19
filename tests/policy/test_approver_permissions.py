"""L16：审批人权限不足时不得批准；补齐权限后才允许。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from apps.cli import main
from zkagent.backends.mock.approvals import SqliteApprovalStore
from zkagent.backends.mock.db import Database
from zkagent.core.policy.approvals import ApprovalRecord


def _record() -> ApprovalRecord:
    return ApprovalRecord(
        approval_id="AP-L16",
        action="create_refund_ticket",
        params_fingerprint="fingerprint",
        status="pending",
        requester_id="H1",
        required_permissions=["refund:approve"],
        created_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    )


def _env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ZKAGENT_INDUSTRY", "appliance")
    monkeypatch.setenv("ZKAGENT_DB_PATH", str(tmp_path / "business.db"))
    monkeypatch.setenv("ZKAGENT_CHECKPOINTS_DB", str(tmp_path / "checkpoints.db"))
    monkeypatch.setenv("ZKAGENT_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.delenv("HITL_APPROVERS", raising=False)
    monkeypatch.delenv("HITL_APPROVER_PERMISSIONS", raising=False)


def test_approve_without_permission_is_denied(tmp_path: Path, monkeypatch, capsys) -> None:
    """验证：审批人不具备规则要求的权限（``refund:approve``）时，批准被拒。

    退出码 3、stderr 出现 ``APPROVER_UNAUTHORIZED``、审批单**保持 pending**——
    被拒不等于被驳回，还有补救余地。对应 L16 的前半段。
    """
    _env(tmp_path, monkeypatch)
    db = Database(tmp_path / "business.db")
    db.initialize()
    store = SqliteApprovalStore(db)
    store.create(_record())

    code = main(["approvals", "approve", "AP-L16", "--actor", "bob"])

    assert code == 3
    assert store.get("AP-L16").status == "pending"
    assert "APPROVER_UNAUTHORIZED" in capsys.readouterr().err


def test_approve_with_permission_succeeds(tmp_path: Path, monkeypatch) -> None:
    """验证：命令行显式带上 ``--permissions refund:approve`` 后，同一个人就能批准。

    与上一条对照，证明拦住他的是**权限**而不是身份本身。
    """
    _env(tmp_path, monkeypatch)
    db = Database(tmp_path / "business.db")
    db.initialize()
    store = SqliteApprovalStore(db)
    store.create(_record())

    code = main(
        ["approvals", "approve", "AP-L16", "--actor", "bob", "--permissions", "refund:approve"]
    )

    assert code == 0
    assert store.get("AP-L16").status == "approved"


def test_approver_permissions_from_env_mapping(tmp_path: Path, monkeypatch) -> None:
    """验证：``HITL_APPROVER_PERMISSIONS=bob=refund:approve|kb:internal`` 的映射生效。

    权限来自服务端可信配置，不取自对话内容，也无需审批人自己声明。
    """
    _env(tmp_path, monkeypatch)
    monkeypatch.setenv("HITL_APPROVER_PERMISSIONS", "bob=refund:approve|kb:internal")
    db = Database(tmp_path / "business.db")
    db.initialize()
    store = SqliteApprovalStore(db)
    store.create(_record())

    code = main(["approvals", "approve", "AP-L16", "--actor", "bob"])

    assert code == 0
    assert store.get("AP-L16").status == "approved"
