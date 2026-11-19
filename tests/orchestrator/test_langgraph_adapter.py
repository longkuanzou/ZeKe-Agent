"""接缝一专项测试（允许 import 框架：文件名为 ``test_langgraph_adapter.py``）。

重点验证三件事：

1. ``ToolSpec`` 是工具定义的唯一来源（框架工具的 name / description / schema 全部来自它）；
2. 消息转换**必须保留 tool_calls**——丢失会导致 OpenAI 兼容接口 400（这是真实模型
   冒烟测试暴露过的缺陷，此处固化为回归用例）；
3. 最大步数熔断与脚本写错时的显式失败。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from zkagent.config import (
    Container,
    default_industry_packs_dir,
    load_fewshots,
    load_industry_pack,
    load_system_prompt,
    load_tools_config,
    resolve_paths,
    load_settings,
)
from zkagent.core.context.engine import ContextEngine
from zkagent.core.llm import FakeProvider, tool_call_step, answer_step
from zkagent.core.orchestrator.langgraph_adapter import (
    ModelNotConfiguredError,
    _from_lc,
    _to_lc,
    build_graph,
    resolve_chat_model,
    specs_to_langchain_tools,
)
from zkagent.core.tools.executor import InMemoryIdempotencyStore, ToolExecutor
from zkagent.core.types import ChatMessage, ToolContext

PACK_DIR = default_industry_packs_dir() / "appliance"


def _context_engine() -> ContextEngine:
    pack = load_industry_pack(PACK_DIR)
    return ContextEngine(
        system_prompt=load_system_prompt(pack, PACK_DIR),
        fewshots=load_fewshots(pack, PACK_DIR),
    )


def _specs(backend):
    pack = load_industry_pack(PACK_DIR)
    tools_config = load_tools_config(pack, PACK_DIR)
    from zkagent.core.tools.registry import apply_industry_overrides

    return apply_industry_overrides(backend.list_tools(), tools_config)


# --------------------------------------------------------------------- 消息转换


def test_ai_message_round_trip_preserves_tool_calls() -> None:
    """回归用例：tool_calls 一旦丢失，tool 消息就没有前置声明，接口会报 400。"""
    original = AIMessage(
        content="",
        tool_calls=[{"name": "get_order", "args": {"order_id": "A1001"}, "id": "call-1"}],
    )
    neutral = _from_lc(original)
    assert neutral is not None
    assert neutral.tool_calls == [
        {"name": "get_order", "args": {"order_id": "A1001"}, "id": "call-1"}
    ]

    restored = _to_lc(neutral)
    assert isinstance(restored, AIMessage)
    assert restored.tool_calls[0]["name"] == "get_order"
    assert restored.tool_calls[0]["id"] == "call-1"


def test_tool_message_round_trip_keeps_id_and_name() -> None:
    """验证：tool 消息往返保留 ``tool_call_id`` 与 ``name``——两者缺一都无法与调用对上号。"""
    neutral = _from_lc(ToolMessage(content="{}", tool_call_id="call-1", name="get_order"))
    assert neutral is not None and neutral.role == "tool"
    restored = _to_lc(neutral)
    assert isinstance(restored, ToolMessage)
    assert restored.tool_call_id == "call-1"
    assert restored.name == "get_order"


def test_tool_message_without_call_id_is_rejected() -> None:
    """验证：无 ``tool_call_id`` 的 tool 消息直接抛错——下游接口会因无法配对而 400。"""
    with pytest.raises(ValueError, match="tool_call_id"):
        _to_lc(ChatMessage(role="tool", content="x"))


def test_content_edge_cases_are_normalized() -> None:
    """content 可能是空列表或内容块列表，都必须归一为纯文本。"""
    empty = _from_lc(AIMessage(content=[]))
    assert empty is not None and empty.content == ""

    blocks = _from_lc(HumanMessage(content=[{"type": "text", "text": "你好"}]))
    assert blocks is not None and blocks.content == "你好"


# --------------------------------------------------------------------- 句柄解包


def test_fake_handle_resolves_to_scripted_model() -> None:
    """验证：``fake`` provider 句柄能解包成脚本化模型——它是全部确定性用例的地基。"""
    handle = FakeProvider([answer_step("你好")]).chat_model()
    model = resolve_chat_model(handle)
    message = model.invoke([HumanMessage("hi")])
    assert message.content == "你好"


def test_deepseek_handle_without_key_fails_loudly(monkeypatch) -> None:
    """缺密钥必须显式失败，不得静默降级为 Fake。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    from zkagent.core.llm import DeepSeekProvider

    with pytest.raises(ModelNotConfiguredError, match="DEEPSEEK_API_KEY"):
        resolve_chat_model(DeepSeekProvider().chat_model())


