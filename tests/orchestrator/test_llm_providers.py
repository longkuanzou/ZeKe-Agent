"""模型供应登记表的单元测试（框架无关：本文件**不 import** 任何框架）。

覆盖四件事：

1. 登记表自身的完整性（id 唯一、协议合法、需要密钥就必须声明变量名）；
2. ``auto`` 选择规则（只挑「密钥就位」的供应商，本地端点不参与）；
3. 句柄是纯数据且**不含密钥值**（只含环境变量名）；
4. 「不猜模型名 / 不猜地址」——缺模型或缺端点时必须显式报错。
"""

from __future__ import annotations

import json

import pytest

from zkagent.config import load_settings, provider_catalog_with_keys, settings_key_env
from zkagent.core.llm import (
    AUTO_PRIORITY,
    PROVIDER_REGISTRY,
    FakeProvider,
    ProviderConfigError,
    RegistryProvider,
    auto_select_provider,
    provider_catalog,
    provider_ids,
    provider_spec,
)
from zkagent.core.types import ProviderSpec

#: 测试用密钥值：只用于断言「它不该出现在句柄 / 视图里」。
FAKE_KEY = "unit-test-key-value"

KEY_ENV_NAMES = (
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "DASHSCOPE_API_KEY",
    "ZHIPUAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture
def clean_key_env(monkeypatch):
    """清空所有供应商密钥环境变量，保证断言不受开发机环境影响。"""
    for name in KEY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# --------------------------------------------------------------------------- 登记表


def test_registry_ids_are_sorted_and_unique() -> None:
    """验证：``provider_ids()`` 去重且有序，CLI 输出稳定、便于比对。"""
    assert provider_ids() == sorted(PROVIDER_REGISTRY)
    assert len(set(provider_ids())) == len(provider_ids())


def test_every_spec_is_self_consistent() -> None:
    """验证：每条登记项自洽——有展示名、协议合法、需密钥就必须声明环境变量名。"""
    for spec in PROVIDER_REGISTRY.values():
        assert isinstance(spec, ProviderSpec)
        assert spec.label.strip(), f"{spec.provider_id} 缺少展示名"
        assert spec.protocol in {"openai", "anthropic", "deepseek"}
        if spec.requires_api_key:
            assert spec.api_key_env, f"{spec.provider_id} 需要密钥却没声明环境变量名"
        if spec.requires_base_url:
            assert spec.base_url is None, "requires_base_url 表示地址必须由用户提供"
        if spec.default_model is not None:
            assert spec.default_model.strip()


def test_required_vendors_are_registered() -> None:
    """用户点名要求的几类供应商必须在册。"""
    for provider_id in ("qwen", "zhipu", "openai", "anthropic", "ollama", "custom", "deepseek"):
        assert provider_id in PROVIDER_REGISTRY
    # 除 Anthropic 外全部走 OpenAI 兼容协议（同一段接缝代码）
    assert PROVIDER_REGISTRY["qwen"].protocol == "openai"
    assert PROVIDER_REGISTRY["zhipu"].protocol == "openai"
    assert PROVIDER_REGISTRY["ollama"].protocol == "openai"
    assert PROVIDER_REGISTRY["custom"].protocol == "openai"
    assert PROVIDER_REGISTRY["anthropic"].protocol == "anthropic"


def test_auto_priority_only_contains_keyed_providers() -> None:
    """本地端点不参与 auto：不能假设用户机器上一定跑着 Ollama / vLLM。"""
    for provider_id in AUTO_PRIORITY:
        assert provider_id in PROVIDER_REGISTRY
        assert PROVIDER_REGISTRY[provider_id].requires_api_key is True
    assert "ollama" not in AUTO_PRIORITY
    assert "custom" not in AUTO_PRIORITY


# --------------------------------------------------------------------------- auto 选择


def test_auto_selects_first_keyed_provider(clean_key_env) -> None:
    """验证：``auto`` 按 ``AUTO_PRIORITY`` 选第一个密钥就位的供应商。"""
    assert auto_select_provider({"DEEPSEEK_API_KEY": "x"}) == "deepseek"
    assert (
        auto_select_provider({"DEEPSEEK_API_KEY": "x", "DASHSCOPE_API_KEY": "y"}) == "deepseek"
    ), "顺序由 AUTO_PRIORITY 决定"
    assert auto_select_provider({"DASHSCOPE_API_KEY": "y"}) == "qwen"
    assert auto_select_provider({"ANTHROPIC_API_KEY": "z"}) == "anthropic"


def test_auto_returns_none_when_nothing_configured(clean_key_env) -> None:
    """验证：一个密钥都没有时返回 ``None``——落到 fake 由调用方决定，这里不瞎猜。"""
    assert auto_select_provider({}) is None


# --------------------------------------------------------------------------- 句柄纯净性


def test_handle_never_contains_secret_value(clean_key_env) -> None:
    """句柄只带「环境变量名」，绝不带密钥值。"""
    clean_key_env.setenv("DASHSCOPE_API_KEY", FAKE_KEY)
    handle = RegistryProvider("qwen").chat_model()
    assert handle.api_key_env == "DASHSCOPE_API_KEY"
    assert FAKE_KEY not in handle.model_dump_json()


def test_handle_carries_transport_fields_as_typed_fields() -> None:
    """验证：传输信息（模型 / 协议 / 端点 / 是否需密钥）是显式字段，``params`` 只留采样参数。"""
    handle = RegistryProvider("qwen").chat_model()
    assert handle.provider_id == "qwen"
    assert handle.model_id == "qwen-plus"
    assert handle.protocol == "openai"
    assert (handle.base_url or "").startswith("https://dashscope")
    assert handle.requires_api_key is True
    # params 只承载采样参数与 provider 私有载荷，不再塞传输信息
    assert set(handle.params) == {"temperature", "timeout_s"}


def test_model_override_wins_over_default() -> None:
    """验证：显式传入的模型名覆盖登记表默认值。"""
    assert RegistryProvider("qwen", "qwen-max").chat_model().model_id == "qwen-max"


def test_provider_without_default_model_demands_explicit_model() -> None:
    """不给默认模型是**有意**的：宁可笑你显式指定，也不猜一个可能已下线的名字。"""
    with pytest.raises(ProviderConfigError, match="必须显式指定模型"):
        RegistryProvider("openai")
    with pytest.raises(ProviderConfigError, match="必须显式指定模型"):
        RegistryProvider("anthropic")


def test_custom_provider_demands_base_url() -> None:
    """验证：``custom`` 必须给 ``base_url``；给了就允许无密钥（自建网关常不校验）。"""
    with pytest.raises(ProviderConfigError, match="base_url"):
        RegistryProvider("custom", "some-local-model")

    handle = RegistryProvider(
        "custom", "some-local-model", base_url="http://localhost:8000/v1"
    ).chat_model()
    assert handle.base_url == "http://localhost:8000/v1"
    assert handle.requires_api_key is False, "自建网关常常不校验密钥"


def test_local_provider_needs_no_key() -> None:
    """验证：本地端点（ollama）不需要密钥，也不声明环境变量名。"""
    handle = RegistryProvider("ollama", "qwen2.5:7b").chat_model()
    assert handle.requires_api_key is False
    assert handle.api_key_env is None


def test_unknown_provider_lists_alternatives() -> None:
    """验证：未知供应商报错并列出可选项，方便 CLI 用户自查拼写。"""
    with pytest.raises(ProviderConfigError, match="未知的模型供应商"):
        provider_spec("no-such-vendor")


def test_fake_handle_is_marked_as_fake() -> None:
    """验证：fake 句柄显式标记 ``protocol=fake`` 且不需密钥——不伪装成真实模型。"""
    handle = FakeProvider([{"content": "hi"}]).chat_model()
    assert handle.protocol == "fake"
    assert handle.requires_api_key is False
    assert handle.api_key_env is None


# --------------------------------------------------------------------------- 配置视图


def test_settings_key_env_collects_only_api_key_fields(clean_key_env) -> None:
    """验证：从配置收集密钥时只取 ``*_API_KEY`` 字段，不夹带其它配置项。"""
    settings = load_settings(dashscope_api_key=FAKE_KEY)
    view = settings_key_env(settings)
    assert view.get("DASHSCOPE_API_KEY") == FAKE_KEY
    assert all(name.endswith("_API_KEY") for name in view), "只应收集密钥字段"


def test_settings_key_env_skips_fields_left_empty(clean_key_env) -> None:
    """验证：空密钥不进视图，否则会被误判成「已配置」。"""
    view = settings_key_env(load_settings())
    assert all(value for value in view.values()), "空值不得进入密钥视图"
    assert set(view) <= set(KEY_ENV_NAMES), "视图里只能是已知的密钥变量名"


def test_catalog_never_exposes_secret_values(clean_key_env) -> None:
    """验证：供应视图里绝不出现密钥值，只报 ``key_present`` 布尔与变量名。"""
    clean_key_env.setenv("DASHSCOPE_API_KEY", FAKE_KEY)
    catalog = provider_catalog_with_keys(load_settings())
    assert FAKE_KEY not in json.dumps(catalog)

    qwen = next(entry for entry in catalog if entry["provider_id"] == "qwen")
    assert qwen["key_present"] is True
    assert qwen["api_key_env"] == "DASHSCOPE_API_KEY"

    anthropic = next(entry for entry in catalog if entry["provider_id"] == "anthropic")
    assert anthropic["key_present"] is False


def test_catalog_reports_protocol_and_defaults() -> None:
    """验证：视图如实报告协议与默认模型（``custom`` 不需要密钥）。"""
    catalog = {entry["provider_id"]: entry for entry in provider_catalog({})}
    assert catalog["qwen"]["protocol"] == "openai"
    assert catalog["qwen"]["default_model"] == "qwen-plus"
    assert catalog["openai"]["default_model"] is None
    assert catalog["custom"]["requires_api_key"] is False
