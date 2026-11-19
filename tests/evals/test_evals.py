"""P6 评估体系：加载校验、引用断言、Runner 端到端与报告。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from zkagent.backends.mock.db import Database
from zkagent.backends.mock.seed import seed_database
from zkagent.config import (
    Container,
    default_industry_packs_dir,
    load_industry_pack,
    load_settings,
    pack_file,
)
from zkagent.core.evals.citations import validate_citations
from zkagent.core.evals.loader import EvalCaseError, load_cases, validate_cases
from zkagent.core.evals.models import CATEGORIES, EvalCase
from zkagent.core.evals.report import render_markdown
from zkagent.core.evals.runner import EvalRunner, run_context_compression
from zkagent.core.observability.cost import estimate_usage, summarize

PACK_ROOT = default_industry_packs_dir()
APPLIANCE = PACK_ROOT / "appliance"
HOMEAPP = PACK_ROOT / "homeapp"


@pytest.mark.skipif(not APPLIANCE.is_dir(), reason="缺少 appliance 包")
def test_appliance_cases_cover_five_categories() -> None:
    """验证：appliance 评测集达到最低条数，并覆盖五类（正常 / 边界 / 失败 / 越权 / 对抗）。"""
    cases = load_cases(APPLIANCE / "evals" / "cases.yaml")
    validate_cases(cases, minimum=20)
    assert {case.category for case in cases} == set(CATEGORIES)


@pytest.mark.skipif(not HOMEAPP.is_dir(), reason="缺少 homeapp 包")
def test_homeapp_cases_cover_five_categories() -> None:
    """验证：homeapp 评测集同样满足条数与分类覆盖——新包不能只跑通、不建用例。"""
    cases = load_cases(HOMEAPP / "evals" / "cases.yaml")
    validate_cases(cases, minimum=20)
    assert {case.category for case in cases} == set(CATEGORIES)


def test_duplicate_ids_fail_loudly(tmp_path: Path) -> None:
    """验证：用例 id 重复在加载期报错，否则报告里两条结果会互相覆盖。"""
    path = tmp_path / "cases.yaml"
    path.write_text(
        "version: 1\ncases:\n"
        "  - {id: a, category: normal, task: t}\n"
        "  - {id: a, category: boundary, task: t}\n",
        encoding="utf-8",
    )
    with pytest.raises(EvalCaseError, match="重复"):
        load_cases(path)


def test_citation_subset_and_none() -> None:
    """验证：``subset`` 模式要求引用落在本轮片段内；``none`` 模式禁止任何引用。"""
    calls = [
        {
            "name": "knowledge_search", "ok": True, 
            "chunk_count": 2, "sources": ["a.md", "b.md"]
        }
    ]
    assert validate_citations("依据[1]与[2]", calls, "subset") == []
    assert validate_citations("依据[3]", calls, "subset")
    assert validate_citations("来源：c.md", calls, "subset")
    assert validate_citations("没有引用", calls, "none") == []
    assert validate_citations("依据[1]", calls, "none")


def _fake_result(**overrides):
    base = {
        "final_text": "订单查询完成。",
        "status": "completed",
        "tool_calls": [{"name": "get_order", "ok": True}],
        "policy": [{"tool": "get_order", "decision": "allow", "rule_id": "default.allow"}],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_assertions_cover_tool_text_policy_and_trace() -> None:
    """验证：四层断言（工具 / 文本 / 策略 / trace）在一次调用里全部给出且通过。"""
    from zkagent.core.evals.assertions import check_case

    case = EvalCase(
        id="x",
        category="normal",
        task="t",
        expect_tools=["get_order"],
        must_include=["完成"],
        expect_policy=["default.allow"],
        expect_trace=[{"event": "tool_call_end", "tool": "get_order", "ok": True}],
    )
    checks = check_case(
        case,
        _fake_result(),
        events=[{"event": "tool_call_end", "tool": "get_order", "ok": True}],
        db=None,
    )
    assert all(check.passed for check in checks)


def test_db_assertion_reads_final_state(tmp_path: Path) -> None:
    """验证：DB 断言读的是**终态**（订单是否 delivered），而不是回答文本。"""
    from zkagent.core.evals.assertions import check_case

    db = Database(tmp_path / "business.db")
    db.reset()
    config = load_industry_pack(APPLIANCE)
    seed_database(db, pack_file(APPLIANCE, config.seed_data))

    case = EvalCase(
        id="db",
        category="normal",
        task="t",
        expect_db=[
            {
                "table": "orders",
                "where": {"order_id": "A1001"},
                "expect": {"status": "delivered"},
            }
        ],
    )
    checks = check_case(case, _fake_result(), events=[], db=db)
    assert checks[0].passed is True


@pytest.mark.skipif(not APPLIANCE.is_dir(), reason="缺少 appliance 包")
def test_runner_end_to_end(tmp_path: Path) -> None:
    """验证：评估器端到端跑一条用例，Pass@1 记录与 ``trace_id`` 都落到结果里。"""
    db_path = tmp_path / "business.db"
    db = Database(db_path)
    db.reset()
    config = load_industry_pack(APPLIANCE)
    seed_database(db, pack_file(APPLIANCE, config.seed_data))

    settings = load_settings(
        industry_id="appliance",
        db_path=str(db_path),
        checkpoints_db_path=str(tmp_path / "checkpoints.db"),
    )
    container = Container.build(settings)
    case = EvalCase(
        id="e2e",
        category="normal",
        task="查订单 A1001",
        script=[
            {"tool_calls": [{"name": "get_order", "args": {"order_id": "A1001"}}]},
            {"content_from_tool": "订单查询完成。"},
        ],
        expect_tools=["get_order"],
        expect_status="completed",
    )
    result = EvalRunner(container, provider="fake").run_case(case)

    assert result.passed is True
    assert result.attempts == [True]
    assert result.trace_id


def test_report_and_cost() -> None:
    """验证：报告含 Pass@1 与失败归因；缺 usage 时按字符估算并标记 ``estimated``。"""
    result = SimpleNamespace(
        case_id="a",
        category="normal",
        passed=False,
        attempts=[False],
        assertions=[],
        failures=["text_mismatch"],
        final_text="答案",
        trace_id="t1",
        usage={},
        latency_ms=12,
    )
    usage = summarize([result], model_id="deepseek-chat")
    markdown = render_markdown(
        [result],
        usage=usage,
        industry="appliance",
        provider="fake",
        model_id="fake",
        k=1,
    )
    assert "Pass@1" in markdown and "text_mismatch" in markdown
    assert usage.estimated is True
    assert estimate_usage("你好")["total_tokens"] > 0


def test_context_compression_report() -> None:
    """验证：压缩对照数据（前后字符、保持率）可生成，且关键信息未丢。"""
    data = run_context_compression(turns=25)
    assert data["turns"] == 25
    assert data["chars_after"] < data["chars_before"]
    assert data["retention_passed"] is True
