"""核心数据契约：纯数据、不含行为、不 import 任何框架。

本模块是「框架类型不得进入自研契约签名」这条红线的直接承载者，由
``tests/test_framework_boundary.py`` 静态扫描强制：``ToolSpec`` / ``ToolResult`` /
``Chunk`` / ``PolicyDecision`` / ``AgentTurnResult`` / ``ChatModelHandle`` 中
不得出现 ``BaseMessage`` / ``Node`` / ``Document`` / ``StructuredTool`` /
``CompiledGraph`` 等框架类型。

约定：
- ``list`` / ``dict`` 默认值一律使用 ``Field(default_factory=...)``；
  虽然 Pydantic v2 对可变默认值本就会深拷贝，但显式写法意图更明确。
- ``ToolResult.error_code`` 的取值域由 ``zkagent.core.tools.schema.ErrorCode`` 定义；
  此处保持 ``str`` 以避免 types → tools 的反向依赖。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "RiskLevel",
    "DecisionType",
    "TurnStatus",
    "ActorType",
    "ToolSpec",
    "ToolResult",
    "Chunk",
    "PolicyDecision",
    "ToolContext",
    "AgentTurnResult",
    "ChatModelHandle",
    "ProviderSpec",
    "InboundMessage",
    "OutboundMessage",
    "ChatMessage",
    "StatusBar",
    "PromptPaths",
    "IndustryPackConfig",
    "ToolInjectionConfig",
    "IndustryToolsConfig",
    "PolicyRule",
    "PolicyRulesDocument",
]

#: 工具风险等级：决定是否审计、是否允许自动执行。
RiskLevel = Literal["low", "medium", "high"]

#: 策略判定结果类型。
DecisionType = Literal["allow", "deny", "require_confirmation", "escalate"]

#: 单轮编排结果状态；``awaiting_confirmation`` 表示挂起待人工审批，**不等于**业务已办理。
TurnStatus = Literal["completed", "awaiting_confirmation", "denied", "failed", "max_steps"]

#: 动作发起方。``human_agent`` 表示人工坐席，用于 HITL 场景。
ActorType = Literal["user", "agent", "human_agent", "system"]

#: 模型**传输协议**（接缝据此选择解包方式）。
#:
#: - ``openai``：OpenAI 兼容协议——绝大多数供应商（DeepSeek / 通义 / 智谱 / Moonshot /
#:   本地 vLLM / Ollama / LM Studio / 自建网关）都提供该端点，差异只在 ``base_url``；
#: - ``anthropic``：Anthropic Messages 协议；
#: - ``deepseek``：DeepSeek 官方集成（与 openai 协议等价，只是复用其默认地址与参数默认值）；
#: - ``fake``：脚本化离线模型，供无密钥 CI 与验收使用。
LLMProtocol = Literal["openai", "anthropic", "deepseek", "fake"]


class ToolSpec(BaseModel):
    """工具的业务元数据——框架不提供这部分，必须自研。

    ``ToolSpec`` 是工具定义的**唯一事实来源**：编排框架的工具对象由它转换而来，
    不得反向以框架定义为基准。
    """

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict, description="JSON Schema")
    risk: RiskLevel = "low"
    permissions: list[str] = Field(default_factory=list, description="调用所需权限标识")
    idempotent: bool = False
    requires_idempotency_key: bool = Field(
        default=False,
        description="写操作必须由调用方提供幂等键；为 True 时无键调用一律拒绝",
    )
    timeout_s: float = 10.0
    tags: list[str] = Field(
        default_factory=list,
        description="用于按需注入过滤：kb/order/refund/service/logistics/risky",
    )
    confirmation_required: bool = Field(
        default=False,
        description="True 表示需人工确认（HITL）后才能执行",
    )


class ToolResult(BaseModel):
    """工具调用的统一返回形态。

    ``ok=False`` 时必须给出 ``error_code``（取值域见 ``ErrorCode``），
    且**不得**把失败改写成成功。
    """

    ok: bool
    data: Any | None = None
    error_code: str | None = None
    message: str | None = None
    latency_ms: int = 0
    trace_id: str | None = None


class Chunk(BaseModel):
    """检索结果对外唯一形态（不暴露任何框架内部节点类型）。"""

    text: str
    source: str = Field(description="文档路径 / 文件名，用于回答中给出引用")
    node_id: str = Field(description="稳定分块标识，形如 <doc_id>:<chunk_idx>")
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    """策略引擎的判定结论——由规则计算得出，**不得**由 LLM 代劳。"""

    action: str
    allowed: bool
    decision: DecisionType
    reason: str
    rule_id: str = Field(description="命中的规则标识，便于失败归因")
    required_permissions: list[str] = Field(default_factory=list)
    requires_hitl: bool = False
    suggested_action: str | None = Field(
        default=None,
        description="Poka-yoke 引导：如「未发货」应引导为 cancel_order",
    )
    #: 以下字段只在 HITL 恢复时使用（``resume``），普通判定留空。
    approval_id: str | None = Field(default=None, description="待消费的审批单号")
    approver_id: str | None = Field(default=None, description="审批人（来自可信配置，不取自对话）")
    approver_mode: str = Field(
        default="dev_unauthenticated",
        description="审批人身份来源：token / dev_unauthenticated",
    )
    approved_payload: dict[str, Any] | None = Field(
        default=None, description="批准时冻结的参数；执行只认它"
    )


class ToolContext(BaseModel):
    """工具执行上下文：身份、归属、幂等与追踪信息。"""

    trace_id: str
    session_id: str
    thread_id: str | None = None
    user_id: str | None = None
    actor: ActorType = "user"
    permissions: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None
    #: 本轮会话是否已通过身份核验（``verify_user`` 成功）；规则用它做订单类工具的门禁。
    verified: bool = False
    industry_id: str = "appliance"
    channel_id: str = "cli"


class AgentTurnResult(BaseModel):
    """编排门面（``AgentRunner``）对外唯一返回形态。"""

    final_text: str
    status: TurnStatus
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    trace_id: str | None = None
    #: 模型用量（input/output/total tokens）；Fake 或供应商不返回时为空，由评估层估算
    usage: dict[str, int] = Field(default_factory=dict)
    #: 本轮模型调用累计耗时（毫秒）；用于 P6 的延迟统计
    latency_ms: int | None = None
    #: 本轮策略判定摘要（tool / decision / rule_id / reason），供评估断言
    policy: list[dict[str, Any]] = Field(default_factory=list)


class ChatModelHandle(BaseModel):
    """模型句柄：**不透明**标识，只承载自研身份信息。

    设计约束：
    - 不含任何框架成员（不得出现 ChatModel / Runnable / BaseMessage 等）；
    - **不定义任何调用方法**——Core 无法直接拿它推理，必须经 ``AgentRunner``；
    - 只有 ``core/orchestrator/langgraph_adapter.py`` 能在内部把它解包为具体模型对象。

    传输信息（``protocol`` / ``base_url`` / ``api_key_env`` / ``requires_api_key``）是
    **显式字段**而非塞进 ``params``：供应商可换，但解包规则只由 ``protocol`` 决定。
    ``api_key_env`` 存的是**环境变量名**，密钥值永不进入本结构。
    """

    provider_id: str = Field(
        description=(
            '供应商标识，如 "deepseek" / "openai" / "qwen" / "zhipu" / "ollama" '
            '/ "anthropic" / "custom" / "fake"'
        )
    )
    model_id: str = Field(description='模型 id，如 "deepseek-chat" / "qwen-plus"')
    protocol: LLMProtocol = Field(
        default="openai",
        description="解包协议：openai 兼容 / anthropic / deepseek 官方 / fake",
    )
    base_url: str | None = Field(
        default=None,
        description="None 表示使用该协议官方默认地址；OpenAI 兼容供应商填其兼容端点",
    )
    api_key_env: str | None = Field(
        default=None, description="存放密钥的环境变量名；**密钥值本身不入句柄**"
    )
    requires_api_key: bool = Field(
        default=True, description="本地模型（如 Ollama / vLLM）不需要密钥"
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="采样参数（temperature / timeout_s）与 provider 私有载荷（如 fake 的 script）",
    )


class ProviderSpec(BaseModel):
    """模型供应登记项——**纯数据**，不含密钥、不含任何框架类型。

    设计要点：

    - ``protocol`` 决定接缝内用哪种传输协议解包，**供应商差异不写进代码分支**；
    - ``base_url`` 为 ``None`` 表示使用该协议的官方默认地址；
    - ``api_key_env`` 只记录**环境变量名**；
    - ``default_model`` 为 ``None`` 表示「该供应商模型名易变，必须由用户显式指定」，
      避免猜一个可能已下线的默认模型；
    - ``requires_base_url`` 用于「任意 OpenAI 兼容端点」这类必须自带地址的登记项。
    """

    provider_id: str
    label: str
    protocol: LLMProtocol
    base_url: str | None = None
    api_key_env: str | None = None
    requires_api_key: bool = True
    requires_base_url: bool = False
    default_model: str | None = None


class ChatMessage(BaseModel):
    """与框架无关的对话消息（Core 内部流通；框架消息只在接缝内转换）。

    存在的意义：Core 需要组装上下文，但**不得**在签名里出现框架的消息类型。

    ``tool_calls`` 用**普通字典**表达（``name`` / ``args`` / ``id``），因此不引入框架类型；
    它是必须保留的信息——一旦丢失，后续的 ``tool`` 消息就会缺少前置的 ``tool_calls``，
    OpenAI 兼容接口（含 DeepSeek）会直接报 400。
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    name: str | None = Field(default=None, description="工具消息对应的工具名")
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list, description="assistant 消息发起的工具调用（name/args/id）"
    )


