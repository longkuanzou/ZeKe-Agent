"""编排图里的策略节点：拒绝回填、核验放行、挂起与恢复执行。"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from zkagent.backends.mock.approvals import SqliteApprovalStore
from zkagent.backends.mock.facts import MockFactsProvider
from zkagent.config import (
    default_industry_packs_dir,
    load_fewshots,
    load_industry_pack,
    load_system_prompt,
)
from zkagent.core.context.engine import ContextEngine
from zkagent.core.llm import FakeProvider, answer_step, tool_call_step
from zkagent.core.orchestrator.langgraph_adapter import (
    LangGraphRunner,
    build_graph,
    open_checkpointer,
    resolve_chat_model,
    specs_to_langchain_tools,
)
from zkagent.core.policy import RulePolicyEngine, load_rule_set
from zkagent.core.tools.executor import InMemoryIdempotencyStore, ToolExecutor
from zkagent.core.types import PolicyDecision, ToolContext

PACK_DIR = default_industry_packs_dir() / "appliance"


def _context_engine() -> ContextEngine:
    pack = load_industry_pack(PACK_DIR)
    return ContextEngine(
        system_prompt=load_system_prompt(pack, PACK_DIR),
        fewshots=load_fewshots(pack, PACK_DIR),
    )


def _specs(backend): # 返回tollspec
    from zkagent.config import load_tools_config
    from zkagent.core.tools.registry import apply_industry_overrides

    pack = load_industry_pack(PACK_DIR)
    return apply_industry_overrides(backend.list_tools(), load_tools_config(pack, PACK_DIR))


def _engine(tmp_path, rules: str) -> RulePolicyEngine:
    path = tmp_path / "rules.yaml"
    path.write_text("version: 1\nrules:\n" + rules, encoding="utf-8 ")
    return RulePolicyEngine(load_rule_set(path))


def _graph(backend, ctx, script, engine, db, *, checkpointer=None):
    specs = _specs(backend)
    executor = ToolExecutor(backend, InMemoryIdempotencyStore(), sleeper=lambda _s: None)
    tools = specs_to_langchain_tools(specs, executor=executor, ctx=ctx)
    model = resolve_chat_model(FakeProvider(script).chat_model())
    graph = build_graph(
        model.bind_tools(tools),
        tools,
        specs=specs,
        executor=executor,
        tool_context=ctx,
        context_engine=_context_engine(),
        checkpointer=checkpointer,
        max_steps=6,
        policy_engine=engine,
        facts_provider=MockFactsProvider(db),
        approval_store=SqliteApprovalStore(db),
    )
    return graph, executor


def _state(text: str, ctx: ToolContext, *, verified: bool = False, thread: str = "t1") -> dict:
    return {
        "messages": [HumanMessage(content=text)],
        "steps": 0,
        "max_steps": 6,
        "trace_id": ctx.trace_id,
        "session_id": ctx.session_id,
        "user_id": ctx.user_id,
        "channel_id": ctx.channel_id,
        "industry_id": ctx.industry_id,
        "tool_calls": [],
        "pending_confirmation": None,
        "blocked_call_ids": [],
        "thread_id": thread,
        "verified_user_id": ctx.user_id if verified else None,
    }


def test_unverified_order_read_is_denied_without_execution(
    backend, business_db, ctx, tmp_path
) -> None:
    """未核验时订单类工具不得执行，并回填可执行的引导（verify_user）。"""
    engine = _engine(
        tmp_path,
        """
  - id: order.requires_verified
    match: {action: [get_order], verified: false}
    effect: deny
    suggested_action: verify_user
""",
    )
    script = [tool_call_step("get_order", {"order_id": "A1001"}), answer_step("请先核验身份")]
    graph, _ = _graph(backend, ctx, script, engine, business_db)

    output = graph.invoke(_state("查订单 A1001", ctx, verified=False))

    assert output["status"] == "completed"
    assert output["tool_calls"] == [], "被拒绝的调用不得执行"
    tool_messages = [m for m in output["messages"] if isinstance(m, ToolMessage)]
    payload = json.loads(str(tool_messages[-1].content))
    assert payload["error_code"] == "POLICY_DENIED"
    assert payload["suggested_action"] == "verify_user"


def test_verified_order_read_executes(backend, business_db, ctx, tmp_path) -> None:
    """验证：同一条规则，身份核验后订单工具**正常执行**并产生 tool_calls。

    与上一条成对：证明拦住调用的是 ``verified`` 事实，而不是规则本身把 get_order 封死了。
    """
    engine = _engine(
        tmp_path,
        """
  - id: order.requires_verified
    match: {action: [get_order], verified: false}
    effect: deny
