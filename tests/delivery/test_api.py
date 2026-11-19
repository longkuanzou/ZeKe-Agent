"""P7 API：health / chat / HITL / 回调预留。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api import create_app
from zkagent.backends.mock.db import Database
from zkagent.backends.mock.seed import seed_database
from zkagent.config import load_industry_pack, load_settings, pack_file

PACK_DIR = Path(__file__).resolve().parents[2] / "zkagent" / "industry_packs" / "appliance"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    db = Database(tmp_path / "business.db")
    db.reset()
    pack = load_industry_pack(PACK_DIR)
    seed_database(db, pack_file(PACK_DIR, pack.seed_data))
    settings = load_settings(
        industry_id="appliance",
        db_path=str(db.path),
        checkpoints_db_path=str(tmp_path / "checkpoints.db"),
        provider="fake",
    )
    return TestClient(create_app(settings))


def test_health_reports_delivery_fields(client: TestClient) -> None:
    """验证：``/health`` 报告交付所需的最低信息（状态、行业包、后端、HITL 模式）。

    ``hitl_mode`` 必须显式暴露——部署检查靠它判断「是不是还在开发模式」。
    """
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["industry"] == "appliance"
    assert body["backend"] == "mock"
    assert body["hitl_mode"] in {"token_verified", "dev_unauthenticated"}


def test_chat_returns_normalized_result(client: TestClient) -> None:
    """验证：``POST /chat`` 返回归一化结果（status / text / tool_calls），调用方可直接消费。

    不把内部对象整体透出去，API 契约与内部模型解耦。
    """
    response = client.post(
        "/chat",
        json={"session_id": "s1", "text": "查订单 A1001", "user_id": "U1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert "A1001" in body["text"]
    assert any(call["name"] == "get_order" and call["ok"] for call in body["tool_calls"])


def test_chat_hitl_then_resolve_and_resume(client: TestClient) -> None:
    """验证：API 侧 HITL 全流程——挂起 → 查状态 → 批准 → 带 ``approval_id`` 恢复。

    四步都走 HTTP，证明审批能力不只在 CLI 里存在。
    """
    response = client.post(
        "/chat",
        json={
            "session_id": "s-hitl",
            "text": "订单 A1001 退全款",
            "user_id": "U1",
            "script": [
                {"tool_calls": [{"name": "get_order", "args": {"order_id": "A1001"}}]},
                {
                    "tool_calls": [
                        {
                            "name": "create_refund_ticket",
                            "args": {"order_id": "A1001", "reason": "质量问题"},
                        }
                    ]
                },
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_confirmation"
    pending = body["pending_confirmation"]
    approval_id = pending["approval_id"]

    status = client.get(f"/hitl/{approval_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "pending"

    resolved = client.post(
        f"/hitl/{approval_id}/resolve",
        json={"approve": True, "actor": "alice"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "approved"

    resumed = client.post(
        "/chat",
        json={
            "session_id": "s-hitl",
            "text": "",
            "user_id": "U1",
            "approval_id": approval_id,
        },
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] in {"completed", "awaiting_confirmation"}


def test_hitl_missing_is_404(client: TestClient) -> None:
    """验证：审批单不存在返回 404 而不是 500——查无此单属正常输入，不是服务故障。"""
    assert client.get("/hitl/AP-NOT-FOUND").status_code == 404


def test_channel_callback_is_501(client: TestClient) -> None:
    """验证：企微回调端点保留为 501（接入点预留），不假装已支持。"""
    response = client.post("/channels/wecom/callback", json={})
    assert response.status_code == 501
