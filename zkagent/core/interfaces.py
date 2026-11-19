"""核心抽象接口（ABC）——不含实现、不 import 任何框架。

Core 只依赖本模块中的抽象；具体实现（后端、渠道、检索、模型）由组合根
``zkagent/config.py`` 按配置注入。因此替换任一实现都不需要改动 Core 代码。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from zkagent.core.types import (
    AgentTurnResult,
    ChatModelHandle,
    Chunk,
    InboundMessage,
    OutboundMessage,
    PolicyDecision,
    ToolContext,
    ToolResult,
    ToolSpec,
)

__all__ = [
    "BusinessBackend",
    "ChannelAdapter",
    "PolicyEngine",
    "Retriever",
    "LLMProvider",
    "Embedder",
    "AgentRunner",
]


class BusinessBackend(ABC):
    """业务后端适配器：订单 / 用户 / 工单 / 退款 / 物流等业务 API。

    职责边界（两处吸收后端差异）：
    1. :meth:`list_tools` 把后端能力声明为一串归一化 :class:`ToolSpec`；
    2. :meth:`invoke` 完成「归一化入参 → 后端方言」与「后端错误 → 统一错误码」的双向翻译。

    实现方必须在服务端完成身份、归属、状态、金额与幂等校验——**不得**依赖提示词。
    """

    backend_id: str = "unnamed-backend"

    @abstractmethod
    def list_tools(self) -> list[ToolSpec]:
        """声明本后端能提供的全部归一化工具。"""

    @abstractmethod
    def invoke(self, tool_name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行一次工具调用，返回统一结果（失败必须如实返回，不得编造成功）。"""


class ChannelAdapter(ABC):
    """渠道适配器：消息收发。能力不支持时必须在 ``capabilities`` 中声明。

    注意：``receive()`` 的同步语义只对 CLI 成立。推送式渠道（HTTP / IM 回调）
    应把消息归一化为 :class:`InboundMessage` 后直接交给 ``AgentRunner``，
    Core **不得**假设 ``receive()`` 一定可用。
    """

    channel_id: str = "unnamed-channel"

    #: 渠道默认能力（**只读类级常量**，禁止就地修改；子类在 __init__ 中拷贝实例副本）。
    _DEFAULT_CAPABILITIES: ClassVar[dict[str, bool]] = {
        "card": False,
        "typing": False,
        "async_callback": False,
    }

    def __init__(
        self, channel_id: str | None = None, capabilities: dict[str, bool] | None = None
    ) -> None:
        """初始化渠道路由标识与能力声明。

        通过 ``{**类级常量, **覆盖}`` 生成**实例级**字典，避免可变类属性在多个实例间共享
        （P1 有单测覆盖该隔离性）。
        """
        if channel_id is not None:
            self.channel_id = channel_id
        self.capabilities: dict[str, bool] = {
            **self._DEFAULT_CAPABILITIES,
            **(capabilities or {}),
        }

    @abstractmethod
    def receive(self) -> InboundMessage:
        """拉取一条入站消息（仅拉取式渠道可用）。"""

    @abstractmethod
    def send(self, msg: OutboundMessage) -> None:
        """发送一条出站消息。"""


class PolicyEngine(ABC):
    """业务规则引擎：规则判定必须由引擎完成，不依赖 LLM 自觉遵守。"""

    @abstractmethod
    def evaluate(self, action: str, context: dict[str, Any]) -> PolicyDecision:
        """对某个动作给出判定（allow / deny / require_confirmation / escalate）。"""


class Retriever(ABC):
    """检索抽象：屏蔽底层是 LlamaIndex 还是别的实现。"""

    @abstractmethod
    def search(
        self, query: str, top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[Chunk]:
        """按查询返回带来源与分块标识的检索结果。"""


class LLMProvider(ABC):
    """模型供给抽象：默认 DeepSeek，可切换为本地 Fake 模型（无密钥可跑通）。

    返回**自研不透明句柄**，契约签名中不出现 ``Any``、也不出现任何框架类型；
    句柄 → 具体模型对象的解包只发生在 ``langgraph_adapter.py`` 内部。
    """

    @abstractmethod
    def chat_model(self) -> ChatModelHandle:
        """返回模型句柄（仅描述身份，不可直接调用）。"""


class Embedder(ABC):
    """向量化抽象：默认本地模型，不依赖任何 API Key。"""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """把文本批量转换为向量。"""


class AgentRunner(ABC):
    """编排门面：Core 与 apps 只依赖它，不依赖任何状态图 / 框架类型。"""

    @abstractmethod
    def run(self, session_id: str, user_input: str, ctx: ToolContext) -> AgentTurnResult:
        """处理一轮用户输入。"""

    @abstractmethod
    def resume(self, session_id: str, decision: PolicyDecision) -> AgentTurnResult:
        """从人工确认的中断点恢复执行（HITL）。"""
