"""商品结构化查询（售前硬参数）：``search_products`` / ``get_product``。

为什么单列一个文件：售前的「容量 / 能效 / 尺寸 / 价格 / 是否支持烘干」必须由**结构化数据**回答，
不能靠知识库检索去猜（那正是 KNOWN_ISSUES 里 S5 的成因）。这里固化三件事：

1. 过滤条件全部在**服务端**生效（品类 / 功能 / 价格上限 / 在售状态）；
2. 查不到时如实返回空 + ``note``，**不得**编造型号或参数；
3. SKU 与订单对齐，``get_product`` 可以完成「订单 → 商品参数」的追溯。
"""

from __future__ import annotations

from zkagent.backends.mock.backend import MockBackend
from zkagent.core.tools.schema import ErrorCode
from zkagent.core.types import ToolContext, ToolResult


def _search(backend: MockBackend, ctx: ToolContext, **args) -> ToolResult:
    return backend.invoke("search_products", args, ctx)


def _skus(result: ToolResult) -> list[str]:
    return [item["sku"] for item in result.data["products"]]


# --------------------------------------------------------------------- 检索


def test_search_by_category(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：按品类过滤在**服务端**生效，``count`` 与实际返回条数一致。"""
    result = _search(backend, ctx, category="washer")

    assert result.ok is True
    assert result.data["count"] == len(result.data["products"])
    assert all(item["category"] == "washer" for item in result.data["products"])
    assert "WSH-3299-A" in _skus(result)


def test_search_by_feature_returns_only_matching_category(
    backend: MockBackend, ctx: ToolContext
) -> None:
    """「烘干」只有洗烘一体机型支持——空调不得因为「制热」被误命中。"""
    result = _search(backend, ctx, feature="烘干")

    assert result.ok is True
    assert _skus(result) == ["WSH-3299-A"]
    assert "烘干" in result.data["products"][0]["features"]


def test_search_price_cap_is_sorted_and_respected(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：价格上限过滤生效，且结果按价格升序（便于模型直接取最低价转述）。"""
    result = _search(backend, ctx, max_price_cents=300000)

    prices = [item["price_cents"] for item in result.data["products"]]
    assert result.ok is True
    assert prices == sorted(prices)
    assert all(price <= 300000 for price in prices)


def test_search_excludes_off_shelf_by_default(backend: MockBackend, ctx: ToolContext) -> None:
    """已停产机型默认不出现（「同型号现售价」这类判断需要显式打开）。"""
    default = _search(backend, ctx, category="fridge")
    assert "REF-1799-A" not in _skus(default)

    with_off_shelf = _search(backend, ctx, category="fridge", include_off_shelf=True)
    assert "REF-1799-A" in _skus(with_off_shelf)


def test_search_empty_result_is_explicit_not_fabricated(
    backend: MockBackend, ctx: ToolContext
) -> None:
    """验证：无匹配时返回空列表 + 明确 note，绝不编造一条商品顶替。"""
    result = _search(backend, ctx, keyword="不存在的型号XYZ")

    assert result.ok is True, "查不到不是错误，但必须说清楚"
    assert result.data["count"] == 0
    assert "note" in result.data and "不得编造" in result.data["note"]


def test_search_limit_is_validated(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：``limit`` 取值越界（0 或 >20）一律 ``INVALID_ARGUMENT``——边界必须在服务端守。"""
    for bad_limit in (0, 21):
        result = _search(backend, ctx, limit=bad_limit)
        assert result.error_code == ErrorCode.INVALID_ARGUMENT.value


def test_search_rejects_unknown_category(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：未收录品类被拒，且错误信息里给出可选值——模型才有机会自我纠正。"""
    result = _search(backend, ctx, category="television")

    assert result.error_code == ErrorCode.INVALID_ARGUMENT.value
    assert "aircon" in result.message, "错误信息要给出可选值，模型才有机会自我纠正"


def test_search_rejects_non_bool_flag(backend: MockBackend, ctx: ToolContext) -> None:
    """模型常把布尔写成字符串 "false"；必须拒绝，否则会被当成真值。"""
    result = _search(backend, ctx, include_off_shelf="false")

    assert result.error_code == ErrorCode.INVALID_ARGUMENT.value


# --------------------------------------------------------------------- 详情


def test_get_product_by_order_sku(backend: MockBackend, ctx: ToolContext) -> None:
    """订单 A1002 的 SKU 是 WSH-3299-A：订单 → 商品参数应可追溯。"""
    order = backend.invoke("get_order", {"order_id": "A1002"}, ctx)
    assert order.ok is True

    result = backend.invoke("get_product", {"sku": order.data["sku"]}, ctx)

    assert result.ok is True
    assert result.data["sku"] == "WSH-3299-A"
    assert result.data["capacity"] == "10kg"
    assert result.data["price_yuan"] == "3299.00", "金额真相是分，元的字符串只为便于转述"


def test_get_product_missing_sku_is_not_found(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：SKU 不存在返回 ``NOT_FOUND``，不得猜一个相近型号返回。"""
    result = backend.invoke("get_product", {"sku": "NOPE-0000"}, ctx)

    assert result.error_code == ErrorCode.NOT_FOUND.value


def test_get_product_requires_sku(backend: MockBackend, ctx: ToolContext) -> None:
    """验证：``get_product`` 必须给 SKU——没有定位键就查详情属非法调用。"""
    result = backend.invoke("get_product", {}, ctx)

    assert result.error_code == ErrorCode.INVALID_ARGUMENT.value


def test_catalog_is_public_read(backend: MockBackend) -> None:
    """商品目录是公开数据：不需要身份、不需要幂等键、风险等级为 low。"""
    spec = next(spec for spec in backend.list_tools() if spec.name == "search_products")

    assert spec.risk == "low"
    assert spec.idempotent is True
    assert spec.requires_idempotency_key is False
    assert spec.confirmation_required is False
