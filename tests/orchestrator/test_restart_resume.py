"""L17：挂起后换一个进程恢复，退款只落一笔。

严格说这不是「kill 正在执行的进程」，而是「原进程结束后，新进程按同一
checkpoint / 审批库恢复」——它验证的是 L17 真正关心的不变量：恢复不依赖进程内状态。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from zkagent.backends.mock.db import Database
from zkagent.backends.mock.seed import seed_database
from zkagent.config import (
    Container,
    default_industry_packs_dir,
    load_industry_pack,
    load_settings,
    pack_file,
)

HOMEAPP = default_industry_packs_dir() / "homeapp"
pytestmark = pytest.mark.skipif(not HOMEAPP.is_dir(), reason="尚未生成 homeapp 行业包")

_CHILD = """
import json
import sys

from zkagent.config import Container, load_settings
from zkagent.core.types import PolicyDecision

db_path, checkpoints, industry, session, approval_id = sys.argv[1:6]
container = Container.build(
    load_settings(industry_id=industry, db_path=db_path, 
    checkpoints_db_path=checkpoints)
)
runner, _ctx = container.make_runner(
    user_id="H1",
    session_id=session,
    provider="fake",
    task="restart-resume",
    script=[{"content": "ok"}],
)
result = runner.resume(
    session,
    PolicyDecision(
        action="create_refund_ticket",
        allowed=True,
        decision="require_confirmation",
        reason="restart",
        rule_id="refund.escalate_high_value_or_overdue",
        approval_id=approval_id,
        approver_id="alice",
        approver_mode="token",
    ),
)
record = container.approval_store.get(approval_id)
print(json.dumps({"status": result.status, "execution": record.execution_status}))
"""


def test_resume_from_a_new_process_executes_once(tmp_path: Path) -> None:
    """验证：原进程结束后，用**新的子进程**按同一 checkpoint / 审批库恢复，退款只落一笔。

    对应 L17：恢复不依赖进程内状态（审批记录、checkpoint、幂等都在库里）。
    挂起阶段断言退款数不变，恢复后断言 +1——多一笔就说明幂等失效。
    """
    db_path = tmp_path / "business.db"
    checkpoints = tmp_path / "checkpoints.db"
    db = Database(db_path)
    db.reset()
    config = load_industry_pack(HOMEAPP)
    seed_database(db, pack_file(HOMEAPP, config.seed_data))
    before = db.count("refund_requests")

    settings = load_settings(
        industry_id="homeapp",
        db_path=str(db_path),
        checkpoints_db_path=str(checkpoints),
        trace_dir=str(tmp_path / "traces"),
    )
    container = Container.build(settings)
    runner, ctx = container.make_runner(
        user_id="H1",
        session_id="restart-s1",
        provider="fake",
        task="restart-suspend",
        script=[
            {"tool_calls": [{"name": "verify_user", "args": {"factor": "2046"}}]},
            {
                "tool_calls": [
                    {
                        "name": "create_refund_ticket",
                        "args": {
                            "order_id": "H2618",
                            "amount_cents": 699900,
                            "reason": "质量问题",
                            "items": ["整机"],
                        },
                    }
                ]
            },
            {"content_from_tool": "已提交审批。"},
        ],
    )

    pending = runner.run("restart-s1", "订单 H2618 退全款", ctx)
    assert pending.status == "awaiting_confirmation"
    approval_id = str((pending.pending_confirmation or {})["approval_id"])
    record = container.approval_store.get(approval_id)
    assert record is not None
    container.approval_store.resolve(
        approval_id,
        approve=True,
        approver_id="alice",
        approver_mode="token",
        approved_payload_json=record.payload_json,
    )
    assert db.count("refund_requests") == before, "挂起阶段不得先落库"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD,
            str(db_path),
            str(checkpoints),
            "homeapp",
            "restart-s1",
            approval_id,
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["status"] == "completed"
    assert payload["execution"] == "succeeded"
    assert db.count("refund_requests") == before + 1, "只允许落一笔退款"