class StatusBar(BaseModel):
    """每轮注入的「状态栏」：单独成块，不污染静态前缀（Prompt Cache 友好）。"""

    industry_id: str
    session_id: str
    user_id: str | None = None
    channel_id: str = "cli"
    step: int = 0
    max_steps: int = 8
    pending_confirmation: bool = False
    #: 本会话是否已完成**本人**身份核验（登录态带入，或 verify_user 通过）。
    #: 必须写进状态栏：否则模型不知道已核验过，会再问一次尾号甚至编造尾号。
    verified: bool = False


class PromptPaths(BaseModel):
    """行业包内提示词文件路径（相对行业包根目录）。"""

    system: str = "prompts/system.md"
    fewshots: str | None = "prompts/fewshots.yaml"


class IndustryPackConfig(BaseModel):
    """``pack.yaml`` 的强类型表示——行业差异的**入口清单**（全部是数据，不是分支）。"""

    id: str
    name: str
    products: list[str] = Field(default_factory=list)
    knowledge_dir: str = "knowledge/"
    policies_file: str = "policies/rules.yaml"
    prompts: PromptPaths = Field(default_factory=PromptPaths)
    tools_file: str = "tools.yaml"
    eval_cases: str = "evals/cases.yaml"
    eval_retrieval: str = Field(
        default="evals/retrieval.yaml",
        description="检索评测集（gold 标注；P3 起用于 Recall@k / MRR@k / nDCG@k）",
    )
    seed_data: str = "seed_data/"
    demo_scenarios: str | None = Field(
        default=None,
        description="演示脚本（仅服务于无密钥离线演示与测试；缺失表示未提供）",
    )