def test_unknown_protocol_is_rejected_by_the_contract() -> None:
    """协议取值由契约层（``Literal``）约束：非法协议根本构造不出句柄。"""
    from pydantic import ValidationError

    from zkagent.core.types import ChatModelHandle

    with pytest.raises(ValidationError):
        ChatModelHandle(provider_id="mystery", model_id="x", protocol="carrier-pigeon")


def test_unknown_protocol_is_rejected_by_the_seam() -> None:
    """绕过校验的句柄（如手工构造的字典）仍必须被接缝拒绝，不得猜一个实现。"""
    from zkagent.core.types import ChatModelHandle

    forged = ChatModelHandle.model_construct(
        provider_id="mystery",
        model_id="x",
        protocol="carrier-pigeon",
        base_url=None,
        api_key_env=None,
        requires_api_key=False,
        params={},
    )
    with pytest.raises(ModelNotConfiguredError, match="未知的模型 provider 协议"):
        resolve_chat_model(forged)


def test_openai_compatible_vendor_passes_model_and_base_url(monkeypatch) -> None:
    """通义 / 智谱 / Moonshot / 自建网关都走这条路径：差异只在 base_url 与 model_id。

    这也是「新增一家 OpenAI 兼容供应商不需要改接缝代码」的机制保证。
    """
    monkeypatch.setenv("DASHSCOPE_API_KEY", "unit-test-key")
    from zkagent.core.llm import RegistryProvider

    model = resolve_chat_model(RegistryProvider("qwen", "qwen-max").chat_model())

    assert model.model_name == "qwen-max"
    assert (model.openai_api_base or "").startswith("https://dashscope")


def test_custom_endpoint_overrides_base_url(monkeypatch) -> None:
    """本地 vLLM / LM Studio：免密钥 + 自带地址 + 自带模型。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from zkagent.core.llm import RegistryProvider

    handle = RegistryProvider(
        "custom", "local-model", base_url="http://localhost:8000/v1"
    ).chat_model()
    model = resolve_chat_model(handle)

    assert model.model_name == "local-model"
    assert model.openai_api_base == "http://localhost:8000/v1"
    assert model.openai_api_key.get_secret_value() == "not-required", "本地端点用占位密钥"


def test_anthropic_protocol_is_supported(monkeypatch) -> None:
    """验证：Anthropic 协议走独立实现，模型名正确透传。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unit-test-key")
    from zkagent.core.llm import RegistryProvider

    model = resolve_chat_model(RegistryProvider("anthropic", "claude-test").chat_model())

    assert getattr(model, "model", None) == "claude-test"


