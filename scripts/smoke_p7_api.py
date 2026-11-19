"""P7 API 冒烟：health → chat → HITL 挂起/查询/批准/恢复 → 回调 501。

用法：``python -m scripts.smoke_p7_api --base-url http://localhost:8000``
只依赖标准库，Docker 健康检查与 run_p7.sh 共用。
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from typing import Any

__all__ = ["main"]


def _request(method: str, url: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"raw": body}
        return exc.code, parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args(argv)
    base = args.base_url.rstrip("/")

    status, health = _request("GET", f"{base}/health")
    assert status == 200, (status, health)
    assert health.get("status") == "ok", health

    status, chat = _request(
        "POST",
        f"{base}/chat",
        {"session_id": "p7-smoke", "text": "查订单 A1001", "user_id": "U1"},
    )
    assert status == 200, (status, chat)
    assert chat.get("status") == "completed", chat
    assert "A1001" in chat.get("text", ""), chat

    status, pending = _request(
        "POST",
        f"{base}/chat",
        {
            "session_id": "p7-hitl",
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
    assert status == 200, (status, pending)
    assert pending.get("status") == "awaiting_confirmation", pending
    approval_id = (pending.get("pending_confirmation") or {}).get("approval_id")
    assert approval_id, pending

    status, hitl = _request("GET", f"{base}/hitl/{approval_id}")
    assert status == 200, (status, hitl)
    assert hitl.get("status") == "pending", hitl

    status, resolved = _request(
        "POST",
        f"{base}/hitl/{approval_id}/resolve",
        {"approve": True, "actor": "alice"},
    )
    assert status == 200, (status, resolved)
    assert resolved.get("status") == "approved", resolved

    status, resumed = _request(
        "POST",
        f"{base}/chat",
        {"session_id": "p7-hitl", "text": "", "user_id": "U1", "approval_id": approval_id},
    )
    assert status == 200, (status, resumed)
    assert resumed.get("status") in {"completed", "awaiting_confirmation"}, resumed

    status, callback = _request("POST", f"{base}/channels/wecom/callback", {})
    assert status == 501, (status, callback)

    print(
        json.dumps(
            {
                "ok": True,
                "health": health,
                "chat_status": chat["status"],
                "approval_id": approval_id,
                "resume_status": resumed["status"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
