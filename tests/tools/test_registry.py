"""ToolRegistry 与行业包元数据覆盖的行为验证。"""

from __future__ import annotations

import pytest

from zkagent.backends.mock.backend import MockBackend
from zkagent.config import (
    IndustryPackError,
    load_industry_pack,
    load_tools_config,
    pack_file,
)
from zkagent.core.rag.knowledge_tool import KNOWLEDGE_TOOL_NAME, KnowledgeSearchTool
from zkagent.core.tools.composite import CompositeBackend
from zkagent.core.tools.registry import (
    ToolRegistry,
    apply_industry_overrides,
    missing_enabled_tools,
)
from zkagent.core.types import IndustryToolsConfig, ToolSpec


def _spec(name: str, **kwargs) -> ToolSpec:
    return ToolSpec(name=name, description=f"{name} 工具", **kwargs)


# --------------------------------------------------------------------- 注册与查询


def test_register_get_and_listing() -> None:
    """验证：注册后可按名取回，且 ``names()`` 按名称排序（输出稳定），查不到返回 ``None``。"""
    registry = ToolRegistry()
    registry.register(_spec("b_tool", tags=["order"]))
    registry.register(_spec("a_tool", tags=["refund"]))

    assert len(registry) == 2
    assert registry.names() == ["a_tool", "b_tool"], "工具名应按名称排序，保证输出稳定"
    assert registry.get("a_tool") is not None
    assert registry.get("missing") is None
    assert "a_tool" in registry


def test_duplicate_registration_fails_loudly() -> None:
    """验证：重复注册同名工具直接抛错——静默覆盖会让两个后端实现互相打架。"""
    registry = ToolRegistry()
    registry.register(_spec("dup"))
    with pytest.raises(ValueError, match="重复注册"):
        registry.register(_spec("dup"))


def test_get_returns_copy_not_shared_reference() -> None:
    """验证：``get`` 返回副本而非共享引用——调用方改 tags 不得污染注册表。"""
    registry = ToolRegistry()
    registry.register(_spec("t", tags=["order"]))
    fetched = registry.get("t")
    assert fetched is not None
    fetched.tags.append("mutated")
    assert registry.get("t").tags == ["order"], "返回的声明不应与注册表共享引用"


# --------------------------------------------------------------------- 筛选


def test_select_by_tags() -> None:
    """验证：按 tags 筛选取并集，且结果仍按名称排序（多选时顺序稳定）。"""
    registry = ToolRegistry()
    registry.register_many([
        _spec("order_read", tags=["order"]),
        _spec("refund_write", tags=["refund", "risky"]),
        _spec("kb_search", tags=["kb"]),
    ])
    assert [s.name for s in registry.select(tags=["order"])] == ["order_read"]
    assert [s.name for s in registry.select(tags=["refund", "kb"])] == ["kb_search", "refund_write"]


def test_select_by_permissions() -> None:
    """验证：按权限筛选——无权限者只看到公开工具，有 ``refund:approve`` 者多看到受限工具。"""
    registry = ToolRegistry()
    registry.register_many([
        _spec("open_tool"),
        _spec("scoped_tool", permissions=["refund:approve"]),
    ])
    assert [s.name for s in registry.select(permissions=[])] == ["open_tool"]
    assert [s.name for s in registry.select(permissions=["refund:approve"])] == [
        "open_tool",
        "scoped_tool",
    ]


def test_select_by_intent_and_max_tools() -> None:
    """验证：按意图筛选，``max_tools`` 是**硬上限**；无匹配返回空列表而非全部。"""
    registry = ToolRegistry()
    registry.register_many([_spec("order_a", tags=["order"]), _spec("order_b", tags=["order"])])
    assert len(registry.select(intent="order")) == 2
    assert len(registry.select(intent="order", max_tools=1)) == 1
    assert registry.select(intent="nothing_matches") == []


def test_select_without_filters_returns_everything() -> None:
    """验证：不带任何筛选条件时返回全部工具（默认不过滤，收窄必须显式声明）。"""
    registry = ToolRegistry()
    registry.register_many([_spec("a"), _spec("b")])
    assert len(registry.select()) == 2