def test_keyed_provider_without_key_fails_loudly(monkeypatch) -> None:
    """验证：需要密钥的 provider 缺密钥时显式失败——不得静默降级成 Fake 或别的供应商。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from zkagent.core.llm import RegistryProvider

    with pytest.raises(ModelNotConfiguredError, match="OPENAI_API_KEY"):
        resolve_chat_model(RegistryProvider("openai", "gpt-test").chat_model())


def test_missing_key_error_is_a_config_error() -> None:
    """缺密钥属于**配置问题**，不是未知崩溃：必须与其它配置错误走同一条上报路径。

    否则 CLI 会直接把异常栈甩给用户（这是本次多供应商改造中实测暴露的问题）。
    """
    from zkagent.core.llm import ProviderConfigError

    assert issubclass(ModelNotConfiguredError, ProviderConfigError)


# --------------------------------------------------------------------- 工具转换


def test_framework_tools_are_projected_from_tool_spec(backend, ctx: ToolContext) -> None:
    """框架工具的元数据全部来自 ToolSpec —— 方向是「ToolSpec → 工具」，不可逆。"""
    specs = _specs(backend)
    executor = ToolExecutor(backend, InMemoryIdempotencyStore(), sleeper=lambda _s: None)
    tools = specs_to_langchain_tools(specs, executor=executor, ctx=ctx)

    assert {tool.name for tool in tools} == {spec.name for spec in specs}
    by_name = {spec.name: spec for spec in specs}
    for tool in tools:
        spec = by_name[tool.name]
        assert tool.description == spec.description
        assert tool.args == {
            key: value
            for key, value in spec.parameters.get("properties", {}).items()
        }


def test_tool_callback_goes_through_executor(backend, ctx: ToolContext) -> None:
    """验证：框架工具被真正调用时走的是 ``ToolExecutor``，而不是直连后端。

    只有经过执行器才有幂等、重试、超时这层保护。
    """
    specs = _specs(backend)
    executor = ToolExecutor(backend, InMemoryIdempotencyStore(), sleeper=lambda _s: None)
    tools = {
        tool.name: tool
        for tool in specs_to_langchain_tools(specs, executor=executor, ctx=ctx)
    }

    payload = tools["get_order"].invoke({"order_id": "A1001"})
    assert payload["ok"] is True and payload["data"]["order_id"] == "A1001"


def test_write_tool_without_idempotency_key_is_rejected_by_executor(
    backend, ctx: ToolContext
) -> None:
    """验证：从框架侧调用写工具，缺幂等键同样被拒——保护不会被「绕过 executor」的入口绕过。"""
    specs = _specs(backend)
    executor = ToolExecutor(backend, InMemoryIdempotencyStore(), sleeper=lambda _s: None)
    tools = {
        tool.name: tool
        for tool in specs_to_langchain_tools(specs, executor=executor, ctx=ctx)
    }

    payload = tools["cancel_order"].invoke({"order_id": "A1002", "reason": "测试"})
    assert payload["ok"] is False
    assert payload["error_code"] == "INVALID_ARGUMENT"


# --------------------------------------------------------------------- 图行为


class _RecordingFake:
    """包装脚本化模型，记录每次决策收到的消息（用于验证上下文回放）。"""

    def __init__(self, model) -> None:
        self.model = model
        self.prompts: list[list] = []

    def bind_tools(self, tools, **kwargs):
        self.model.bind_tools(tools, **kwargs)
        return self

    def invoke(self, messages, **kwargs):
        self.prompts.append(list(messages))
        return self.model.invoke(messages, **kwargs)


def _build(backend, ctx: ToolContext, script: list[dict], *, max_steps: int = 8):
    specs = _specs(backend)
    executor = ToolExecutor(backend, InMemoryIdempotencyStore(), sleeper=lambda _s: None)
    tools = specs_to_langchain_tools(specs, executor=executor, ctx=ctx)
    model = _RecordingFake(resolve_chat_model(FakeProvider(script).chat_model()))
    bound = model.bind_tools(tools)
    graph = build_graph(
        bound,
        tools,
        specs=specs,
        executor=executor,
        tool_context=ctx,
        context_engine=_context_engine(),
        checkpointer=None,
        max_steps=max_steps,
    )
    return graph, model


def _initial_state(text: str, ctx: ToolContext, *, max_steps: int = 8) -> dict:
    return {
        "messages": [HumanMessage(content=text)],
        "steps": 0,
        "max_steps": max_steps,
        "trace_id": ctx.trace_id,
        "session_id": ctx.session_id,
        "user_id": ctx.user_id,
        "channel_id": ctx.channel_id,
        "industry_id": ctx.industry_id,
        "tool_calls": [],
    }


def test_graph_executes_tool_then_answers(backend, ctx: ToolContext) -> None:
    """验证：图的主干路径——调工具 → 拿到结果 → 作答，``tool_calls`` 如实记录。"""
    script = [tool_call_step("get_order", {"order_id": "A1001"}), answer_step("订单已查到")]
    graph, _ = _build(backend, ctx, script)

    output = graph.invoke(_initial_state("查订单 A1001", ctx))

    assert output["status"] == "completed"
    assert output["final_text"] == "订单已查到"
    assert output["tool_calls"] == [
        {"name": "get_order", "ok": True, "error_code": None}
    ]


def test_second_decision_sees_tool_message_with_preceding_tool_calls(
    backend, ctx: ToolContext
) -> None:
    """真实模型 400 的根因：第二次决策的上下文必须包含「带 tool_calls 的 AI 消息 + 对应 tool 消息」。"""
    script = [tool_call_step("get_order", {"order_id": "A1001"}), answer_step("好的")]
    graph, model = _build(backend, ctx, script)

    graph.invoke(_initial_state("查订单 A1001", ctx))

    assert len(model.prompts) >= 2, "应当发生两次决策（先调工具，再作答）"
    second = model.prompts[1]
    tool_messages = [m for m in second if isinstance(m, ToolMessage)]
    assert tool_messages, "第二次决策必须带上工具返回"
    assert any(
        isinstance(m, AIMessage) and m.tool_calls for m in second
    ), "工具返回之前必须存在带 tool_calls 的 AI 消息"


def test_max_steps_fuses_instead_of_looping_forever(backend, ctx: ToolContext) -> None:
    """脚本耗尽后重复最后一步（工具调用），因此必然触发熔断。"""
    script = [tool_call_step("get_order", {"order_id": "A1001"})]
    graph, _ = _build(backend, ctx, script, max_steps=2)

    output = graph.invoke(_initial_state("查订单 A1001", ctx, max_steps=2))

    assert output["status"] == "max_steps"
    assert "最大处理步数" in output["final_text"]


def test_script_with_unknown_tool_fails_at_decision(backend, ctx: ToolContext) -> None:
    """脚本写了未注入的工具名时必须报错，避免「测试通过但工具根本没被调用」。"""
    script = [tool_call_step("not_a_tool", {})]
    graph, _ = _build(backend, ctx, script)

    with pytest.raises(ValueError, match="未注入的工具"):
        graph.invoke(_initial_state("随便", ctx))


def test_failed_tool_result_is_reported_not_rewritten(backend, ctx: ToolContext) -> None:
    """工具失败必须如实回填（此处 A1002 无物流记录）。"""
    script = [
        tool_call_step("get_logistics", {"order_id": "A1002"}),
        answer_step("没有物流记录"),
    ]
    graph, _ = _build(backend, ctx, script)

    output = graph.invoke(_initial_state("查物流 A1002", ctx))

    assert output["tool_calls"] == [
        {"name": "get_logistics", "ok": False, "error_code": "NOT_FOUND"}
    ]


def test_build_graph_rejects_spec_without_tool(backend, ctx: ToolContext) -> None:
    """验证：``build_graph`` 拒绝「声明了 spec 却没有对应框架工具」的组合，避免模型调用到空实现。"""
    specs = _specs(backend)
    executor = ToolExecutor(backend, InMemoryIdempotencyStore(), sleeper=lambda _s: None)
    tools = specs_to_langchain_tools(specs[:1], executor=executor, ctx=ctx)

    with pytest.raises(ValueError, match="没有对应的框架工具"):
        build_graph(
            resolve_chat_model(FakeProvider([answer_step("x")]).chat_model()),
            tools,
            specs=specs,
            executor=executor,
            tool_context=ctx,
            context_engine=_context_engine(),
        )


# --------------------------------------------------------------------- 组合根


def test_container_builds_runner_with_fake_provider(tmp_path) -> None:
    """组合根能装配出可用的编排器（会话级），并绑定身份到工具上下文。"""
    settings = load_settings(project_root=tmp_path, db_path=tmp_path / "business.db")
    paths = resolve_paths(settings)
    pack = load_industry_pack(paths.industry_pack_dir)

    container = Container.build(settings)
    # 空库也能装配；订单查询会如实返回 NOT_FOUND（不编造数据）
    runner, ctx = container.make_runner(
        user_id="U1", session_id="s1", provider="fake", task="查订单 A1001"
    )
    assert ctx.session_id == "s1"
    assert ctx.user_id == "U1"
    assert ctx.industry_id == pack.id
    result = runner.run("s1", "查订单 A1001", ctx)
    assert result.status == "completed"
    assert result.tool_calls[0]["name"] == "get_order"
    assert result.tool_calls[0]["ok"] is False


def test_container_rejects_unknown_demo_task() -> None:
    """验证：fake provider 必须配一个行业包里存在的确定性脚本，否则显式报错。

    防「fake 模式跑到一半没脚本可放」这种静默跑偏。
    """
    container = Container.build(load_settings())
    with pytest.raises(Exception, match="需要确定性脚本"):
        container.make_runner(provider="fake", task="行业包里没有的任务")


def test_container_resolves_deepseek_when_key_present(monkeypatch) -> None:
    """验证：``provider=auto`` 在有密钥时优先选 deepseek——供应商选择靠配置而非硬编码。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-used")
    container = Container.build(load_settings())
    handle = container.resolve_model_handle(provider="auto")
    assert handle.provider_id == "deepseek"


def test_resume_requires_approval_id() -> None:
    """P4 已实现恢复：没有审批单号必须显式失败，不能「凭空续跑」。"""
    from zkagent.core.policy.errors import ApprovalError
    from zkagent.core.types import PolicyDecision

    container = Container.build(load_settings())
    runner, _ = container.make_runner(provider="fake", task="查订单 A1001")
    with pytest.raises(ApprovalError, match="APPROVAL_NOT_FOUND"):
        runner.resume(
            "s1",
            PolicyDecision(
                action="create_refund_ticket",
                allowed=True,
                decision="require_confirmation",
                reason="",
                rule_id="r1",
            ),
        )
