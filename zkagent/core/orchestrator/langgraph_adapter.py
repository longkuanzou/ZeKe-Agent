"""接缝一：**唯一**允许 import ``langgraph`` / ``langchain`` 的文件。

对外提供两组接口（DESIGN §6.2）：

- **低层（框架形态）**：:func:`specs_to_langchain_tools`、:func:`build_graph`、
  :func:`resolve_chat_model` —— 只应被本文件与「接缝专项测试」调用；
- **高层（对齐自研抽象）**：:func:`build_runner` 与 :class:`LangGraphRunner`
  （实现 :class:`zkagent.core.interfaces.AgentRunner`），Core 与 apps 只用这一层。

本阶段（P2）的图是**最小 ReAct 循环**：

``load_context → llm_decide →（tool_execute → llm_decide | respond）→ respond``

带 ``max_steps`` 熔断；工具执行一律经 ``ToolExecutor``（幂等 / 超时 / 重试都在那里）。
``policy_check`` 与 ``hitl_interrupt`` 节点由 P4 在本文件扩展加入（见 DESIGN §6.2 节点表），
本阶段**不接线、也不提供占位实现**——避免出现「看起来有安全兜底其实没有」的假象。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any

from langchain.chat_models import init_chat_model  # noqa: F401  (保留统一入口，供 DeepSeek 之外的 provider 使用)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from zkagent.core.context.engine import ContextEngine
from zkagent.core.interfaces import AgentRunner, PolicyEngine
from zkagent.core.llm import ProviderCallError, ProviderConfigError
from zkagent.core.policy.approvals import (
    ApprovalRecord,
    ApprovalStore,
    approval_id_for,
    fingerprint_of,
    is_expired,
)
from zkagent.core.policy.errors import ApprovalError
from zkagent.core.policy.facts import FactsProvider, PolicyFacts
from zkagent.core.tools.executor import ToolExecutor
from zkagent.core.types import (
    AgentTurnResult,
    ChatMessage,
    ChatModelHandle,
    PolicyDecision,
    StatusBar,
    ToolContext,
    ToolSpec,
)

__all__ = [
    "resolve_chat_model",
    "to_langchain_messages",
    "stream_chat",
    "chat_once",
    "specs_to_langchain_tools",
    "build_graph",
    "build_runner",
    "LangGraphRunner",
    "open_checkpointer",
]


class ModelNotConfiguredError(ProviderConfigError):
    """模型句柄无法解包（例如缺少密钥）——必须显式失败，不得静默降级为 Fake。

    继承 :class:`ProviderConfigError` 而非裸 ``RuntimeError``：这类错误本质是
    **配置问题**，应当与「供应商未注册 / 缺模型 id / 缺端点」走同一条上报路径，
    由 CLI 转成结构化错误信息，而不是把异常栈甩给用户。
    """


class FakeScriptError(ValueError):
    """``_ScriptedChatModel`` 的脚本写错（未注入的工具 / 缺 name）。

    继承 ``ValueError`` 兼容既有断言；单独成类是为了让编排层**不要把脚本配置错误**
    包装成「模型服务不可用」。
    """


# ===================================================================== 句柄解包


def extract_tool_payload(text: str) -> dict[str, Any] | None:
    """从工具消息文本中提取结构化载荷。

    上下文引擎出于「轨迹隔离」会给工具消息加摘要前缀（如 ``[工具 x 返回] {...}``），
    因此不能简单地 ``json.loads`` 整段文本；这里先按整段解析，失败再定位第一个 ``{``
    并用 ``raw_decode`` 解析到对象结束。
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        value = json.loads(stripped)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        pass
    start = stripped.find("{")
    if start == -1:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(stripped[start:])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def summarize_last_tool_result(messages: list[BaseMessage]) -> str:
    """把最近一条工具返回渲染成人类可读文本（成功给数据、失败给错误码与原因）。

    用途：Fake 脚本用 ``content_from_tool`` 生成回答时，**依据真实工具结果**说话，
    避免「工具失败但回答宣称成功」这种最危险的不一致。
    """
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            continue
        raw = message.content if isinstance(message.content, str) else str(message.content)
        payload = extract_tool_payload(raw)
        if payload is None:
            return f"（工具 {getattr(message, 'name', '?')} 返回了非结构化内容）"
        if payload.get("ok"):
            return "查询结果：" + json.dumps(payload.get("data"), ensure_ascii=False, default=str)
        code = payload.get("error_code") or "UNKNOWN"
        return f"操作未成功（{code}）：{payload.get('message') or '未提供原因'}"
    return "（本轮没有工具返回）"


class _ScriptedChatModel(BaseChatModel):
    """脚本化 Fake 模型：支持工具调用与 ``bind_tools``，无需任何密钥。

    为什么自建：实测 LangChain 的 ``FakeListChatModel`` 与 ``GenericFakeChatModel``
    的 ``bind_tools`` 都抛 ``NotImplementedError``，无法驱动工具调用循环。
    脚本耗尽后**重复最后一步**（不会抛 ``StopIteration``）；若最后一步是工具调用，
    图会一路走到 ``max_steps`` 熔断，正好用于验证熔断路径。
    """

    script: list[dict[str, Any]]
    cursor: int = 0
    bound_tool_names: list[str] = []

    @property
    def _llm_type(self) -> str:
        """LangChain 要求实现的模型类型标识（仅用于日志与追溯）。

        :returns: 固定为 ``zkagent-scripted-fake``。
        """
        return "zkagent-scripted-fake"

    def bind_tools(  # type: ignore[override]
        self, tools: Any, **kwargs: Any
    ) -> "_ScriptedChatModel":
        """记录被注入的工具名，便于脚本写错时尽早报错。"""
        names = [getattr(tool, "name", "") or getattr(tool, "__name__", "") for tool in tools]
        self.bound_tool_names = [name for name in names if name]
        return self

    def _generate(  # type: ignore[override]
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """按脚本产出下一步（可能是工具调用，也可能是最终回答）。

        脚本耗尽后重复最后一步；若脚本写了未注入的工具名，这里立刻抛
        :class:`FakeScriptError`——避免「测试通过但工具根本没被调用」。

        :param messages: 本轮上下文（脚本模式不使用，但保留签名）。
        :param stop: 停止词，脚本模式忽略。
        :param run_manager: LangChain 回调管理器，脚本模式忽略。
        :param kwargs: 其它框架参数，忽略。
        :returns: 一步 :class:`ChatResult`。
        :raises FakeScriptError: 脚本缺少工具名，或调用了未注入的工具。
        """
        step = self.script[min(self.cursor, len(self.script) - 1)]
        self.cursor += 1
        raw_calls = list(step.get("tool_calls") or [])
        tool_calls: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_calls):
            name = raw.get("name")
            if not name:
                raise FakeScriptError("Fake 脚本的 tool_calls 缺少 name")
            if self.bound_tool_names and name not in self.bound_tool_names:
                raise FakeScriptError(
                    f"Fake 脚本调用了未注入的工具 {name!r}；已注入：{self.bound_tool_names}"
                )
            # 单调用保持 ``call-N``（既有断言依赖它）；多调用必须唯一，
            # 否则同一批里的 tool 消息无法与调用一一对应（真实模型也不会重复 id）。
            call_id = (
                f"call-{self.cursor}"
                if len(raw_calls) == 1
                else f"call-{self.cursor}-{index + 1}"
            )
            tool_calls.append({"name": name, "args": dict(raw.get("args") or {}), "id": call_id})

        if "content_from_tool" in step:
            # 由**真实工具结果**渲染回答：成功给数据、失败给错误，绝不谎报成功
            content = str(step.get("content_from_tool") or "") + summarize_last_tool_result(
                messages
            )
        else:
            content = str(step.get("content") or "")

        message = AIMessage(content=content, tool_calls=tool_calls)
        return ChatResult(generations=[ChatGeneration(message=message)])


#: 本地端点（Ollama / vLLM / 自建网关）通常不校验密钥，但客户端库要求非空字符串。
_PLACEHOLDER_API_KEY = "not-required"


def _api_key_for(handle: ChatModelHandle) -> str:
    """按句柄里的**环境变量名**取密钥；「需要密钥却缺失」时显式报错。

    - 需要密钥但环境变量为空 → :class:`ModelNotConfiguredError`（**不静默降级为 Fake**）；
    - 明确不需要密钥（本地端点）→ 返回占位值，客户端库要求非空。
    """
    env_name = handle.api_key_env
    if not env_name:
        return _PLACEHOLDER_API_KEY
    api_key = os.environ.get(env_name)
    if api_key:
        return api_key
    if handle.requires_api_key:
        raise ModelNotConfiguredError(
            f"provider={handle.provider_id} 需要环境变量 {env_name}；"
            "若只想离线跑通，请改用 --provider fake（本系统不会自动降级）"
        )
    return _PLACEHOLDER_API_KEY


def resolve_chat_model(handle: ChatModelHandle) -> BaseChatModel:
    """把自研句柄解包成具体模型对象（**本函数是本文件存在的主要理由之一**）。

    **按 ``protocol`` 分发，不按供应商名分发**：因此新增一家 OpenAI 兼容供应商
    （通义 / 智谱 / Moonshot / 本地 vLLM / 自建网关……）**不需要改本函数**——
    差异全部来自句柄的 ``base_url`` 与 ``model_id``。

    密钥只在此时从环境变量读取，绝不进入句柄、日志或 trace。
    """
    protocol = (handle.protocol or "").strip().lower()
    params = handle.params

    if protocol == "fake":
        script = params.get("script") or [{"content": "（Fake 脚本为空）"}]
        return _ScriptedChatModel(script=[dict(step) for step in script])

    kwargs: dict[str, Any] = {
        "model": handle.model_id,
        "api_key": _api_key_for(handle),
        "temperature": float(params.get("temperature", 0.0)),
        "timeout": float(params.get("timeout_s", 60.0)),
    }
    if handle.base_url:
        # 三个协议的模型类都接受 ``base_url``（OpenAI 兼容端点 / Anthropic 网关）
        kwargs["base_url"] = handle.base_url

    if protocol == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(**kwargs)

    if protocol == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(**kwargs)

    if protocol == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        return ChatDeepSeek(**kwargs)

    raise ModelNotConfiguredError(
        f"未知的模型 provider 协议：{protocol!r}（provider_id={handle.provider_id!r}）"
    )


# ===================================================================== 工具转换


def specs_to_langchain_tools(
    specs: list[ToolSpec],
    *,
    executor: ToolExecutor,
    ctx: ToolContext,
) -> list[StructuredTool]:
    """把自研 ``ToolSpec`` **转换**为框架工具对象。

    方向不可逆：``ToolSpec`` 是唯一事实来源，框架工具只是它的投影。
    每个框架工具的回调都走 ``ToolExecutor``，因此幂等 / 超时 / 重试语义一致。
    """
    tools: list[StructuredTool] = []

    def _make(spec: ToolSpec) -> StructuredTool:
        """把一条 ``ToolSpec`` 投影成框架工具。

        :param spec: 工具声明（名称、描述、参数 schema 全部来自它）。
        :returns: 绑定到执行器的 :class:`StructuredTool`。
        """

        def _call(**kwargs: Any) -> dict[str, Any]:
            """框架回调：统一走 ``ToolExecutor``，保证幂等 / 超时 / 重试语义一致。

            :param kwargs: 模型给出的工具参数。
            :returns: 序列化后的 :class:`ToolResult` 字典。
            """
            result = executor.call(spec, kwargs, ctx)
            return result.model_dump(mode="json")

        return StructuredTool(
            name=spec.name,
            description=spec.description,
            args_schema=spec.parameters or None,
            func=_call,
        )

    for spec in specs:
        tools.append(_make(spec))
    return tools


# ===================================================================== 消息转换


def _to_lc(message: ChatMessage) -> BaseMessage:
    """自研消息 → 框架消息。"""
    if message.role == "system":
        return SystemMessage(content=message.content)
    if message.role == "user":
        return HumanMessage(content=message.content)
    if message.role == "assistant":
        return AIMessage(content=message.content, tool_calls=list(message.tool_calls))
    if message.role == "tool":
        if not message.tool_call_id:
            raise ValueError("tool 角色的消息必须带 tool_call_id")
        return ToolMessage(
            content=message.content,
            tool_call_id=message.tool_call_id,
            name=message.name or "tool",
        )
    raise ValueError(f"未知消息角色：{message.role!r}")


def _text_of(content: Any) -> str:
    """把框架消息的 content 归一为纯文本。

    兼容三种形态：``str``、``None``（部分实现会返回）、内容块列表
    （``[{"type": "text", "text": "..."}]``）；**不得**把 ``None`` 渲染成 ``"None"``。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(content)


def _backfill_tool_responses(history: list[ChatMessage]) -> list[ChatMessage]:
    """确保每条 ``assistant.tool_calls`` 都有对应的 ``tool`` 消息。

    OpenAI 兼容接口对此是**硬校验**：少一条就整轮 400。图里正常路径一定会配对，
    但中止/裁剪/异常回退都可能留下悬挂调用（真实验收中确实出现过）。这里统一兜底，
    给缺失的调用补一条「未执行」回执——既不伪装成功，也保证对话可继续。
    """
    answered = {
        str(message.tool_call_id)
        for message in history
        if message.role == "tool" and message.tool_call_id
    }
    patched: list[ChatMessage] = []
    for message in history:
        patched.append(message)
        if message.role != "assistant" or not message.tool_calls:
            continue
        for call in message.tool_calls:
            call_id = str(call.get("id") or "")
            if not call_id or call_id in answered:
                continue
            answered.add(call_id)
            patched.append(
                ChatMessage(
                    role="tool",
                    name=str(call.get("name") or "tool"),
                    tool_call_id=call_id,
                    content=json.dumps(
                        {
                            "ok": False,
                            "error_code": "DEFERRED_BY_APPROVAL",
                            "message": "该调用未执行（等待审批或本轮中止），请勿假设已完成",
                        },
                        ensure_ascii=False,
                    ),
                )
            )
    return patched


def _from_lc(message: BaseMessage) -> ChatMessage | None:
    """框架消息 → 自研消息；无法映射的类型返回 ``None``。

    **必须保留 ``tool_calls``**：否则回放上下文时，``tool`` 消息会缺少前置的
    ``tool_calls``，OpenAI 兼容接口（含 DeepSeek）会直接拒绝该请求。
    """
    if isinstance(message, SystemMessage):
        return ChatMessage(role="system", content=_text_of(message.content))
    if isinstance(message, HumanMessage):
        return ChatMessage(role="user", content=_text_of(message.content))
    if isinstance(message, ToolMessage):
        return ChatMessage(
            role="tool",
            content=_text_of(message.content),
            name=getattr(message, "name", None),
            tool_call_id=getattr(message, "tool_call_id", None),
        )
    if isinstance(message, AIMessage):
        calls = [
            {
                "name": call.get("name", ""),
                "args": dict(call.get("args") or {}),
                "id": call.get("id", ""),
            }
            for call in (getattr(message, "tool_calls", None) or [])
        ]
        return ChatMessage(role="assistant", content=_text_of(message.content), tool_calls=calls)
    return None


# ===================================================================== 图状态与节点


class AgentState(TypedDict, total=False):
    """图状态。

    注意：状态里含框架消息对象，因此**只存在于本文件内**；
    对外一律经 :func:`to_turn_result` 与 :class:`AgentTurnResult` 交换。
    """

    messages: Annotated[list[BaseMessage], add_messages]
    prefix: list[dict[str, Any]]
    steps: int
    max_steps: int
    trace_id: str
    session_id: str
    user_id: str | None
    channel_id: str
    industry_id: str
    tool_calls: list[dict[str, Any]]
    final_text: str
    status: str
    pending_confirmation: dict[str, Any] | None
    #: 已通过 ``verify_user`` 的用户（会话级，随检查点持久化）
    verified_user_id: str | None
    #: 本轮被策略拒绝的调用 id：``tool_execute`` 必须跳过它们，且不回填成功
    blocked_call_ids: list[str]
    #: 最近一次策略判定的摘要（排障用，不参与业务）
    policy: dict[str, Any] | None
    #: 本轮累计模型用量（input/output/total tokens），供 P6 成本统计
    usage: dict[str, int]
    #: 本轮累计模型耗时（毫秒）
    latency_ms: int
    #: 当前线程 id（审批号确定性生成需要）
    thread_id: str


def build_graph(
    bound_model: BaseChatModel,
    tools: list[StructuredTool],
    *,
    specs: list[ToolSpec],
    executor: ToolExecutor,
    tool_context: ToolContext,
    context_engine: ContextEngine,
    checkpointer: Any = None,
    max_steps: int = 8,
    policy_engine: PolicyEngine | None = None,
    facts_provider: FactsProvider | None = None,
    approval_store: ApprovalStore | None = None,
    tracer: Any = None,
) -> Any:
    """构造并编译最小 ReAct 状态图（自建 ``StateGraph``，非预置 Agent）。

    相比 P2 的 4 节点版本，P4 在 ``llm_decide`` 与 ``tool_execute`` 之间插入了
    ``policy_check`` 与 ``hitl_interrupt``：规则判定与人工审批都在**执行之前**完成。

    :param bound_model: 已 ``bind_tools`` 的模型。
    :param tools: :func:`specs_to_langchain_tools` 的产物（仅用于校验工具集合）。
    :param specs: 工具声明，执行时用于获取 risk / 幂等等元数据。
    :param policy_engine: 规则引擎；``None`` 时视为默认放行（便于只跑编排的单测）。
    :param facts_provider: 事实提供者；``None`` 时事实为空（规则会按「不满足」处理）。
    :param approval_store: 审批仓储；``None`` 时挂起只返回 pending，不落库（单测用）。
    """
    declared = {spec.name: spec for spec in specs}
    tools_by_name = {tool.name: tool for tool in tools}
    missing = declared.keys() - tools_by_name.keys()
    if missing:
        raise ValueError(f"以下工具声明没有对应的框架工具：{sorted(missing)}")

    def load_context(state: AgentState) -> dict[str, Any]:
        """组装静态前缀 + 状态栏（轨迹单独存放，不进入前缀）。"""
        status = StatusBar(
            industry_id=state.get("industry_id", tool_context.industry_id),
            session_id=state.get("session_id", tool_context.session_id),
            user_id=state.get("user_id", tool_context.user_id),
            channel_id=state.get("channel_id", tool_context.channel_id),
            step=state.get("steps", 0),
            max_steps=state.get("max_steps", max_steps),
            verified=_verified(state),
        )
        bundle = context_engine.build(status=status, history=[])
        return {"prefix": [message.model_dump() for message in bundle.prefix]}

    def llm_decide(state: AgentState) -> dict[str, Any]:
        """调用模型产出「工具调用」或「最终回答」——**不做任何规则判定**。"""
        prefix = [_to_lc(ChatMessage.model_validate(item)) for item in state.get("prefix", [])]
        history = [
            converted
            for converted in (_from_lc(message) for message in state.get("messages", []))
            if converted is not None
        ]
        # P5：先压缩（工具载荷 + 旧轮摘要），再补 tool 回执；顺序反了会把 JSON 压坏
        prepared, context_stats = context_engine.prepare_history(history)
        if tracer is not None:
            tracer.record(state.get("trace_id", ""), "context_stats", **context_stats)
        trimmed = _backfill_tool_responses(prepared)
        prompt = [*prefix, *[_to_lc(message) for message in trimmed]]
        started = time.perf_counter()
        try:
            decision = bound_model.invoke(prompt)
        except FakeScriptError:
            # 脚本化 Fake 的「调用了未注入工具」属于编排配置错误，保留原始异常，
            # 不能被归因成「模型服务不可用」。
            raise
        except Exception as exc:  # 供应商 SDK 的异常不得以裸栈穿透到 CLI
            raise ProviderCallError(
                f"模型调用失败（{type(exc).__name__}）：{exc}"
            ) from exc
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        usage = dict(state.get("usage") or {})
        for key, value in (getattr(decision, "usage_metadata", None) or {}).items():
            if isinstance(value, int):
                usage[key] = usage.get(key, 0) + value
        return {
            "messages": [decision],
            "steps": state.get("steps", 0) + 1,
            "usage": usage,
            "latency_ms": int(state.get("latency_ms") or 0) + elapsed_ms,
        }

    def _verified(state: AgentState) -> bool:
        """会话级核验：必须是**当前用户**本人通过，不能拿别人的核验结果。"""
        return bool(state.get("verified_user_id")) \
            and state.get("verified_user_id") == state.get(
            "user_id"
        )

    def _current_query(state: AgentState) -> str:
        """取最近一条用户消息的原文（供 ``query_contains_any`` 类规则做关键词判定）。

        :param state: 图状态。
        :returns: 用户原话；没有用户消息时返回空串。
        """
        for message in reversed(state.get("messages", [])):
            if isinstance(message, HumanMessage):
                return str(message.content)
        return ""

    def policy_check(state: AgentState) -> dict[str, Any]:
        """执行前逐条判定：deny 回填错误、需审批则整轮挂起（不做「一半已执行」）。

        **批量语义**：只要有一条需要人工审批，本轮**不执行任何工具**——否则会出现
        「退款已提交、另两笔还在等审批」的中间态，用户与坐席都无法判断最终结果。
        """
        last = state["messages"][-1]
        calls = list(getattr(last, "tool_calls", []) or [])
        prior = [str(item["name"]) for item in state.get("tool_calls", []) if item.get("ok")]
        verified = _verified(state)
        # 恢复执行时 runner 的 ctx 可能是按 approval 重建的（甚至没有 user_id），
        # 身份必须以**图状态**为准，否则归属校验会把本人操作判成越权。
        ctx_view = tool_context.model_copy(
            update={
                "verified": verified,
                "user_id": state.get("user_id") or tool_context.user_id,
                "session_id": state.get("session_id") or tool_context.session_id,
            }
        )
        query = _current_query(state)

        denials: list[BaseMessage] = []
        blocked: list[str] = []
        summary: list[dict[str, Any]] = []
        pending: dict[str, Any] | None = None
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        for call in calls:
            name = str(call.get("name") or "")
            args = dict(call.get("args") or {})
            call_id = str(call.get("id") or name)

            facts = (
                facts_provider.facts_for(name, args, ctx_view)
                if facts_provider is not None
                else PolicyFacts()
            )
            # 事实里的核验状态、本轮已调工具、用户原话由编排层补齐（后端只负责业务数据）
            facts = facts.model_copy(
                update={"prior_tools": prior, "query_text": query, "verified": verified}
            )

            if policy_engine is None:
                decision = PolicyDecision(
                    action=name,
                    allowed=True,
                    decision="allow",
                    reason="未配置策略引擎（默认放行）",
                    rule_id="default.allow",
                )
            else:
                decision = policy_engine.evaluate(
                    name,
                    {
                        "facts": facts,
                        "permissions": ctx_view.permissions,
                        "user_id": ctx_view.user_id,
                    },
                )

            summary.append(
                {
                    "tool": name,
                    "decision": decision.decision,
                    "rule_id": decision.rule_id,
                    "reason": decision.reason,
                }
            )
            if tracer is not None:
                tracer.record(
                    state.get("trace_id", ""),
                    "policy_decision",
                    tool=name,
                    decision=decision.decision,
                    rule_id=decision.rule_id,
                )

            if decision.allowed:
                continue

            if decision.requires_hitl:
                fingerprint = fingerprint_of(name, args)
                approval_id = approval_id_for(
                    state.get("thread_id") or "", state.get("steps", 0), fingerprint
                )
                record = ApprovalRecord(
                    approval_id=approval_id,
                    thread_id=state.get("thread_id") or None,
                    session_id=state.get("session_id"),
                    action=name,
                    payload_json=json.dumps(args, ensure_ascii=False, sort_keys=True),
                    params_fingerprint=fingerprint,
                    requester_id=state.get("user_id"),
                    reason=decision.reason,
                    rule_id=decision.rule_id,
                    required_permissions=list(decision.required_permissions),
                    created_at=now,
                )
                if approval_store is not None:
                    approval_store.create(record)  # 幂等：interrupt 重放不会产生第二条
                pending = {
                    "approval_id": approval_id,
                    "tool_name": name,
                    "tool_call_id": call_id,
                    "args": args,
                    "fingerprint": fingerprint,
                    "decision": decision.decision,
                    "rule_id": decision.rule_id,
                    "reason": decision.reason,
                    "suggested_action": decision.suggested_action,
                    "requester_id": state.get("user_id"),
                }
                blocked = [str(item.get("id") or item.get("name") or "") for item in calls]
                # 不 break：同一批里其余调用虽然本轮不执行，但**每个 tool_call 都必须有
                # 对应的 tool 消息**，否则下一次模型调用会因缺少 tool_call_id 被接口拒绝。
                continue

            blocked.append(call_id)
            denials.append(
                ToolMessage(
                    content=json.dumps(
                        {
                            "ok": False,
                            "error_code": "POLICY_DENIED",
                            "message": decision.reason,
                            "rule_id": decision.rule_id,
                            "suggested_action": decision.suggested_action,
                        },
                        ensure_ascii=False,
                    ),
                    tool_call_id=call_id,
                    name=name or "tool",
                )
            )

        answered = {
            str(message.tool_call_id) for message in denials if isinstance(message, ToolMessage)
        }
        if pending is not None:
            for call in calls:
                call_id = str(call.get("id") or call.get("name") or "")
                if call_id in answered or call_id == pending.get("tool_call_id"):
                    continue
                # 同批其余调用本轮一律不执行：给出**明确的未执行**回执，
                # 既满足「每个 tool_call 必有 tool 消息」，也避免模型误以为已完成。
                denials.append(
                    ToolMessage(
                        content=json.dumps(
                            {
                                "ok": False,
                                "error_code": "DEFERRED_BY_APPROVAL",
                                "message": "本轮有操作等待人工审批，该调用未执行，请不要假设已完成",
                            },
                            ensure_ascii=False,
                        ),
                        tool_call_id=call_id,
                        name=str(call.get("name") or "tool"),
                    )
                )

        return {
            "messages": denials,
            "blocked_call_ids": blocked,
            "policy": {"decisions": summary},
            "pending_confirmation": pending,
        }

    def hitl_interrupt(state: AgentState) -> dict[str, Any]:
        """挂起等待审批；恢复后用**被批准的参数**执行一次（幂等键 = approval_id）。

        执行只用 ``pending.args``/恢复时带回的 payload，**绝不**从对话里重新解析参数——
        这是「审批小额、执行大额」的唯一防线（DESIGN §7.7.3）。
        """
        pending = dict(state.get("pending_confirmation") or {})
        tool_name = str(pending.get("tool_name") or "")
        call_id = str(pending.get("tool_call_id") or tool_name)
        approval_id = str(pending.get("approval_id") or "")

        resume_value = interrupt(pending)
        data = resume_value if isinstance(resume_value, dict) else {}

        if not bool(data.get("approved")):
            payload = {
                "ok": False,
                "error_code": "APPROVAL_REJECTED",
                "message": "审批未通过，操作未执行",
                "approval_id": approval_id,
            }
            return {
                "messages": [
                    ToolMessage(
                        content=json.dumps(payload, ensure_ascii=False),
                        tool_call_id=call_id,
                        name=tool_name or "tool",
                    )
                ],
                "pending_confirmation": None,
                "blocked_call_ids": [],
            }

        approved_payload = dict(data.get("payload") or pending.get("args") or {})
        fingerprint = fingerprint_of(tool_name, approved_payload)
        spec = declared.get(tool_name)
        if spec is None:
            result_payload: dict[str, Any] = {
                "ok": False,
                "error_code": "NOT_FOUND",
                "message": f"未知工具：{tool_name}",
            }
        elif fingerprint != pending.get("fingerprint"):
            result_payload = {
                "ok": False,
                "error_code": "APPROVAL_PARAMS_CHANGED",
                "message": "审批参数与当前参数不一致，拒绝执行",
                "approval_id": approval_id,
            }
        else:
            ctx = tool_context.model_copy(
                update={
                    "idempotency_key": approval_id,
                    "verified": _verified(state),
                    # 同 policy_check：执行身份取图状态，不依赖本次 runner 的构造参数
                    "user_id": state.get("user_id") or tool_context.user_id,
                    "session_id": state.get("session_id") or tool_context.session_id,
                }
            )
            # I4：批准后必须按**当前事实**重新判定——审批期间订单可能已取消/已退款。
            recheck_failed: str | None = None
            if policy_engine is not None:
                facts = (
                    facts_provider.facts_for(tool_name, approved_payload, ctx)
                    if facts_provider is not None
                    else PolicyFacts()
                )
                facts = facts.model_copy(
                    update={
                        "verified": _verified(state),
                        "query_text": _current_query(state),
                        "prior_tools": [
                            str(item["name"])
                            for item in state.get("tool_calls", [])
                            if item.get("ok")
                        ],
                    }
                )
                recheck = policy_engine.evaluate(
                    tool_name,
                    {
                        "facts": facts,
                        "permissions": ctx.permissions,
                        "user_id": ctx.user_id,
                    },
                )
                if recheck.decision == "deny":
                    recheck_failed = f"{recheck.rule_id}: {recheck.reason}"

            if recheck_failed is not None:
                result_payload = {
                    "ok": False,
                    "error_code": "POLICY_RECHECK_FAILED",
                    "message": f"业务状态已变化，审批不再适用：{recheck_failed}",
                    "approval_id": approval_id,
                }
                if approval_store is not None:
                    approval_store.finish(
                        approval_id,
                        execution_status="failed",
                        error_code="POLICY_RECHECK_FAILED",
                    )
            else:
                result = executor.call(spec, approved_payload, ctx)
                result_payload = result.model_dump()
                if approval_store is not None:
                    approval_store.finish(
                        approval_id,
                        execution_status="succeeded" if result.ok else "failed",
                        error_code=None if result.ok else str(result.error_code or "UNKNOWN"),
                    )

        return {
            "messages": [
                ToolMessage(
                    content=json.dumps(result_payload, ensure_ascii=False),
                    tool_call_id=call_id,
                    name=tool_name or "tool",
                )
            ],
            "pending_confirmation": None,
            "blocked_call_ids": [],
        }

    def tool_execute(state: AgentState) -> dict[str, Any]:
        """执行上一条 AI 消息里的工具调用（被策略拒绝的调用直接跳过）。

        **只走一条执行路径**：调用框架工具对象，其回调内部统一经 ``ToolExecutor``，
        因此幂等 / 超时 / 重试语义与直连执行器完全一致（不会出现两套行为）。
        """
        last = state["messages"][-1]
        blocked = set(state.get("blocked_call_ids") or [])
        outputs: list[BaseMessage] = []
        recorded: list[dict[str, Any]] = []
        verified_user_id = state.get("verified_user_id")
        for call in getattr(last, "tool_calls", []) or []:
            name = str(call.get("name") or "")
            args = dict(call.get("args") or {})
            call_id = str(call.get("id") or name)
            if call_id in blocked:
                continue  # 已由策略层回填拒绝原因，不能再执行
            tool = tools_by_name.get(name)
            if tool is None:
                payload: dict[str, Any] = {
                    "ok": False,
                    "error_code": "NOT_FOUND",
                    "message": f"未知工具：{name}",
                }
            else:
                try:
                    payload = dict(tool.invoke(args))
                except Exception as exc:  # 工具层异常不得中断整轮编排
                    payload = {
                        "ok": False,
                        "error_code": "UNKNOWN",
                        "message": f"工具 {name} 执行异常：{type(exc).__name__}: {exc}",
                    }
            if name == "verify_user" and payload.get("ok"):
                verified_user_id = state.get("user_id")
            outputs.append(
                ToolMessage(
                    content=json.dumps(payload, ensure_ascii=False),
                    tool_call_id=call_id,
                    name=name or "tool",
                )
            )
            entry: dict[str, Any] = {
                "name": name,
                "ok": bool(payload.get("ok")),
                "error_code": payload.get("error_code"),
            }
            if name == "knowledge_search" and payload.get("ok"):
                # 引用断言需要「本轮到底返回了哪些片段」；只保留来源与数量，不回传正文
                chunks = payload.get("chunks") or []
                entry["chunk_count"] = len(chunks)
                entry["sources"] = [
                    str(item.get("source")) for item in chunks if isinstance(item, Mapping)
                ]
            recorded.append(entry)
        return {
            "messages": outputs,
            "tool_calls": [*state.get("tool_calls", []), *recorded],
            "blocked_call_ids": [],
            "verified_user_id": verified_user_id,
        }

    def respond(state: AgentState) -> dict[str, Any]:
        """产出最终回复；达到最大步数时如实说明，不假称完成。"""
        last = state["messages"][-1]
        steps = state.get("steps", 0)
        if isinstance(last, AIMessage) and last.content and not getattr(last, "tool_calls", None):
            return {
                "final_text": str(last.content),
                "status": "completed",
                "pending_confirmation": None,
            }
        if steps >= max_steps:
            return {
                "final_text": (
                    f"已达到最大处理步数（{max_steps} 步），为安全起见停止继续调用工具。"
                    "请补充信息或转人工处理。"
                ),
                "status": "max_steps",
                "pending_confirmation": None,
            }
        return {
            "final_text": str(getattr(last, "content", "") or "本轮未产生可回复内容。"),
            "status": "completed",
            "pending_confirmation": None,
        }

    def route_after_llm(state: AgentState) -> str:
        """模型决策后的分支：要调工具 → 进策略校验；否则或已超步 → 直接作答。

        :param state: 图状态（读最后一条消息与 ``steps``）。
        :returns: ``respond`` 或 ``policy_check`` 节点名。
        """
        last = state["messages"][-1]
        if not getattr(last, "tool_calls", None):
            return "respond"
        if state.get("steps", 0) >= max_steps:
            return "respond"  # 熔断：不再执行工具
        return "policy_check"

    def route_after_policy(state: AgentState) -> str:
        """策略判定后的分支：本轮有挂起 → 进人工审批；否则执行工具。

        :param state: 图状态（读 ``pending_confirmation``）。
        :returns: ``hitl_interrupt`` 或 ``tool_execute`` 节点名。
        """
        if state.get("pending_confirmation"):
            return "hitl_interrupt"
        return "tool_execute"

    def route_after_tools(state: AgentState) -> str:
        """工具执行后的分支：达到步数上限即熔断，否则回到模型继续决策。

        :param state: 图状态（读 ``steps``）。
        :returns: ``respond`` 或 ``llm_decide`` 节点名。
        """
        return "respond" if state.get("steps", 0) >= max_steps else "llm_decide"

    graph = StateGraph(AgentState)
    graph.add_node("load_context", load_context)
    graph.add_node("llm_decide", llm_decide)
    graph.add_node("policy_check", policy_check)
    graph.add_node("hitl_interrupt", hitl_interrupt)
    graph.add_node("tool_execute", tool_execute)
    graph.add_node("respond", respond)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "llm_decide")
    graph.add_conditional_edges(
        "llm_decide",
        route_after_llm,
        {"policy_check": "policy_check", "respond": "respond"},
    )
    graph.add_conditional_edges(
        "policy_check",
        route_after_policy,
        {"hitl_interrupt": "hitl_interrupt", "tool_execute": "tool_execute"},
    )
    graph.add_edge("hitl_interrupt", "llm_decide")
    graph.add_conditional_edges(
        "tool_execute", route_after_tools, {"llm_decide": "llm_decide", "respond": "respond"}
    )
    graph.add_edge("respond", END)

    return graph.compile(checkpointer=checkpointer)


def open_checkpointer(db_path: str | "os.PathLike[str]") -> SqliteSaver:
    """打开可持久化的 SQLite 检查点（**不用** ``MemorySaver``）。

    进程重启后仍可按 ``thread_id`` 续跑；连接允许跨线程使用，因为图调用与
    工具执行可能发生在不同线程。
    """
    path = os.fspath(db_path)
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


# ===================================================================== 高层门面


class LangGraphRunner(AgentRunner):
    """``AgentRunner`` 的 LangGraph 实现（Core 与 apps 只用这一层）。"""

    def __init__(
        self,
        graph: Any,
        *,
        context_engine: ContextEngine,
        max_steps: int = 8,
        approval_store: ApprovalStore | None = None,
        approval_ttl_hours: int = 24,
    ) -> None:
        self._graph = graph
        self._context_engine = context_engine
        self._max_steps = max_steps
        self._approval_store = approval_store
        self._approval_ttl_hours = approval_ttl_hours

    def run(self, session_id: str, user_input: str, ctx: ToolContext) -> AgentTurnResult:
        """执行一轮对话；历史由检查点按 ``thread_id`` 自动续接。"""
        from zkagent.core.orchestrator.runner import thread_id_for, to_turn_result

        thread_id = thread_id_for(session_id)
        state: AgentState = {
            "messages": [HumanMessage(content=user_input)],
            "steps": 0,
            "max_steps": self._max_steps,
            "trace_id": ctx.trace_id,
            "session_id": session_id,
            "user_id": ctx.user_id,
            "channel_id": ctx.channel_id,
            "industry_id": ctx.industry_id,
            "tool_calls": [],
            "pending_confirmation": None,
            "blocked_call_ids": [],
            "policy": None,
            "thread_id": thread_id,
        }
        # 登录态已核验：入口声明「本人已核验」时直接带入，不必让模型再问一次尾号。
        # 真实系统里用户登录后即完成实名/短信核验，会话级核验不该每轮重来。
        # 注意这不等于授权跨用户查询——``order.owner_matches`` 是独立规则，仍会拦
        # 「已核验的 H1 查 H2 的订单」。
        if ctx.verified and ctx.user_id:
            state["verified_user_id"] = ctx.user_id
        output = self._graph.invoke(
            state,
            config={
                "configurable": {"thread_id": thread_id},
                # 每个步骤约占 2 个超步（决策 + 执行），留出余量
                "recursion_limit": 2 * self._max_steps + 5,
            },
        )
        return to_turn_result(output, trace_id=ctx.trace_id)

    def resume(self, session_id: str, decision: PolicyDecision) -> AgentTurnResult:
        """从人工审批中断点恢复执行。

        顺序（DESIGN §7.7.5）：校验审批记录 → **原子消费** → 用冻结参数恢复图。
        业务执行本身仍走图里的 ``hitl_interrupt`` 节点，从而保证「重校验 → 幂等执行 →
        回填 ``execution_status``」只有一条路径。
        """
        from zkagent.core.orchestrator.runner import thread_id_for, to_turn_result

        approval_id = str(decision.approval_id or "")
        if not approval_id:
            raise ApprovalError("APPROVAL_NOT_FOUND", "恢复执行缺少 approval_id")

        approved_payload: dict[str, Any] | None = None
        if self._approval_store is not None:
            record = self._approval_store.get(approval_id)
            if record is None:
                raise ApprovalError("APPROVAL_NOT_FOUND", approval_id)
            if record.status == "expired" or is_expired(record, self._approval_ttl_hours):
                raise ApprovalError("APPROVAL_EXPIRED", approval_id)
            if decision.allowed:
                if record.status != "approved":
                    raise ApprovalError(
                        "APPROVAL_NOT_APPROVED", f"{approval_id} 当前为 {record.status}"
                    )
                # 消费即一次性：并发/重复恢复只有一个能成功
                record = self._approval_store.consume(approval_id)
                approved_payload = json.loads(
                    record.approved_payload_json or record.payload_json or "{}"
                )
            elif record.status != "rejected":
                raise ApprovalError(
                    "APPROVAL_ALREADY_RESOLVED", f"{approval_id} 当前为 {record.status}"
                )
        elif decision.allowed:
            approved_payload = decision.approved_payload

        resume_value = {
            "approved": bool(decision.allowed),
            "approval_id": approval_id,
            "payload": approved_payload,
            "actor": decision.approver_id,
            "mode": decision.approver_mode,
        }
        output = self._graph.invoke(
            Command(resume=resume_value),
            config={
                "configurable": {"thread_id": thread_id_for(session_id)},
                "recursion_limit": 2 * self._max_steps + 5,
            },
        )
        return to_turn_result(
            output, trace_id=str(output.get("trace_id") or "") or None
        )


def build_runner(
    *,
    handle: ChatModelHandle,
    specs: list[ToolSpec],
    executor: ToolExecutor,
    tool_context: ToolContext,
    context_engine: ContextEngine,
    checkpointer: Any = None,
    max_steps: int = 8,
    policy_engine: PolicyEngine | None = None,
    facts_provider: FactsProvider | None = None,
    approval_store: ApprovalStore | None = None,
    approval_ttl_hours: int = 24,
    tracer: Any = None,
) -> LangGraphRunner:
    """组装完整编排链路的便捷入口（组合根调用）。

    链路：句柄解包 → 工具转换 → 绑定工具 → 编译图 → 门面。
    """
    model = resolve_chat_model(handle)
    tools = specs_to_langchain_tools(specs, executor=executor, ctx=tool_context)
    bound = model.bind_tools(tools) if tools else model
    graph = build_graph(
        bound,
        tools,
        specs=specs,
        executor=executor,
        tool_context=tool_context,
        context_engine=context_engine,
        checkpointer=checkpointer,
        max_steps=max_steps,
        policy_engine=policy_engine,
        facts_provider=facts_provider,
        approval_store=approval_store,
        tracer=tracer,
    )
    return LangGraphRunner(
        graph,
        context_engine=context_engine,
        max_steps=max_steps,
        approval_store=approval_store,
        approval_ttl_hours=approval_ttl_hours,
    )


# --------------------------------------------------------------------- 单轮对话（面板用）


#: 角色 → 框架消息类。面板只传「角色 + 文本」，不接触任何框架类型。
_ROLE_MESSAGES: dict[str, type[BaseMessage]] = {
    "system": SystemMessage,
    "user": HumanMessage,
    "human": HumanMessage,
    "assistant": AIMessage,
    "ai": AIMessage,
}


def to_langchain_messages(messages: Sequence[Mapping[str, str]]) -> list[BaseMessage]:
    """把「角色 + 文本」列表转成框架消息（方向不可逆，仅接缝内使用）。"""
    converted: list[BaseMessage] = []
    for item in messages:
        role = str(item.get("role", "user")).strip().lower()
        message_cls = _ROLE_MESSAGES.get(role)
        if message_cls is None:
            raise ValueError(f"未知消息角色：{role!r}（可选 system/user/assistant）")
        converted.append(message_cls(content=str(item.get("content", ""))))
    return converted


def stream_chat(handle: ChatModelHandle, messages: Sequence[Mapping[str, str]]) -> Iterator[str]:
    """单轮流式对话：解包句柄 → 逐段产出文本。

    **为什么放在接缝里**：交互面板要边生成边展示，但 ``apps/`` 不允许 import 模型框架
    （边界规则 1）。因此把「流式」收敛到这里，对外只暴露文本片段。
    """
    model = resolve_chat_model(handle)
    try:
        for chunk in model.stream(to_langchain_messages(messages)):
            text = getattr(chunk, "content", "")
            if isinstance(text, str) and text:
                yield text
    except Exception as exc:  # 供应商 SDK 异常不得以裸栈穿透到面板
        raise ProviderCallError(
            f"模型调用失败（{type(exc).__name__}）：{exc}"
        ) from exc


def chat_once(handle: ChatModelHandle, messages: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """非流式单轮对话，返回文本与用量（供测试与脚本使用）。"""
    model = resolve_chat_model(handle)
    try:
        response = model.invoke(to_langchain_messages(messages))
    except Exception as exc:  # 与图内 llm_decide 保持同一种错误类型
        raise ProviderCallError(
            f"模型调用失败（{type(exc).__name__}）：{exc}"
        ) from exc
    content = getattr(response, "content", "")
    usage = getattr(response, "usage_metadata", None) or {}
    return {
        "text": content if isinstance(content, str) else str(content),
        "usage": dict(usage) if isinstance(usage, Mapping) else {},
    }
