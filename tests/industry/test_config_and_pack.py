"""行业包数据、组合根配置、渠道能力隔离与 Tracer 脱敏的校验。

本阶段（P1）只验证「数据可被解析与校验」——**不做规则判定**（属 P4）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from zkagent.config import (
    IndustryPackError,
    default_industry_packs_dir,
    load_industry_pack,
    load_policy_rules,
    load_settings,
    load_tools_config,
    pack_file,
    resolve_paths,
)
from zkagent.core.interfaces import ChannelAdapter
from zkagent.core.observability.tracer import Tracer
from zkagent.core.types import InboundMessage, OutboundMessage

PACK_DIR = default_industry_packs_dir() / "appliance"

#: 归一化标准工具中由**业务后端**提供的 12 个；``knowledge_search`` 自 P3 起由 Core 的 RAG 提供。
EXPECTED_TOOLS = {
    "get_user",
    "verify_user",
    "search_products",
    "get_product",
    "get_order",
    "list_orders",
    "cancel_order",
    "create_refund_ticket",
    "get_refund_ticket",
    "approve_refund",
    "create_service_ticket",
    "schedule_service",
    "get_logistics",
    "escalate_to_human",
}


class _CardChannel(ChannelAdapter):
    """测试替身：声明支持卡片的渠道。"""

    channel_id = "dummy-card"
    _DEFAULT_CAPABILITIES = {"card": True, "typing": True, "async_callback": False}

    def receive(self) -> InboundMessage:  # pragma: no cover - 本用例不涉及收取
        return InboundMessage(session_id="s", text="")

    def send(self, msg: OutboundMessage) -> None:  # pragma: no cover - 本用例不涉及发送
        return None


# --------------------------------------------------------------------- 行业包


def test_appliance_pack_dir_exists() -> None:
    """默认行业包目录必须存在（换行业即换此目录）。"""
    assert PACK_DIR.is_dir(), f"缺少行业包目录：{PACK_DIR}"


def test_pack_yaml_loads_and_validates() -> None:
    """验证：``pack.yaml`` 能加载并通过校验（id / 名称 / 产品范围 / 引用文件后缀）。"""
    config = load_industry_pack(PACK_DIR)
    assert config.id == "appliance"
    assert config.name
    assert config.products, "行业包必须声明产品范围"
    assert config.policies_file.endswith(".yaml")
    assert config.prompts.system.endswith(".md")


def test_pack_referenced_files_exist() -> None:
    """pack.yaml 里声明的每个文件都必须真实存在，避免“配置指向空气”。"""
    config = load_industry_pack(PACK_DIR)
    must_exist = [
        config.tools_file,
        config.policies_file,
        config.prompts.system,
        config.seed_data,
    ]
    for relative in must_exist:
        assert pack_file(PACK_DIR, relative).exists(), f"pack.yaml 声明的路径不存在：{relative}"
    if config.prompts.fewshots:
        assert pack_file(PACK_DIR, config.prompts.fewshots).exists()


def test_cli_tools_list_sees_rag_provided_tool(capsys) -> None:
    """回归用例：`zkagent tools list` 必须基于**组合后端**列工具。

    P3 把 ``knowledge_search`` 加进 ``tools.yaml.enabled`` 后，该命令仍用单独的
    ``MockBackend`` 做校验，于是误报「启用的工具未提供」并返回退出码 3，
    直接打破了 P2 Gate 里的 ``tools list --tag order``。此用例锁住该行为。
    """
    from apps.cli import main

    assert main(["tools", "list"]) == 0

    payload = json.loads(capsys.readouterr().out)
    names = {tool["name"] for tool in payload["tools"]}
    assert "knowledge_search" in names, "RAG 提供的工具必须出现在清单里"
    assert payload["registered"] == len(names)


def test_tools_yaml_loads_and_matches_enabled_scope() -> None:
    """验证：``tools.yaml`` 的启用集合与后端能力一致，且注入上限不小于启用数。"""
    config = load_industry_pack(PACK_DIR)
    tools = load_tools_config(config, PACK_DIR)

    # 业务工具 + RAG 工具（knowledge_search 自 P3 起可用）
    assert set(tools.enabled) == EXPECTED_TOOLS | {"knowledge_search"}
    assert tools.injection.max_tools >= len(tools.enabled), (
        "注入上限不应小于启用工具数，否则会静默丢掉工具"
    )
    assert "tags" in tools.injection.prefilter_by
    # overrides 里的工具必须在 enabled 中声明
    assert set(tools.overrides) <= set(tools.enabled)


def test_rules_yaml_loads_with_unique_ids_and_valid_effects() -> None:
    """验证：规则能加载、id 唯一、effect 合法，并按 priority 升序返回。"""
    config = load_industry_pack(PACK_DIR)
    rules = load_policy_rules(config, PACK_DIR)
    assert rules, "行业包必须至少声明一条规则"
    ids = [rule.id for rule in rules]
    assert len(ids) == len(set(ids)), "规则 id 必须唯一"
    allowed_effects = {"allow", "deny", "require_confirmation", "escalate"}
    for rule in rules:
        assert rule.effect in allowed_effects, f"非法 effect：{rule.effect}"
        assert rule.match, f"规则 {rule.id} 缺少匹配条件"
    # 返回结果须按 priority 升序（数值小者优先）
    assert [rule.priority for rule in rules] == sorted(rule.priority for rule in rules)


def test_policy_engine_is_not_implemented_in_p1() -> None:
    """P1 只写规则数据；确认代码里没有把判定逻辑偷偷塞进来。"""
    rule_source = (PACK_DIR / "policies/rules.yaml").read_text(encoding="utf-8")
    assert "refresh" not in rule_source.lower()
    # 规则文件应当是纯数据：不允许出现可执行代码标记
    assert "!!python" not in rule_source


# --------------------------------------------------------------------- 种子数据


def test_seed_data_is_parseable_and_complete() -> None:
    """验证：种子数据可解析且自洽——订单引用真实用户、金额为整数分、PII 已脱敏。"""
    config = load_industry_pack(PACK_DIR)
    seed_dir = pack_file(PACK_DIR, config.seed_data)
    files = {path.name for path in seed_dir.glob("*.yaml")}
    assert {"users.yaml", "orders.yaml", "logistics.yaml", "service_tickets.yaml"} <= files

    users = yaml.safe_load((seed_dir / "users.yaml").read_text(encoding="utf-8"))["users"]
    orders = yaml.safe_load((seed_dir / "orders.yaml").read_text(encoding="utf-8"))["orders"]

    user_ids = {user["user_id"] for user in users}
    allowed_status = {"created", "paid", "shipped", "delivered", "cancelled"}
    for order in orders:
        assert order["user_id"] in user_ids, f"订单 {order['order_id']} 的用户不存在"
        assert isinstance(order["amount_cents"], int), "金额必须是整数分"
        assert order["amount_cents"] > 0
        assert order["status"] in allowed_status

    # PII 只存脱敏值
    for user in users:
        assert "*" in user["phone_masked"], "手机号必须脱敏存储"
    for order in orders:
        assert "*" in order["address_masked"], "地址必须脱敏存储"


def test_seed_data_covers_required_scenarios() -> None:
    """种子数据必须覆盖后续验收用例所需的场景（否则 P3/P4 会返工造数据）。"""
    config = load_industry_pack(PACK_DIR)
    orders = {
        order["order_id"]: order
        for order in yaml.safe_load(
            pack_file(PACK_DIR, config.seed_data + "orders.yaml").read_text(encoding="utf-8")
        )["orders"]
    }
    assert orders["A1001"]["status"] == "delivered"
    assert orders["A1001"]["amount_cents"] > 100000, "A1001 应为高价订单（转人工用）"
    assert orders["A1002"]["status"] == "created", "A1002 应为未发货订单（Poka-yoke 用）"
    assert orders["A1003"]["amount_cents"] <= 100000, "A1003 应为低价订单（自动退款用）"
    assert orders["A1004"]["user_id"] != orders["A1001"]["user_id"], "A1004 必须归属他人（越权用）"


# --------------------------------------------------------------------- 组合根


def test_pack_loader_fails_loudly_on_missing_file(tmp_path: Path) -> None:
    """验证：行业包目录缺 ``pack.yaml`` 时显式报错，不返回一个空配置。"""
    with pytest.raises(IndustryPackError):
        load_industry_pack(tmp_path)


def test_settings_and_path_resolution(tmp_path: Path) -> None:
    """验证：``resolve_paths`` 产出绝对路径并按需创建目录，行业包目录指向默认包。"""
    settings = load_settings(project_root=tmp_path, db_path=Path("sub/business.db"))
    paths = resolve_paths(settings)
    assert paths.db_path == (tmp_path / "sub/business.db").resolve()
    assert paths.db_path.parent.is_dir(), "resolve_paths 应按需创建目录"
    assert paths.industry_pack_dir == PACK_DIR


def test_env_example_has_no_secret_values() -> None:
    """``.env.example`` 中凡是密钥类变量都必须留空（防止密钥误提交）。

    非敏感的开关 / 路径类变量允许带默认值（如 ``LANGSMITH_TRACING=false``）。
    """
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    assert example.strip(), ".env.example 不应为空"
    secret_like = re.compile(r"(API_KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)
    checked = 0
    for line in example.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if secret_like.search(key):
            assert value == "", f".env.example 中密钥类变量 {key} 必须留空，实际为 {value!r}"
            checked += 1
    assert checked > 0, "未在 .env.example 中找到任何密钥类变量，占位约定形同虚设"


# --------------------------------------------------------------------- 渠道能力隔离


def test_channel_capabilities_are_instance_scoped() -> None:
    """可变类属性不得在实例间共享（DESIGN §7.2 的既有约束）。"""
    first = _CardChannel()
    second = _CardChannel()

    first.capabilities["card"] = False

    assert second.capabilities["card"] is True, "实例之间共享了 capabilities 字典"
    assert _CardChannel._DEFAULT_CAPABILITIES["card"] is True, "类级常量被污染"


def test_channel_capabilities_can_be_overridden_per_instance() -> None:
    """验证：实例级覆盖只改自己，未覆盖的能力仍沿用类级默认值。"""
    channel = _CardChannel(capabilities={"typing": False, "async_callback": True})
    assert channel.capabilities == {"card": True, "typing": False, "async_callback": True}
    assert channel.channel_id == "dummy-card"


# --------------------------------------------------------------------- Tracer


def test_tracer_redacts_sensitive_fields_and_flushes(tmp_path: Path) -> None:
    """验证：trace 落盘前把敏感字段（手机号 / 地址）替换成 ``***``，非敏感字段原样保留。"""
    tracer = Tracer(tmp_path)
    trace_id = tracer.new_trace_id()
    tracer.record(
        trace_id,
        "tool_call",
        tool="get_user",
        phone="13800001234",
        args={"address": "北京市朝阳区", "order_id": "A1001"},
    )
    tracer.record(trace_id, "tool_result", ok=True, latency_ms=3)

    events = tracer.events(trace_id)
    assert events[0]["phone"] == "***"
    assert events[0]["args"]["address"] == "***"
    assert events[0]["args"]["order_id"] == "A1001", "非敏感字段不应被脱敏"

    path = tracer.flush(trace_id)
    assert path is not None and path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["trace_id"] == trace_id
    assert len(payload["events"]) == 2


def test_tracer_truncates_long_values(tmp_path: Path) -> None:
    """验证：超长字段被截断并标注，避免单条事件把 trace 撑爆。"""
    tracer = Tracer(tmp_path, max_value_len=10)
    trace_id = tracer.new_trace_id()
    tracer.record(trace_id, "blob", note="x" * 100)
    assert "截断" in tracer.events(trace_id)[0]["note"]
