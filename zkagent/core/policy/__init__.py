"""业务规则与 HITL 审批（P4）。

分层：

| 模块 | 职责 | 是否碰 I/O |
| --- | --- | --- |
| ``rules.py`` | 规则模型 + YAML 加载 + 严格校验 | 只读文件 |
| ``facts.py`` | 判定事实（订单状态/金额/签收天数/核验状态）与提供者协议 | 否 |
| ``engine.py`` | 纯函数判定 | 否 |
| ``approvals.py`` | 审批状态机、参数指纹、一次性消费 | 否（仓储注入） |
"""

from __future__ import annotations

from zkagent.core.policy.engine import RulePolicyEngine
from zkagent.core.policy.errors import (
    APPROVAL_ERROR_CODES,
    ApprovalError,
    PolicyError,
    RuleValidationError,
)
from zkagent.core.policy.facts import FactsProvider, PolicyFacts
from zkagent.core.policy.rules import (
    EFFECTS,
    Match,
    PolicySettings,
    Precondition,
    Range,
    Rule,
    RuleSet,
    load_rule_set,
)

__all__ = [
    "APPROVAL_ERROR_CODES",
    "ApprovalError",
    "EFFECTS",
    "FactsProvider",
    "Match",
    "PolicyError",
    "PolicyFacts",
    "PolicySettings",
    "Precondition",
    "Range",
    "Rule",
    "RulePolicyEngine",
    "RuleSet",
    "RuleValidationError",
    "load_rule_set",
]
