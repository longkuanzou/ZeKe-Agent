"""``homeapp`` 业务数据与场景用例的完整性校验。

这些断言只读 YAML，不连库、不调模型，属于**数据契约**层：

1. 种子数据自洽：订单 SKU 存在、退款只挂已签收订单、工单属于订单用户、金额不超订单；
2. 场景覆盖：30 单里该有的状态与边界单都在（不是「有数据」就行，要能测出问题）；
3. 用例可执行：``business_cases.yaml`` 引用的工具已启用、数据真实存在。

数据由 ``scripts/gen_homeapp_seed.py`` 生成；本测试是它的第二道闸门——
手改 YAML 或改了 schema 而忘了重跑，这里会先报。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
import yaml

from zkagent.config import default_industry_packs_dir, load_industry_pack, load_tools_config
from zkagent.core.rag.metadata import CONSULT_JOURNEYS

PACK_DIR = default_industry_packs_dir() / "homeapp"
pytestmark = pytest.mark.skipif(not PACK_DIR.is_dir(), reason="尚未生成 homeapp 行业包")

SEED = PACK_DIR / "seed_data"
CASES = PACK_DIR / "evals" / "business_cases.yaml"


def _rows(filename: str) -> list[dict]:
    data = yaml.safe_load((SEED / filename).read_text(encoding="utf-8"))
    assert isinstance(data, dict) and len(data) == 1, f"{filename} 顶层应为单键映射"
    return next(iter(data.values())) or []


def _cases() -> list[dict]:
    data = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    return data["cases"]


def test_seed_tables_are_populated() -> None:
    """每张业务表都必须有行——避免「表建了、数据没灌」的假通过。"""
    expected = {
        "users": 4, "user_homes": 4, "products": 12, "inventory": 12, "promotions": 5,
        "orders": 30, "logistics": 8, "service_tickets": 7, "refund_requests": 4, "invoices": 4,
    }
    actual = {name: len(_rows(f"{name}.yaml")) for name in expected}
    assert actual == expected, f"种子行数与预期不符：{actual}"
    assert len(_rows("logistics_events.yaml")) >= 20
    assert len(_rows("ticket_events.yaml")) >= 12
    # 计数容易漏：真正要守的是「下过单的人必须有房屋记录」（下单必有收货地址）。
    # 曾出现 H4 有 15 单但无 user_homes 的情况。
    order_users = {row["user_id"] for row in _rows("orders.yaml")}
    home_users = {row["user_id"] for row in _rows("user_homes.yaml")}
    assert order_users <= home_users, f"有订单但无房屋记录：{sorted(order_users - home_users)}"


def test_orders_cover_every_stage() -> None:
    """30 单必须覆盖四个状态，且边界单真的存在。"""
    orders = {row["order_id"]: row for row in _rows("orders.yaml")}
    assert Counter(row["status"] for row in orders.values()) == {
        "paid": 8, "shipped": 7, "delivered": 12, "cancelled": 3,
    }
    assert orders["H2601"]["status"] == "paid"
    assert orders["H2611"]["status"] == "shipped"       # 停滞单
    assert orders["H2618"]["status"] == "delivered"     # 超 7 天
    assert orders["H2626"]["status"] == "delivered"     # 3 年多前购机（保外判断）
    assert orders["H2602"]["user_id"] == "H2"           # 越权样本


def test_seed_referential_integrity() -> None:
    """验证：种子数据的引用完整性——订单 SKU 存在、工单与订单同用户、退款只挂已签收单且金额不超订单。"""
    users = {row["user_id"] for row in _rows("users.yaml")}
    products = {row["sku"]: row for row in _rows("products.yaml")}
    orders = {row["order_id"]: row for row in _rows("orders.yaml")}
    tickets = {row["ticket_id"]: row for row in _rows("service_tickets.yaml")}

    for row in orders.values():
        assert row["sku"] in products, f"订单 {row['order_id']} 引用了不存在的 SKU"
        assert row["user_id"] in users
        assert row["status"] in {"paid", "shipped", "delivered", "cancelled"}
        if row["status"] == "delivered":
            assert row["delivered_at"], f"已签收订单缺签收时间：{row['order_id']}"

    for row in _rows("service_tickets.yaml"):
        order = orders[row["order_id"]]
        assert row["user_id"] == order["user_id"], f"工单 {row['ticket_id']} 与订单用户不一致"
    for row in _rows("ticket_events.yaml"):
        assert row["ticket_id"] in tickets

    for row in _rows("refund_requests.yaml"):
        order = orders[row["order_id"]]
        assert order["status"] == "delivered", f"退款单 {row['refund_id']} 挂在未签收订单"
        assert 0 < row["amount_cents"] <= order["amount_cents"]
        assert json.loads(row["items_json"]) or row["items_json"]

    for row in _rows("invoices.yaml"):
        assert row["order_id"] in orders
    for row in _rows("logistics_events.yaml"):
        assert row["order_id"] in orders


def test_products_match_brand_categories() -> None:
    """商品必须覆盖三个品类，且有 1 个下架机（用于「同型号现售价/备件」）。"""
    products = _rows("products.yaml")
    assert Counter(row["category"] for row in products) == {
        "aircon": 4, "washer": 4, "rice_cooker": 4,
    }
    assert [row["sku"] for row in products if row["status"] == "off_shelf"] == ["SI-WM12P2602"]
    assert all(json.loads(row["features_json"]) for row in products)


def test_business_cases_are_runnable() -> None:
    """用例引用的工具必须在 enabled 里、数据必须真实存在。"""
    enabled = set(load_tools_config(load_industry_pack(PACK_DIR), PACK_DIR).enabled)
    indexes = {
        "products": {row["sku"] for row in _rows("products.yaml")},
        "orders": {row["order_id"] for row in _rows("orders.yaml")},
        "users": {row["user_id"] for row in _rows("users.yaml")},
        "homes": {row["home_id"] for row in _rows("user_homes.yaml")},
        "tickets": {row["ticket_id"] for row in _rows("service_tickets.yaml")},
        "refunds": {row["refund_id"] for row in _rows("refund_requests.yaml")},
        "invoices": {row["invoice_id"] for row in _rows("invoices.yaml")},
        "logistics": {row["order_id"] for row in _rows("logistics.yaml")},
        "inventory": {row["sku"] for row in _rows("inventory.yaml")},
        "promotions": {row["promo_id"] for row in _rows("promotions.yaml")},
    }

    cases = _cases()
    assert len(cases) >= 20, f"业务用例应 ≥20 条，实际 {len(cases)}"
    assert len({case["id"] for case in cases}) == len(cases), "用例 id 重复"

    for case in cases:
        assert case["journey"] in CONSULT_JOURNEYS, case["id"]
        assert case.get("as_user", "H1") in indexes["users"], case["id"]
        expect = case.get("expect") or {}
        assert expect, f"{case['id']} 没有任何期望，等于没测"
        for tool in expect.get("any_tool") or []:
            assert tool in enabled, f"{case['id']} 引用了未启用的工具：{tool}"
        for needle in expect.get("must_include") or []:
            assert isinstance(needle, str) and needle, case["id"]
        for ref in case.get("data") or []:
            kind, _, value = ref.partition(":")
            assert kind in indexes, f"{case['id']} 的数据引用类型未知：{ref}"
            assert value in indexes[kind], f"{case['id']} 引用了不存在的数据：{ref}"


def test_every_case_journey_has_data() -> None:
    """用例标注的链路必须真有对应数据支撑，避免「标了链路但没有数据」。"""
    orders = _rows("orders.yaml")
    tickets = _rows("service_tickets.yaml")
    journeys = {case["journey"] for case in _cases()}
    assert journeys == set(CONSULT_JOURNEYS), f"业务用例未覆盖全部链路：{sorted(journeys)}"
    assert any(t["type"] == "install" for t in tickets), "install 链路没有工单数据"
    assert any(t["type"] == "repair" for t in tickets), "repair_request 链路没有工单数据"
    assert len(orders) == 30
