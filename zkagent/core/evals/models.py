"""评估用例与结果的纯数据模型（框架无关）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "CATEGORIES",
    "AssertionResult",
    "CaseResult",
    "EvalCase",
    "UsageSummary",
]

#: 五类用例：正常 / 边界 / 失败 / 越权 / 对抗。
CATEGORIES: tuple[str, ...] = ("normal", "boundary", "failure", "forbidden", "adversarial")

Category = Literal["normal", "boundary", "failure", "forbidden", "adversarial"]


class EvalCase(BaseModel):
    """一条端到端评估用例。"""

    id: str = Field(min_length=1)
    category: Category
    task: str = Field(min_length=1)
    user_id: str = "U1"
    session: str | None = None
    #: 工具注入的意图关键字（仅在会话构建时生效）
    intent: str | None = None
    #: 模拟「已登录且已核验」的会话：真实系统里用户登录即完成核验，
    #: 不该要求模型再问一次手机尾号。只影响 ``verified``，不绕过归属校验。
    verified: bool = False

    #: Fake 模型的确定性脚本（真实模型下忽略）
    script: list[dict[str, Any]] = Field(default_factory=list)

    expect_tools: list[str] = Field(default_factory=list, description="至少调用其中一个")
    forbid_tools: list[str] = Field(default_factory=list, description="不得调用")
    must_include: list[str] = Field(default_factory=list)
    #: 同义表述任一命中即通过（用户可见措辞本就多样：「已开具」/「已经开好」）。
    #: 与 ``must_include`` 并存：后者要求全部命中，本字段要求至少一个。
    must_include_any: list[str] = Field(default_factory=list)
    must_not_include: list[str] = Field(default_factory=list)
    expect_status: str | None = None

    #: 数据库终态断言：[{"table": "...", "where": {...}, "expect": {...}}]
    expect_db: list[dict[str, Any]] = Field(default_factory=list)
    #: 策略规则断言：这些 rule_id 必须出现在本轮判定里
    expect_policy: list[str] = Field(default_factory=list)
    #: Trace 断言：[{"event": "tool_call_end", "tool": "get_order", "ok": true}]
    expect_trace: list[dict[str, Any]] = Field(default_factory=list)
    #: 引用断言：subset（必须落在本轮片段内）/ none（不得出现引用）
    expect_citations: Literal["subset", "none"] | None = None

    manual_review: bool = False


class AssertionResult(BaseModel):
    """单条断言的结果（供报告逐项展示，失败时能说清哪一层没过）。"""

    name: str
    passed: bool
    detail: str = ""


class CaseResult(BaseModel):
    """一条用例的完整运行结果（含逐项断言、Usage 与耗时）。"""

    case_id: str
    category: str
    passed: bool
    attempts: list[bool] = Field(default_factory=list)
    assertions: list[AssertionResult] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    final_text: str = ""
    trace_id: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    latency_ms: int = 0


class UsageSummary(BaseModel):
    """Token / 成本 / 耗时的汇总；``estimated`` 表示数值来自本地估算而非供应商回传。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_cny: float = 0.0
    latency_ms: int = 0
    estimated: bool = False
