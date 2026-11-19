"""模型供给：把所有模型供应商收进**一张登记表**，只产出 ``ChatModelHandle``（纯数据）。

设计要点
--------

1. **供应商差异不写进代码分支**：新增一家供应商 = 在 ``PROVIDER_REGISTRY`` 里加一行数据；
   接缝只按 ``protocol`` 解包（``openai`` 兼容 / ``anthropic`` / ``deepseek`` 官方 / ``fake``）。
2. **OpenAI 兼容协议是主力**：DeepSeek、通义、智谱、Moonshot、本地 vLLM / Ollama /
   LM Studio / 自建网关都提供 OpenAI 兼容端点，差异只是 ``base_url``；
   ``custom`` 登记项专门承载「任意兼容端点」。
3. **不猜模型名**：``default_model`` 为空表示该供应商模型名迭代太快，必须由用户显式指定，
   否则显式报错——比给一个可能已下线的默认值更诚实。
4. **密钥不进入句柄**：句柄只带「从哪个环境变量读密钥」的名字，读取发生在接缝内。

本模块**不 import** langchain / langgraph；「句柄 → 框架模型」的解包在
``core/orchestrator/langgraph_adapter.py`` 内完成。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from zkagent.core.interfaces import LLMProvider
from zkagent.core.types import ChatModelHandle, ProviderSpec

__all__ = [
    "ProviderConfigError",
    "PROVIDER_REGISTRY",
    "AUTO_PRIORITY",
    "provider_ids",
    "provider_spec",
    "provider_catalog",
    "auto_select_provider",
    "RegistryProvider",
    "DeepSeekProvider",
    "FakeProvider",
    "ScriptStep",
    "tool_call_step",
    "answer_step",
    "result_step",
]


class ProviderCallError(RuntimeError):
    """模型调用失败（网络 / 鉴权 / 限流 / SDK 异常），对外只给可读原因。"""

    code = "MODEL_CALL_FAILED"


class ProviderConfigError(ValueError):
    """供应商配置不完整（未知供应商 / 缺模型 id / 缺 base_url）——**必须显式失败**。"""


#: 内置模型供应登记表：**唯一**的供应商事实来源（密钥值不在其中，只有变量名）。
PROVIDER_REGISTRY: dict[str, ProviderSpec] = {
    spec.provider_id: spec
    for spec in (
        ProviderSpec(
            provider_id="deepseek",
            label="DeepSeek（官方集成）",
            protocol="deepseek",
            api_key_env="DEEPSEEK_API_KEY",
            default_model="deepseek-chat",
        ),
        ProviderSpec(
            provider_id="openai",
            label="OpenAI / ChatGPT",
            protocol="openai",
            api_key_env="OPENAI_API_KEY",
            # 模型名迭代快：不设默认值，必须显式指定（避免猜一个已下线的模型）
            default_model=None,
        ),
        ProviderSpec(
            provider_id="qwen",
            label="通义千问（DashScope OpenAI 兼容端点）",
            protocol="openai",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key_env="DASHSCOPE_API_KEY",
            default_model="qwen-plus",
        ),
        ProviderSpec(
            provider_id="zhipu",
            label="智谱 GLM（OpenAI 兼容端点）",
            protocol="openai",
            base_url="https://open.bigmodel.cn/api/paas/v4",
            api_key_env="ZHIPUAI_API_KEY",
            default_model="glm-4-plus",
        ),
        ProviderSpec(
            provider_id="anthropic",
            label="Anthropic Claude（Messages 协议）",
            protocol="anthropic",
            api_key_env="ANTHROPIC_API_KEY",
            default_model=None,
        ),
        ProviderSpec(
            provider_id="ollama",
            label="本地 Ollama（无需密钥）",
            protocol="openai",
            base_url="http://localhost:11434/v1",
            api_key_env=None,
            requires_api_key=False,
            default_model=None,
        ),
        ProviderSpec(
            provider_id="custom",
            label="任意 OpenAI 兼容端点（必须配合 --base-url 与 --model）",
            protocol="openai",
            api_key_env="OPENAI_API_KEY",
            requires_api_key=False,  # 自建网关常常不校验密钥
            requires_base_url=True,
            default_model=None,
        ),
    )
}

#: ``--provider auto`` 的探测顺序。
#:
#: **只包含需要密钥的登记项**：本地端点（ollama / custom）不参与自动选择，
#: 因为「本机跑着 Ollama」是不能假设的。
AUTO_PRIORITY: tuple[str, ...] = ("deepseek", "openai", "qwen", "zhipu", "anthropic")

#: Fake 脚本的一步，三种形态之一：
#: - ``{"tool_calls": [{"name": str, "args": dict}]}``：发起工具调用；
#: - ``{"content": str}``：直接给固定文本；
#: - ``{"content_from_tool": str}``：以给定前缀开头，**拼接真实工具结果**渲染回答
#:   （强烈推荐用于最终回答：工具失败时不会谎报成功）。
ScriptStep = dict[str, Any]


# --------------------------------------------------------------------------- 登记表查询


def provider_ids() -> list[str]:
    """全部内置供应商标识（供 CLI 的 ``choices`` 与文档使用，顺序稳定）。"""
    return sorted(PROVIDER_REGISTRY)


def provider_spec(provider_id: str) -> ProviderSpec:
    """按 id 取登记项；未知 id 显式报错并列出可选值。"""
    spec = PROVIDER_REGISTRY.get((provider_id or "").strip().lower())
    if spec is None:
        raise ProviderConfigError(
            f"未知的模型供应商 {provider_id!r}；可选：{'、'.join(provider_ids())}"
        )
    return spec


def provider_catalog(env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """登记表的只读投影（**不含任何密钥值**，只报告密钥是否就位）。"""
    source = os.environ if env is None else env
    return [
        {
            "provider_id": spec.provider_id,
            "label": spec.label,
            "protocol": spec.protocol,
            "base_url": spec.base_url,
            "api_key_env": spec.api_key_env,
            "requires_api_key": spec.requires_api_key,
            "default_model": spec.default_model,
            "key_present": bool(spec.api_key_env and source.get(spec.api_key_env)),
        }
        for spec in (PROVIDER_REGISTRY[pid] for pid in provider_ids())
    ]


def auto_select_provider(env: Mapping[str, str] | None = None) -> str | None:
    """按 :data:`AUTO_PRIORITY` 挑选第一个「密钥已就位」的供应商。

    找不到返回 ``None``——由调用方**显式**落到 Fake，不做静默降级。
    """
    source = os.environ if env is None else env
    for provider_id in AUTO_PRIORITY:
        spec = PROVIDER_REGISTRY[provider_id]
        if spec.api_key_env and source.get(spec.api_key_env):
            return provider_id
    return None


# --------------------------------------------------------------------------- 供给实现


class RegistryProvider(LLMProvider):
    """登记表驱动的模型供给：换供应商 = 换一个 ``provider_id``，不新增代码分支。

    :param provider_id: :data:`PROVIDER_REGISTRY` 中的标识。
    :param model_id: 覆盖登记项的 ``default_model``；两者皆空时**显式报错**（不猜模型）。
    :param base_url: 覆盖登记项地址（自建网关 / 本地端点）。
    :param temperature: 采样温度。
    :param timeout_s: 单次调用超时（秒）。
    """

    def __init__(
        self,
        provider_id: str,
        model_id: str | None = None,
        *,
        base_url: str | None = None,
        temperature: float = 0.0,
        timeout_s: float = 60.0,
    ) -> None:
        spec = provider_spec(provider_id)
        self._spec = spec

        resolved_base = (base_url or spec.base_url or "").strip() or None
        if spec.requires_base_url and not resolved_base:
            raise ProviderConfigError(
                f"供应商 {spec.provider_id!r} 必须显式给出 base_url"
                "（例如 --base-url http://localhost:8000/v1）"
            )

        resolved_model = (model_id or spec.default_model or "").strip()
        if not resolved_model:
            raise ProviderConfigError(
                f"供应商 {spec.provider_id!r} 未设置默认模型，必须显式指定模型 id"
                "（例如 --model <模型名>，或设置 ZKAGENT_MODEL_ID）"
            )

        self._model_id = resolved_model
        self._base_url = resolved_base
        self._temperature = temperature
        self._timeout_s = timeout_s

    @property
    def provider_id(self) -> str:
        """登记表标识（如 ``qwen``）。"""
        return self._spec.provider_id

    @property
    def spec(self) -> ProviderSpec:
        """本供给对应的登记项（只读）。"""
        return self._spec

    def chat_model(self) -> ChatModelHandle:
        """返回模型句柄（**不含密钥**，只含读取密钥的环境变量名）。"""
        return ChatModelHandle(
            provider_id=self._spec.provider_id,
            model_id=self._model_id,
            protocol=self._spec.protocol,
            base_url=self._base_url,
            api_key_env=self._spec.api_key_env,
            requires_api_key=self._spec.requires_api_key,
            params={
                "temperature": self._temperature,
                "timeout_s": self._timeout_s,
            },
        )


class DeepSeekProvider(RegistryProvider):
    """DeepSeek 快捷方式：等价于 ``RegistryProvider("deepseek", model_id, ...)``。"""

    def __init__(self, model_id: str = "deepseek-chat", **kwargs: Any) -> None:
        super().__init__("deepseek", model_id, **kwargs)


class FakeProvider(LLMProvider):
    """脚本化 Fake 模型：**无密钥即可跑通全流程**（CI 与本地演示的默认兜底）。

    脚本按调用顺序消费；耗尽后重复最后一步（避免迭代器耗尽抛异常）。因此
    若最后一步是工具调用，图会一路走到 ``max_steps`` 熔断——可用于测试熔断路径。
    """

    provider_id = "fake"

    def __init__(self, script: list[ScriptStep], *, model_id: str = "fake-scripted") -> None:
        if not script:
            raise ValueError("Fake 脚本不能为空")
        self._script = [dict(step) for step in script]
        self._model_id = model_id

    def chat_model(self) -> ChatModelHandle:
        """返回携带脚本的句柄。"""
        return ChatModelHandle(
            provider_id=self.provider_id,
            model_id=self._model_id,
            protocol="fake",
            api_key_env=None,
            requires_api_key=False,
            params={"script": self._script},
        )


def tool_call_step(name: str, args: dict[str, Any]) -> ScriptStep:
    """便捷构造：一次工具调用步骤。"""
    return {"tool_calls": [{"name": name, "args": dict(args)}]}


def answer_step(content: str) -> ScriptStep:
    """便捷构造：一次纯文本回答步骤（固定文本，与工具结果无关）。"""
    return {"content": content}


def result_step(prefix: str = "") -> ScriptStep:
    """便捷构造：按**真实工具结果**渲染回答的步骤。"""
    return {"content_from_tool": prefix}
