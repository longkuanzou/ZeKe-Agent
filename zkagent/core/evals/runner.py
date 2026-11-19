"""评估执行器：逐条跑 Agent，汇总 Pass@1 / Pass^k 与失败归因。"""

from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

from zkagent.backends.mock.db import Database
from zkagent.backends.mock.seed import seed_database
from zkagent.core.context.engine import ContextEngine
from zkagent.core.evals.assertions import check_case
from zkagent.core.evals.models import AssertionResult, CaseResult, EvalCase
from zkagent.core.types import ChatMessage, StatusBar

__all__ = ["EvalRunner", "run_context_compression"]

#: 断言名 → 失败归因。
_ATTRIBUTION = {
    "tools_called": "tool_not_called",
    "tools_forbidden": "wrong_tool",
    "status": "status_mismatch",
    "answer_text": "text_mismatch",
    "db": "db_state_mismatch",
    "policy": "policy_violation",
    "trace": "trace_mismatch",
    "citations": "citation_invalid",
}


def _failure_reasons(checks: list[AssertionResult]) -> list[str]:
    """把失败的断言名翻译成归因标签（便于跨版本对比「哪一类经常挂」）。

    :param checks: 一条用例的全部断言结果。
    :returns: 失败项的归因标签列表；全部通过时为空列表。
    """
    return [
        _ATTRIBUTION.get(check.name, check.name)
        for check in checks
        if not check.passed
    ]


class EvalRunner:
    """驱动现有 ``Container`` 的端到端评估器（不直接调用工具）。"""

    def __init__(
        self,
        container,
        *,
        provider: str | None = "fake",
        k: int = 1,
        isolate: bool = True,
    ) -> None:
        self.container = container
        self.provider = provider
        self.k = max(1, k)
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        #: 评估实际使用的业务库路径（隔离时为临时库），供报告标注。
        self.db_path: str = str(container.backend.database.path)
        if isolate:
            self._isolate_database()

    def _isolate_database(self) -> None:
        """为本次评估创建临时 SQLite 并灌入种子，避免污染演示库（M20）。"""
        self._temp_dir = tempfile.TemporaryDirectory(prefix="zkagent-eval-")
        db = Database(Path(self._temp_dir.name) / "business.db")
        db.reset()
        seed_dir = Path(self.container.paths.industry_pack_dir) / self.container.pack.seed_data
        seed_database(db, seed_dir)
        # 后端 / 事实 / 审批共用同一份临时库；工具执行器持有的是后端对象，随之切换。
        self.container.backend._db = db  # noqa: SLF001 - 组合根式装配，测试需替换隔离库
        self.container.facts_provider._db = db  # noqa: SLF001
        self.container.approval_store._db = db  # noqa: SLF001
        self.db_path = str(db.path)

    def close(self) -> None:
        """释放临时库（不调用也能在进程退出时清理）。"""
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None

    def run_case(self, case: EvalCase) -> CaseResult:
        """跑一条用例 ``k`` 次（每次独立 session），汇总 Pass@1 与 Pass^k。

        每次都走完整的 ``Container.make_runner`` 端到端链路，而不是直接调工具——
        否则测不到编排、策略与上下文工程。

        :param case: 用例定义（含可选的确定性 ``script``）。
        :returns: 含逐次结果、断言明细与失败归因的用例结果。
        """
        attempts: list[bool] = []
        last_checks: list[AssertionResult] = []
        last_result = None
        session_base = case.session or f"eval-{case.id}"

        for index in range(self.k):
            session = f"{session_base}-{index}-{uuid4().hex[:6]}"
            runner, ctx = self.container.make_runner(
                user_id=case.user_id,
                session_id=session,
                provider=self.provider,
                task=case.task,
                script=case.script or None,
                intent=case.intent,
                verified=case.verified,
            )
            last_result = runner.run(session, case.task, ctx)
            events = self.container.tracer.events(ctx.trace_id)
            self.container.tracer.flush(ctx.trace_id)

            last_checks = check_case(
                case, last_result, events=events, db=self.container.backend.database
            )
            attempts.append(all(check.passed for check in last_checks))

        assert last_result is not None
        return CaseResult(
            case_id=case.id,
            category=case.category,
            passed=all(attempts),
            attempts=attempts,
            assertions=last_checks,
            failures=_failure_reasons(last_checks),
            final_text=last_result.final_text,
            trace_id=last_result.trace_id,
            usage=dict(last_result.usage or {}),
            latency_ms=int(last_result.latency_ms or 0),
        )

    def run_all(self, cases: list[EvalCase]) -> list[CaseResult]:
        """顺序跑完所有用例（不并发：共用同一个库与会话状态）。

        :param cases: 用例列表。
        :returns: 与输入同序的结果列表。
        """
        return [self.run_case(case) for case in cases]


def _demo_history(turns: int) -> list[ChatMessage]:
    """造一段合成的多轮历史，用于验证压缩管线（不依赖真实模型）。

    每轮包含「提问 → 带 tool_calls 的 AI 消息 → 大载荷 tool 消息 → 回答」，
    故意让 tool 内容很长，才能观察到 JSON 压缩的收益。

    :param turns: 轮数。
    :returns: 展开后的消息列表，长度约为 ``turns * 4``。
    """
    messages: list[ChatMessage] = []
    for index in range(1, turns + 1):
        messages.extend(
            [
                ChatMessage(role="user", content=f"第{index}问：订单 H26{index:02d} 到哪了"),
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "name": "get_order",
                            "args": {"order_id": f"H26{index:02d}"},
                            "id": f"call-{index}",
                        }
                    ],
                ),
                ChatMessage(
                    role="tool",
                    name="get_order",
                    tool_call_id=f"call-{index}",
                    content=(
                        '{"ok":true,"order_id":"H26%02d","status":"shipped",'
                        '"timeline":["%s"]}'
                    )
                    % (index, "x" * 300),
                ),
                ChatMessage(role="assistant", content=f"第{index}答：运输中"),
            ]
        )
    return messages


def run_context_compression(*, turns: int = 25) -> dict[str, Any]:
    """压缩前后对照（字符 / 估算 token / 关键信息保持率）。"""
    engine = ContextEngine(system_prompt="你是客服助手。", keep_recent_turns=4)
    history = _demo_history(turns)
    bundle = engine.build(
        status=StatusBar(
            industry_id="homeapp", session_id="ctx-eval", user_id="H1", channel_id="cli"
        ),
        history=history,
    )
    summary = bundle.history[0].content if bundle.history else ""
    recent_users = [m.content for m in bundle.history if m.role == "user"][-4:]
    retention = (
        "第1问" in summary
        and len(recent_users) == 4
        and recent_users[-1] == f"第{turns}问：订单 H26{turns:02d} 到哪了"
    )
    before = bundle.stats["chars_before"]
    after = bundle.stats["chars_after"]
    return {
        "turns": turns,
        "chars_before": before,
        "chars_after": after,
        "ratio": round(after / max(1, before), 4),
        "estimated_tokens_before": round(before / 1.0),
        "estimated_tokens_after": round(after / 1.0),
        "summarized_turns": bundle.stats["summarized_turns"],
        "retention_passed": retention,
    }
