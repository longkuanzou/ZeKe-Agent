"""后端契约测试：任何 ``BusinessBackend`` 实现都必须满足这些约束。

同一组用例将来会同时跑在 ``MockBackend`` 与 ``HttpBackend``（P7）上，
用于证明「换业务后端、Core 不改」。
"""

from __future__ import annotations

import pytest

from zkagent.backends.mock.backend import MockBackend
from zkagent.core.tools.schema import ErrorCode
from zkagent.core.types import ToolContext, ToolResult

#: 归一化标准工具中应由后端提供的 20 个（knowledge_search 属 Core RAG，P3 落地）。
EXPECTED_TOOL_NAMES = {
    "get_user",
    "get_user_home",
    "verify_user",
    "search_products",
    "get_product",
    "list_promotions",
    "get_installation_quote",
    "get_order",
    "list_orders",
    "cancel_order",
    "create_refund_ticket",
    "get_refund_ticket",
    "get_invoice",
    "submit_invoice_request",
    "approve_refund",
    "create_service_ticket",
    "schedule_service",
    "get_service_ticket",
    "get_logistics",
    "escalate_to_human",
}

#: 需要幂等键的写操作。
EXPECTED_WRITE_TOOLS = {
    "cancel_order",
    "create_refund_ticket",
    "approve_refund",
    "submit_invoice_request",
    "create_service_ticket",
    "schedule_service",
}

VALID_ERROR_CODES = {code.value for code in ErrorCode}


def test_backend_identifies_itself(backend: MockBackend) -> None:
    """验证：后端必须报告非空的 ``backend_id``——它要进 trace、报告与 `/health`。"""
    assert backend.backend_id
    assert isinstance(backend.backend_id, str)


def test_declares_exactly_the_normalized_tools(backend: MockBackend) -> None:
    """验证：工具集合与归一化清单**完全一致**（不多不少）。

    多一个 → 行业包 `enabled` 可能引用到不存在的实现；少一个 → 换后端就少能力。
    """
    assert {spec.name for spec in backend.list_tools()} == EXPECTED_TOOL_NAMES


def test_knowledge_search_is_not_a_business_tool(backend: MockBackend) -> None:
    """``knowledge_search`` 来自 Core 的检索能力，不属于业务后端。"""
    assert "knowledge_search" not in {spec.name for spec in backend.list_tools()}


def test_write_tools_require_idempotency_key(backend: MockBackend) -> None:
    """验证：写操作必须声明需要幂等键，且写操作集合与清单一致。

    漏声明等于允许「无键重试」，重复退款就从这里开始。
    """
    writes = {spec.name for spec in backend.list_tools() if spec.requires_idempotency_key}
    assert writes == EXPECTED_WRITE_TOOLS


def test_read_tools_are_idempotent_and_not_confirmation_required(backend: MockBackend) -> None:
    """读操作必须幂等且不需要人工确认（``escalate_to_human`` 属上报动作，单列验证）。"""
    for spec in backend.list_tools():
        if spec.requires_idempotency_key or spec.name == "escalate_to_human":
            continue
        assert spec.idempotent is True, f"读操作 {spec.name} 应当是幂等的"
        assert spec.confirmation_required is False, f"读操作 {spec.name} 不应要求人工确认"


def test_escalation_is_neither_idempotent_nor_a_write(backend: MockBackend) -> None:
    """升级人工是「上报」语义：重复上报无害，但不应被当成幂等写操作。"""
    spec = next(s for s in backend.list_tools() if s.name == "escalate_to_human")
    assert spec.idempotent is False
    assert spec.requires_idempotency_key is False


def test_high_risk_writes_require_confirmation(backend: MockBackend) -> None:
    """验证：``risk=high`` 的工具必须声明 ``confirmation_required``。

    这是「高危操作不能静默执行」的声明层保证，与规则引擎的 require_confirmation 是两道闸。
    """
    for spec in backend.list_tools():
        if spec.risk == "high":
            assert spec.confirmation_required is True, f"高风险工具 {spec.name} 必须走人工确认"


def test_every_spec_has_valid_json_schema(backend: MockBackend) -> None:
    """验证：每个声明都有描述、正超时、合法风险等级，且 ``required`` 字段真的在 ``properties`` 里。

    最后一条容易漏：schema 自相矛盾时模型能构造出「必填但没定义」的参数。
    """
    for spec in backend.list_tools():
        assert spec.description, f"{spec.name} 缺少描述"
        assert spec.timeout_s > 0, f"{spec.name} 的超时必须为正数"
        assert spec.risk in {"low", "medium", "high"}, f"{spec.name} 的风险等级非法"
        schema = spec.parameters
        assert schema.get("type") == "object", f"{spec.name} 的参数必须是 JSON Schema 对象"
        assert set(schema.get("properties", {})) >= set(
            schema.get("required", [])
        ), f"{spec.name} 的 required 字段必须都在 properties 中"


def test_every_tool_can_be_invoked_without_raising(backend: MockBackend, ctx: ToolContext) -> None:
    """缺参调用必须返回 ``ToolResult``，而不是抛异常（编排层不应被异常打断）。

    **没有必填参数的工具**（如 ``search_products`` 的参数全是收窄条件）用空参数调用
    可以成功——契约要求的是「不抛异常」与「必填缺失要报错」，不是「空参数一律失败」。
    """
    for spec in backend.list_tools():
        result = backend.invoke(spec.name, {}, ctx)
        assert isinstance(result, ToolResult), f"{spec.name} 未返回 ToolResult"
        if spec.parameters.get("required"):
            assert result.ok is False, f"{spec.name} 在缺参时不应返回成功"
            assert result.error_code in VALID_ERROR_CODES, (
                f"{spec.name} 返回了非法错误码：{result.error_code}"
            )


def test_declared_required_args_are_enforced(backend: MockBackend, ctx: ToolContext) -> None:
    """声明为必填的参数若缺失，必须报 ``INVALID_ARGUMENT``。"""
    for spec in backend.list_tools():
        if not spec.parameters.get("required"):
            continue
        result = backend.invoke(spec.name, {}, ctx)
        assert result.error_code == ErrorCode.INVALID_ARGUMENT.value, (
            f"{spec.name} 缺少必填参数时应报 INVALID_ARGUMENT，实际 {result.error_code}"
        )


def test_specs_are_returned_as_copies(backend: MockBackend) -> None:
    """验证：``list_tools()`` 返回副本——调用方 clear 掉不能影响后端自身声明。"""
    first = backend.list_tools()
    first.clear()
    assert len(backend.list_tools()) == len(EXPECTED_TOOL_NAMES), "调用方不应能清空后端声明"


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOL_NAMES))
def test_each_tool_is_addressable_by_name(backend: MockBackend, tool_name: str) -> None:
    """验证：清单里的每个工具名都能在声明中被找到（参数化，一条对应一个工具）。"""
    assert any(spec.name == tool_name for spec in backend.list_tools())