""",
    )
    script = [tool_call_step("get_order", {"order_id": "A1001"}), answer_step("订单已查到")]
    graph, _ = _graph(backend, ctx, script, engine, business_db)

    output = graph.invoke(_state("查订单 A1001", ctx, verified=True))

    assert output["tool_calls"] == [{"name": "get_order", "ok": True, "error_code": None}]
    assert output["final_text"] == "订单已查到"


ARGS = {"order_id": "A1001", "amount_cents": 699900, "reason": "质量问题"}
ESCALATE_RULE = """
  - id: refund.escalate
    priority: 40
    match: {action: [create_refund_ticket]}
    effect: escalate
"""


def _refund_count(db) -> int:
    return db.count("refund_requests")


def test_hitl_suspends_then_resume_executes_once(
    backend, business_db, ctx, tmp_path
) -> None:
    """验证：HITL 全流程——挂起阶段**绝不先执行**，批准后恢复执行**且只执行一次**。

    三段断言：挂起时退款数为 0；批准后真正落库（1 笔）；重复 resume 抛
    ``APPROVAL_NOT_CONSUMABLE`` 后仍是 1 笔。这是防重复扣款的端到端防线。
    """
    engine = _engine(tmp_path, ESCALATE_RULE)
    script = [tool_call_step("create_refund_ticket", ARGS), answer_step("已提交审批")]
    checkpointer = open_checkpointer(tmp_path / "cp.db")
    graph, _ = _graph(backend, ctx, script, engine, business_db, checkpointer=checkpointer)
    store = SqliteApprovalStore(business_db)
    runner = LangGraphRunner(
        graph, context_engine=_context_engine(), max_steps=6, approval_store=store
    )

    pending_turn = runner.run("hitl-1", "我要退 A1001 全额", ctx)
    assert pending_turn.status == "awaiting_confirmation"
    approval_id = str((pending_turn.pending_confirmation or {})["approval_id"])
    record = store.get(approval_id)
    assert record is not None and record.status == "pending"
    assert _refund_count(business_db) == 0, "挂起阶段绝不能先执行"

    store.resolve(
        approval_id,
        approve=True,
        approver_id="alice",
        approver_mode="token",
        approved_payload_json=json.dumps(ARGS, sort_keys=True),
    )
    done = runner.resume(
        "hitl-1",
        PolicyDecision(
            action="create_refund_ticket",
            allowed=True,
            decision="require_confirmation",
            reason="人工批准",
            rule_id="refund.escalate",
            approval_id=approval_id,
            approver_id="alice",
            approver_mode="token",
        ),
    )
    assert done.status == "completed"
    assert _refund_count(business_db) == 1, "批准后必须真正落库"
    assert store.get(approval_id).execution_status == "succeeded"

    from zkagent.core.policy.errors import ApprovalError

    with pytest.raises(ApprovalError, match="APPROVAL_NOT_CONSUMABLE"):
        runner.resume(
            "hitl-1",
            PolicyDecision(
                action="create_refund_ticket",
                allowed=True,
                decision="require_confirmation",
                reason="重复批准",
                rule_id="refund.escalate",
                approval_id=approval_id,
            ),
        )
    assert _refund_count(business_db) == 1, "重复恢复不得产生第二笔退款"


def test_tampered_payload_is_rejected(
    backend, business_db, ctx, tmp_path
) -> None:
    """审批后参数被改：指纹不一致，拒绝执行（I2）。"""
    engine = _engine(tmp_path, ESCALATE_RULE)
    script = [tool_call_step("create_refund_ticket", ARGS), answer_step("已提交审批")]
    checkpointer = open_checkpointer(tmp_path / "cp2.db")
    graph, _ = _graph(backend, ctx, script, engine, business_db, checkpointer=checkpointer)
    store = SqliteApprovalStore(business_db)
    runner = LangGraphRunner(
        graph, context_engine=_context_engine(), max_steps=6, approval_store=store
    )

    pending_turn = runner.run("hitl-2", "我要退 A1001 全额", ctx)
    approval_id = str((pending_turn.pending_confirmation or {})["approval_id"])
    tampered = dict(ARGS, amount_cents=100)
    store.resolve(
        approval_id,
        approve=True,
        approver_id="alice",
        approver_mode="token",
        approved_payload_json=json.dumps(tampered, sort_keys=True),
    )

    done = runner.resume(
        "hitl-2",
        PolicyDecision(
            action="create_refund_ticket",
            allowed=True,
            decision="require_confirmation",
            reason="人工批准",
            rule_id="refund.escalate",
            approval_id=approval_id,
        ),
    )
    assert done.status == "completed"
    assert _refund_count(business_db) == 0, "参数被改后不得执行"


def test_suspended_batch_answers_every_tool_call(
    backend, business_db, ctx, tmp_path
) -> None:
    """挂起时同批每个 tool_call 都必须有 tool 消息。

    回归用例：少一条就会让下一次模型调用被 OpenAI 兼容接口以 400 拒绝
    （"assistant message with tool_calls must be followed by tool messages"）。
    """
    engine = _engine(tmp_path, ESCALATE_RULE)
    two_calls = {
        "tool_calls": [
            {"name": "get_order", "args": {"order_id": "A1001"}},
            {"name": "create_refund_ticket", "args": ARGS},
        ]
    }
    script = [two_calls, answer_step("已提交审批")]
    checkpointer = open_checkpointer(tmp_path / "cp-batch.db")
    graph, _ = _graph(backend, ctx, script, engine, business_db, checkpointer=checkpointer)

    output = graph.invoke(
        _state("查订单并退款", ctx, verified=True, thread="batch-1"),
        config={"configurable": {"thread_id": "batch-1"}},
    )

    pending = output["pending_confirmation"] or {}
    assert pending, "该批次应挂起"
    answered = {
        message.tool_call_id
        for message in output["messages"]
        if isinstance(message, ToolMessage)
    }
    last_ai = next(
        message
        for message in reversed(output["messages"])
        if isinstance(message, AIMessage) and message.tool_calls
    )
    call_ids = {call["id"] for call in last_ai.tool_calls}
    assert len(call_ids) == 2, "本用例是「一个放行 + 一个挂起」的批量调用"
    assert call_ids - answered == {pending["tool_call_id"]}, (
        "除挂起那条外，每个 tool_call 都必须先有回执"
    )


def test_recheck_blocks_when_business_state_changed(
    backend, business_db, ctx, tmp_path
) -> None:
    """I4：批准后订单状态变了，恢复时重判定必须拦住（不得照旧执行）。"""
    rules = ESCALATE_RULE + """
  - id: refund.block_unshipped_order
    priority: 20
    match: {action: [create_refund_ticket], order_status: [paid]}
    effect: deny
    suggested_action: cancel_order