# --------------------------------------------------------------------- 行业包覆盖


def test_apply_industry_overrides_filters_tags_and_metadata(pack_dir) -> None:
    """验证：行业包 ``tools.yaml`` 的 overrides 能改写 timeout 与 tags。

    后端未声明的启用项会被忽略（由 ``missing_enabled_tools`` 单独报警，不在覆盖里补造）。
    """
    config = load_industry_pack(pack_dir)
    tools_config = load_tools_config(config, pack_dir)
    specs = [_spec("get_order", tags=["order"]), _spec("create_service_ticket", tags=["service"])]

    applied = {spec.name: spec for spec in apply_industry_overrides(specs, tools_config)}

    assert "create_service_ticket" in applied
    # 后端未声明的启用项会被忽略（由 missing_enabled_tools 负责报警）
    assert applied["create_service_ticket"].timeout_s == 15.0
    assert "write" in applied["create_service_ticket"].tags


def test_missing_enabled_tools_reports_gap(pack_dir) -> None:
    """验证：``enabled`` 里声明了但后端没实现的工具会被报出来（且排序后返回）。

    这是「换行业包不改 Core」的守门人：声明与实现不一致要在启动时暴露。
    """
    config = load_industry_pack(pack_dir)
    tools_config = load_tools_config(config, pack_dir)
    missing = missing_enabled_tools([_spec("get_order")], tools_config)
    assert "create_refund_ticket" in missing
    assert missing == sorted(missing)


def test_unknown_override_field_is_rejected() -> None:
    """``tools.yaml`` 写了不存在的字段时必须报错，避免「配置写了但不生效」。"""
    broken = IndustryToolsConfig(
        enabled=["get_order"], overrides={"get_order": {"nonexistent_field": 1}}
    )
    with pytest.raises(ValueError, match="未知字段"):
        apply_industry_overrides([_spec("get_order")], broken)


# --------------------------------------------------------------------- 与真实后端对齐


def test_registry_matches_backend_and_pack(backend: MockBackend, pack_dir) -> None:
    """行业包 ``enabled`` 必须与「业务后端 + RAG 工具」的**合并**声明对齐。

    P3 起 ``knowledge_search`` 由 Core 的 RAG 提供（不是业务 API），因此：
    - 业务后端单独看会缺这一个工具（这正是合并后端的意义）；
    - 合并后的声明必须与 ``enabled`` 完全一致，且缺一即报错。
    """
    config = load_industry_pack(pack_dir)
    tools_config = load_tools_config(config, pack_dir)

    assert missing_enabled_tools(backend.list_tools(), tools_config) == [KNOWLEDGE_TOOL_NAME]

    tool = KnowledgeSearchTool(lambda: None)  # type: ignore[arg-type]
    combined = CompositeBackend(backend, [tool])
    assert missing_enabled_tools(combined.list_tools(), tools_config) == []

    registry = ToolRegistry()
    registry.register_many(apply_industry_overrides(combined.list_tools(), tools_config))
    assert set(registry.names()) == set(tools_config.enabled)
    assert len(registry) == 15


def test_injection_limit_is_configured(pack_dir) -> None:
    """行业包必须给出注入上限与过滤维度（P5 会用意图过滤把上限收紧）。"""
    config = load_industry_pack(pack_dir)
    tools_config = load_tools_config(config, pack_dir)
    assert tools_config.injection.max_tools >= 1
    assert tools_config.injection.max_tools <= len(tools_config.enabled)
    assert "tags" in tools_config.injection.prefilter_by


def test_missing_tools_yaml_fails_loudly(tmp_path, pack_dir) -> None:
    """``tools.yaml`` 缺失时必须显式报错，而不是静默返回空配置。"""
    config = load_industry_pack(pack_dir)
    with pytest.raises(IndustryPackError):
        load_tools_config(config, tmp_path)


def test_pack_file_helper_resolves_declared_path(pack_dir) -> None:
    """验证：``pack_file`` 能按包内声明的相对路径解析出真实文件（路径契约有效）。"""
    config = load_industry_pack(pack_dir)
    assert pack_file(pack_dir, config.tools_file).exists()
