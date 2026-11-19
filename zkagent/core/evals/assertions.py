"""四层断言：工具调用 / 回答文本 / 数据库终态 / 策略与 Trace。"""

from __future__ import annotations

from typing import Any

from zkagent.core.evals.citations import validate_citations
from zkagent.core.evals.models import AssertionResult, EvalCase

__all__ = ["check_case"]


def _norm(text: str) -> str:
    """归一化文本用于包含性断言：去掉所有空白并转小写。

    :param text: 原始文本。
    :returns: 去空白、小写化后的字符串，避免空格/大小写导致误判。
    """
    return "".join(str(text).split()).lower()


def _tool_names(result) -> list[str]:
    """抽出本轮实际发生的工具名（含失败调用，失败也要能被断言到）。

    :param result: :class:`AgentTurnResult`。
    :returns: 按调用顺序排列的工具名列表。
    """
    return [str(call.get("name")) for call in result.tool_calls]


def _db_rows(db, table: str, where: dict[str, Any]) -> list[dict[str, Any]]:
    """按等值条件查若干行（仅用于断言，条件来自用例 YAML）。

    :param db: 业务库 ``Database``。
    :param table: 目标表名。
    :param where: 等值条件字典；为空表示不过滤。
    :returns: 命中的行（dict 列表）。
    """
    clauses = " AND ".join(f'"{key}" = ?' for key in where)
    sql = f'SELECT * FROM "{table}"'
    if clauses:
        sql += f" WHERE {clauses}"
    with db.transaction() as conn:
        return [dict(row) for row in conn.execute(sql, tuple(where.values())).fetchall()]


def _check_db(case: EvalCase, db) -> AssertionResult:
    """校验数据库终态（写操作是否真的落库、状态是否被正确改写）。

    :param case: 用例定义，读 ``expect_db``。
    :param db: 业务库 ``Database``。
    :returns: 名为 ``db`` 的断言结果；未找到记录或字段不符都记入 ``detail``。
    """
    problems: list[str] = []
    for item in case.expect_db:
        table = str(item.get("table") or "")
        where = dict(item.get("where") or {})
        expected = dict(item.get("expect") or {})
        rows = _db_rows(db, table, where)
        if not rows:
            problems.append(f"{table} 未找到满足 {where} 的记录")
            continue
        row = rows[0]
        for key, value in expected.items():
            if row.get(key) != value:
                problems.append(f"{table}.{key}={row.get(key)!r}，期望 {value!r}")
    return AssertionResult(name="db", passed=not problems, detail="；".join(problems))


def _check_trace(case: EvalCase, events: list[dict[str, Any]]) -> AssertionResult:
    """校验 Trace 里出现过指定事件（子集匹配：只比对用例声明的键）。

    :param case: 用例定义，读 ``expect_trace``。
    :param events: 本轮写入的 trace 事件列表。
    :returns: 名为 ``trace`` 的断言结果。
    """
    problems: list[str] = []
    for expectation in case.expect_trace:
        matched = any(
            all(event.get(key) == value for key, value in expectation.items())
            for event in events
        )
        if not matched:
            problems.append(f"trace 缺少事件 {expectation}")
    return AssertionResult(name="trace", passed=not problems, detail="；".join(problems))


def _check_policy(case: EvalCase, result) -> AssertionResult:
    """校验本轮判定命中过指定 ``rule_id``（策略是否真的生效，而不是「恰好没出事」）。

    :param case: 用例定义，读 ``expect_policy``。
    :param result: :class:`AgentTurnResult`，读 ``policy``。
    :returns: 名为 ``policy`` 的断言结果。
    """
    seen = {str(item.get("rule_id")) for item in result.policy}
    missing = [rule for rule in case.expect_policy if rule not in seen]
    return AssertionResult(
        name="policy", passed=not missing, detail=f"缺少规则判定：{missing}" if missing else ""
    )


def check_case(
    case: EvalCase,
    result,
    *,
    events: list[dict[str, Any]],
    db,
) -> list[AssertionResult]:
    """执行一条用例的全部断言，返回逐项结果（调用方负责汇总）。

    断言分四层：工具调用 → 回答文本 → 数据库终态 → 策略与 Trace，
    另外按 ``expect_citations`` 追加引用检查。

    :param case: 用例定义。
    :param result: 该轮的 :class:`AgentTurnResult`。
    :param events: 本轮 trace 事件。
    :param db: 业务库 ``Database``。
    :returns: 断言结果列表；空列表表示没有可查的项（不会自动判通过，由 runner 处理）。
    """
    checks: list[AssertionResult] = []
    names = _tool_names(result)

    if case.expect_tools:
        hit = [name for name in case.expect_tools if name in names]
        checks.append(
            AssertionResult(
                name="tools_called",
                passed=bool(hit),
                detail="" if hit else f"未调用 {case.expect_tools}，实际 {names}",
            )
        )

    if case.forbid_tools:
        hit = [name for name in case.forbid_tools if name in names]
        checks.append(
            AssertionResult(
                name="tools_forbidden",
                passed=not hit,
                detail=f"不应调用却调用了：{hit}" if hit else "",
            )
        )

    if case.expect_status:
        checks.append(
            AssertionResult(
                name="status",
                passed=result.status == case.expect_status,
                detail="" if result.status == case.expect_status else f"实际 {result.status}",
            )
        )

    compact = _norm(result.final_text)
    missing = [token for token in case.must_include if _norm(token) not in compact]
    if case.must_include_any and not any(_norm(token) in compact for token in case.must_include_any):
        missing.append(f"任一{case.must_include_any}")
    violated = [token for token in case.must_not_include if _norm(token) in compact]
    if case.must_include or case.must_include_any or case.must_not_include:
        checks.append(
            AssertionResult(
                name="answer_text",
                passed=not missing and not violated,
                detail=f"缺少 {missing}；不应出现 {violated}" if (missing or violated) else "",
            )
        )

    if case.expect_db:
        checks.append(_check_db(case, db))

    if case.expect_policy:
        checks.append(_check_policy(case, result))

    if case.expect_trace:
        checks.append(_check_trace(case, events))

    if case.expect_citations:
        failures = validate_citations(
            result.final_text, result.tool_calls, case.expect_citations
        )
        checks.append(
            AssertionResult(
                name="citations", passed=not failures, detail="；".join(failures)
            )
        )

    return checks