"""
    engine = _engine(tmp_path, rules)
    script = [tool_call_step("create_refund_ticket", ARGS), answer_step("已提交审批")]
    checkpointer = open_checkpointer(tmp_path / "cp-recheck.db")
    graph, _ = _graph(backend, ctx, script, engine, business_db, checkpointer=checkpointer)
    store = SqliteApprovalStore(business_db)
    runner = LangGraphRunner(
        graph, context_engine=_context_engine(), max_steps=6, approval_store=store
    )

    pending_turn = runner.run("hitl-3", "我要退 A1001 全额", ctx)
    approval_id = str((pending_turn.pending_confirmation or {})["approval_id"])
    store.resolve(
        approval_id,
        approve=True,
        approver_id="alice",
        approver_mode="token",
        approved_payload_json=json.dumps(ARGS, sort_keys=True),
    )
    with business_db.transaction() as conn:
        conn.execute("UPDATE orders SET status = 'paid' WHERE order_id = 'A1001'")

    done = runner.resume(
        "hitl-3",
        PolicyDecision(
            action="create_refund_ticket",
            allowed=True,
            decision="require_confirmation",
            reason="人工批准",
            rule_id="refund.escalate",
            approval_id=approval_id,
        ),
    )
    assert done.status == "completed"
    assert _refund_count(business_db) == 0, "状态已变，不得执行"
    final = store.get(approval_id)
    assert final.execution_status == "failed"
    assert final.execution_error_code == "POLICY_RECHECK_FAILED"