class ToolInjectionConfig(BaseModel):
    """工具按需注入策略（禁止把全部工具描述塞给模型）。"""

    max_tools: int = Field(default=8, ge=1, description="单次注入的工具数量上限")
    prefilter_by: list[str] = Field(
        default_factory=lambda: ["tags", "intent", "permissions"],
        description="过滤维度：tags / intent / permissions",
    )
    always_include: list[str] = Field(
        default_factory=list,
        description=(
            "无论 tag / intent 如何过滤、也无论 max_tools 如何截断，都必须注入的工具"
            "（身份核验、查单、写操作等核心工具）；顺序即优先级"
        ),
    )


class IndustryToolsConfig(BaseModel):
    """``tools.yaml`` 的强类型表示。"""

    enabled: list[str] = Field(default_factory=list)
    tags: dict[str, list[str]] = Field(default_factory=dict, description="按工具名补充 tag")
    overrides: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="按工具名覆盖 risk / timeout_s / confirmation_required"
    )
    extensions: list[dict[str, Any]] = Field(default_factory=list)
    injection: ToolInjectionConfig = Field(default_factory=ToolInjectionConfig)


class PolicyRule(BaseModel):
    """``rules.yaml`` 中的一条声明式规则（判定逻辑在 PolicyEngine，P4 落地）。"""

    id: str
    description: str = ""
    match: dict[str, Any] = Field(default_factory=dict)
    effect: DecisionType
    preconditions: list[str] = Field(default_factory=list)
    suggested_action: str | None = None
    priority: int = Field(default=100, description="冲突时数值小者优先")


class PolicyRulesDocument(BaseModel):
    """``rules.yaml`` 顶层结构。"""

    version: int = 1
    rules: list[PolicyRule] = Field(default_factory=list)


class InboundMessage(BaseModel):
    """归一化入站消息——渠道差异在此之前已被抹平。"""

    session_id: str
    text: str
    user_id: str | None = None
    received_at: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(
        default_factory=dict,
        description="原始渠道载荷：仅留痕，**不参与任何决策**",
    )


class OutboundMessage(BaseModel):
    """归一化出站消息。"""

    session_id: str
    text: str
    cards: list[dict[str, Any]] | None = None
    awaiting_confirmation: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
