"""事实提供者：从 SQLite 取订单/工单/退款事实，缺失时不得编造。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from zkagent.backends.mock.db import Database
from zkagent.backends.mock.facts import MockFactsProvider
from zkagent.core.types import ToolContext

_NOW = datetime.now()
_FMT = "%Y-%m-%dT%H:%M:%S"


def _db(tmp_path) -> Database:
    db = Database(tmp_path / "business.db")
    db.initialize()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO users (user_id, name, phone_masked, level, created_at) VALUES (?,?,?,?,?)",
            ("U1", "张三", "138****1234", "gold", _NOW.strftime(_FMT)),
        )
        conn.execute(
            "INSERT INTO products (sku, category, model, name, price_cents, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("SKU1", "washer", "TG100", "小天鹅 洗衣机", 279900, "on_sale", _NOW.strftime(_FMT)),
        )
        conn.execute(
            "INSERT INTO orders (order_id, user_id, product, sku, amount_cents, status, "
            "created_at, delivered_at, address_masked) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "A1", "U1", "洗衣机", "SKU1", 279900, "delivered",
                (_NOW - timedelta(days=10)).strftime(_FMT),
                (_NOW - timedelta(days=3)).strftime(_FMT),
                "杭州市****",
            ),
        )
        conn.execute(
            "INSERT INTO service_tickets (ticket_id, order_id, user_id, type, description, "
            "status, scheduled_time, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("T1", "A1", "U1", "repair", "不排水", "scheduled", None, _NOW.strftime(_FMT)),
        )
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, happened_at, kind, note) VALUES (?,?,?,?)",
            ("T1", _NOW.strftime(_FMT), "part_wait", "等待排水泵"),
        )
        conn.execute(
            "INSERT INTO refund_requests (refund_id, order_id, user_id, items_json, amount_cents, "
            "reason, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("R1", "A1", "U1", "[]", 10000, "无理由", "pending_confirmation",
             _NOW.strftime(_FMT), _NOW.strftime(_FMT)),
        )
    return db


@pytest.fixture()
def provider(tmp_path) -> MockFactsProvider:
    return MockFactsProvider(_db(tmp_path))


def _ctx(verified: bool = True) -> ToolContext:
    return ToolContext(trace_id="t", session_id="s", user_id="U1", verified=verified)


def test_order_facts(provider: MockFactsProvider) -> None:
    """验证：订单事实能从 SQLite 正确投影出来供规则判定。

    覆盖状态、金额、签收天数、品类、归属用户五个字段——这些正是 ``rules.yaml``
    里 ``order_status`` / ``amount_cents`` / ``days_since_delivered`` 的判定依据。
    """
    facts = provider.facts_for("get_order", {"order_id": "A1"}, _ctx())
    assert facts.order_status == "delivered"
    assert facts.amount_cents == 279900
    assert facts.days_since_delivered == 3
    assert facts.category == "washer"
    assert facts.order_user_id == "U1"
    assert facts.verified is True


def test_verified_comes_from_context(provider: MockFactsProvider) -> None:
    """验证：``verified`` 取自 ToolContext（会话核验结果），不由数据库推断。

    防的事故是「查过订单就当作已核验」——核验是一次显式动作，库里有订单不代表身份通过。
    """
    facts = provider.facts_for("get_order", {"order_id": "A1"}, _ctx(verified=False))
    assert facts.verified is False


def test_ticket_facts_include_events(provider: MockFactsProvider) -> None:
    """验证：工单事实带出 ``ticket_events``，让事件成为规则的前置条件。

    homeapp 的 ``service.part_wait_handoff`` 就靠 ``part_wait`` 这个事件触发转人工。
    """
    facts = provider.facts_for("get_service_ticket", {"ticket_id": "T1"}, _ctx())
    assert facts.order_status == "delivered"
    assert facts.ticket_status == "scheduled"
    assert "part_wait" in facts.ticket_events


def test_refund_ticket_id_resolves_order(provider: MockFactsProvider) -> None:
    """验证：传退款单号也能反查到订单，且金额取**订单金额**而非退款申请金额。

    规则按订单金额判定档位（如 >1000 元转人工），取错金额会让大额退款绕过审批。
    """
    facts = provider.facts_for("get_refund_ticket", {"ticket_id": "R1"}, _ctx())
    assert facts.order_user_id == "U1"
    assert facts.amount_cents == 279900, "应取订单金额，不是退款金额"


def test_missing_order_returns_empty_facts(provider: MockFactsProvider) -> None:
    """验证：订单不存在时事实全为 ``None``，**不得编造默认值**。

    这是「缺失即不满足」原则：事实缺失必须让前置条件失败（走 deny），
    若填 0 或空串就可能误判成「已签收 0 天」从而放行。
    """
    facts = provider.facts_for("get_order", {"order_id": "NOPE"}, _ctx())
    assert facts.order_status is None
    assert facts.amount_cents is None
    assert facts.days_since_delivered is None
