"""P5 新增的三个业务工具：优惠查询、安装报价、发票申请。

同时验证 ``always_include`` 的保底语义：上限收紧时核心工具仍排在最前。
"""

from __future__ import annotations

import pytest

from zkagent.backends.mock.backend import MockBackend
from zkagent.backends.mock.db import Database
from zkagent.backends.mock.seed import seed_database
from zkagent.config import default_industry_packs_dir, load_industry_pack, pack_file
from zkagent.core.tools.registry import ToolRegistry
from zkagent.core.types import ToolContext, ToolSpec

HOME_PACK = default_industry_packs_dir() / "homeapp"
pytestmark = pytest.mark.skipif(not HOME_PACK.is_dir(), reason="尚未生成 homeapp 行业包")


@pytest.fixture()
def home_db(tmp_path):
    db = Database(tmp_path / "business.db")
    db.reset()
    config = load_industry_pack(HOME_PACK)
    seed_database(db, pack_file(HOME_PACK, config.seed_data))
    return db


@pytest.fixture()
def home_backend(home_db) -> MockBackend:
    return MockBackend(home_db)


def _ctx(user_id: str = "H1") -> ToolContext:
    return ToolContext(
        trace_id="trace-p5",
        session_id="s1",
        user_id=user_id,
        actor="user",
        permissions=[],
        industry_id="homeapp",
        channel_id="cli",
    )


def test_list_promotions_by_sku_returns_active_only(home_backend: MockBackend) -> None:
    """验证：按 SKU 查促销只回**在售**活动，能取到种子里的 PM2601。"""
    result = home_backend.invoke("list_promotions", {"sku": "MD-KFR35-Q2"}, _ctx())

    assert result.ok is True
    assert result.data["count"] >= 1
    assert any(item["promo_id"] == "PM2601" for item in result.data["promotions"])


def test_list_promotions_missing_sku_is_not_found(home_backend: MockBackend) -> None:
    """验证：SKU 不存在返回 ``NOT_FOUND``，而不是空列表——查无此物与没有活动是两回事。"""
    result = home_backend.invoke("list_promotions", {"sku": "NO-SUCH"}, _ctx())

    assert result.ok is False
    assert result.error_code == "NOT_FOUND"


def test_installation_quote_uses_floor_and_elevator(home_backend: MockBackend) -> None:
    """H1 的房屋：6 楼、无电梯、门宽 68、外机位「有」、16A。"""
    result = home_backend.invoke("get_installation_quote", {"sku": "MD-KFR35-Q2"}, _ctx())

    assert result.ok is True
    data = result.data
    assert data["installable"] is True
    assert data["requires_survey"] is False
    assert data["total_cents"] == 20000 + (6 - 3) * 5000
    assert data["simulated"] is True


def test_installation_quote_denies_other_users_home(home_backend: MockBackend) -> None:
    """验证：指定他人 ``home_id`` 报价返回 ``PERMISSION_DENIED``——房屋信息不得跨用户读取。"""
    result = home_backend.invoke(
        "get_installation_quote", {"sku": "MD-KFR35-Q2", "home_id": "HM2"}, _ctx()
    )

    assert result.ok is False
    assert result.error_code == "PERMISSION_DENIED"


def test_submit_invoice_then_reuse_pending(home_backend: MockBackend, home_db: Database) -> None:
    """验证：同一订单重复开发票**复用**已存在的 pending 单，不产生第二张，并写审计日志。"""
    first = home_backend.invoke(
        "submit_invoice_request", {"order_id": "H2601", "kind": "normal"}, _ctx()
    )
    second = home_backend.invoke(
        "submit_invoice_request", {"order_id": "H2601", "kind": "special"}, _ctx()
    )

    assert first.ok is True
    assert first.data["status"] == "pending"
    assert second.ok is True
    assert second.data["reused"] is True
    assert second.data["invoice_id"] == first.data["invoice_id"]
    assert home_db.count("invoices") == 5, "重复申请不能产生第二张发票"
    assert any(row["action"] == "submit_invoice_request" for row in home_db.rows("audit_logs"))


def test_submit_invoice_rejects_already_issued(home_backend: MockBackend) -> None:
    """验证：已开票订单再申请返回 ``CONFLICT_STATE``——发票不可重复开具。"""
    result = home_backend.invoke(
        "submit_invoice_request", {"order_id": "H2616", "kind": "normal"}, _ctx()
    )

    assert result.ok is False
    assert result.error_code == "CONFLICT_STATE"


def test_always_include_survives_max_tools_and_keeps_order() -> None:
    """验证：``max_tools`` 收紧时 ``always_include`` 的核心工具仍入选且排在最前，不重复。"""
    registry = ToolRegistry()
    registry.register_many([ToolSpec(name=f"t{index:02d}", description="x") for index in range(20)])

    selected = registry.select(max_tools=5, always_include=["t03", "t01"])

    assert len(selected) == 5
    assert [spec.name for spec in selected[:2]] == ["t03", "t01"], "核心工具按配置顺序优先"
    assert len({spec.name for spec in selected}) == 5


def test_always_include_typo_fails_loudly() -> None:
    """验证：``always_include`` 写错工具名直接抛错——拼错会静默丢掉核心工具，必须响。"""
    registry = ToolRegistry()
    registry.register(ToolSpec(name="real_tool", description="x"))

    with pytest.raises(ValueError, match="always_include"):
        registry.select(always_include=["typo_tool"])
