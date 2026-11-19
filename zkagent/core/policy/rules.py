"""声明式规则模型与加载器。

**不用表达式求值**（禁止 ``eval``）：所有条件都由白名单算子表达，加载期严格校验。
规则写错（未知字段、未知工具、未知算子）时**启动即失败**——静默放行比崩溃危险。

规则文件位于行业包 ``policies/rules.yaml``，字段见 ``docs/DESIGN.md`` §7.7；
这里只描述「长什么样」，判定逻辑在 :mod:`zkagent.core.policy.engine`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from zkagent.core.policy.errors import RuleValidationError
from zkagent.core.policy.facts import PolicyFacts

__all__ = [
    "EFFECTS",
    "Range",
    "Precondition",
    "Match",
    "Rule",
    "PolicySettings",
    "RuleSet",
    "load_rule_set",
]

#: 规则结论；顺序即同优先级下的冲突裁决顺序（deny 最强）。
EFFECTS: tuple[str, ...] = ("deny", "escalate", "require_confirmation", "allow")

Effect = Literal["allow", "deny", "require_confirmation", "escalate"]
PreconditionKind = Literal["verified", "owner_match", "status_in", "amount_lte"]


class Range(BaseModel):
    """整数区间条件；两个边界都可省略，同时省略视为非法（加载期拒绝）。"""

    model_config = ConfigDict(extra="forbid")

    lte: int | None = None
    gt: int | None = None

    def matches(self, value: int | None) -> bool:
        """判断整数是否落在 ``(gt, lte]`` 区间内。

        :param value: 待判断的整数（如金额、签收天数）；``None`` 一律不命中。
        :returns: 满足下界（严格大于）与上界（小于等于）时返回 ``True``。
        """
        if value is None or (self.lte is None and self.gt is None):
            return False
        if self.lte is not None and value > self.lte:
            return False
        if self.gt is not None and value <= self.gt:
            return False
        return True


class Match(BaseModel):
    """匹配条件：列出的条件**同时成立**才算命中（``any_of`` 内部是析取）。"""

    model_config = ConfigDict(extra="forbid")

    action: list[str] | None = None
    order_status: list[str] | None = None
    amount_cents: Range | None = None
    days_since_delivered: Range | None = None
    category: list[str] | None = None
    appliance: list[str] | None = None
    verified: bool | None = Field(
        default=None, description="按核验状态匹配；身份**门禁**请用 preconditions.verified"
    )
    prior_tool_present: str | list[str] | None = Field(
        default=None, description="本轮已成功调用过其中任一工具才命中"
    )
    prior_tool_missing: str | list[str] | None = Field(
        default=None, description="本轮**尚未**调用其中任一工具才命中（用于「先查单再退款」）"
    )
    query_contains_any: list[str] | None = Field(
        default=None, description="用户原话包含任一关键词才命中（安全红线 / 越界品类）"
    )
    ticket_status: list[str] | None = Field(
        default=None, description="工单状态（created/scheduled/completed/cancelled）"
    )
    ticket_event_present: str | None = Field(
        default=None, description="工单事件流水里出现该类型（如 part_wait 备件等待）"
    )
    any_of: list["Match"] | None = Field(default=None, description="内部任一子条件命中即可")

    def matches(self, action: str, facts: PolicyFacts) -> bool:
        """判断本次工具调用是否命中本条件（各条件**合取**，``any_of`` 内部析取）。

        :param action: 待判定的工具名。
        :param facts: 事前从后端取好的事实（无 I/O，纯函数）。
        :returns: 全部已声明条件都成立返回 ``True``；任一不成立立即 ``False``。
            事实缺失视为不满足（例如区间条件遇到 ``None``）。
        """
        if self.action is not None and action not in self.action:
            return False
        if self.order_status is not None and facts.order_status not in self.order_status:
            return False
        if self.amount_cents is not None and not self.amount_cents.matches(facts.amount_cents):
            return False
        if self.days_since_delivered is not None and not self.days_since_delivered.matches(
            facts.days_since_delivered
        ):
            return False
        if self.category is not None and facts.category not in self.category:
            return False
        if self.appliance is not None and facts.appliance not in self.appliance:
            return False
        if self.verified is not None and facts.verified != self.verified:
            return False
        if self.prior_tool_present is not None:
            required = _as_list(self.prior_tool_present)
            if not set(required) & set(facts.prior_tools):
                return False
        if self.prior_tool_missing is not None:
            required = _as_list(self.prior_tool_missing)
            if set(required) & set(facts.prior_tools):
                return False
        if self.query_contains_any is not None:
            text = facts.text()
            if not any(_normalize(keyword) in text for keyword in self.query_contains_any):
                return False
        if self.ticket_status is not None and facts.ticket_status not in self.ticket_status:
            return False
        if self.ticket_event_present is not None and (
            self.ticket_event_present not in facts.ticket_events
        ):
            return False
        if self.any_of is not None and not any(item.matches(action, facts) for item in self.any_of):
            return False
        return True


class Precondition(BaseModel):
    """前置校验：不满足即 ``deny``，与规则 ``effect`` 无关（门禁语义）。"""

    model_config = ConfigDict(extra="forbid")

    kind: PreconditionKind
    values: list[str] = Field(default_factory=list)
    amount_cents: int | None = None


class Rule(BaseModel):
    """一条规则。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = ""
    priority: int = Field(default=100, ge=0, description="数值小者优先")
    enabled: bool = True
    match: Match
    effect: Effect
    preconditions: list[Precondition] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)
    suggested_action: str | None = None


class PolicySettings(BaseModel):
    """审批相关设置（行业包可覆盖）。"""

    model_config = ConfigDict(extra="forbid")

    four_eyes: bool = Field(default=True, description="审批人不得与申请人同人")
    approval_ttl_hours: int = Field(default=24, gt=0, le=168)


class RuleSet(BaseModel):
    """一个行业包的规则全集。"""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    settings: PolicySettings = Field(default_factory=PolicySettings)
    rules: list[Rule] = Field(default_factory=list)

    def by_priority(self) -> list[Rule]:
        """按「优先级升序 + effect 强度」排序，供引擎取第一条命中。"""
        return sorted(
            (rule for rule in self.rules if rule.enabled),
            key=lambda rule: (rule.priority, EFFECTS.index(rule.effect)),
        )


#: 规则里允许出现的订单状态（与业务库约束一致）。
_ORDER_STATUSES = {"created", "paid", "shipped", "delivered", "cancelled"}


def _as_list(value: str | list[str]) -> list[str]:
    """把「单个字符串或字符串列表」统一成列表。

    :param value: 规则 YAML 里允许写成单值或列表的字段。
    :returns: 字符串列表（单值时包成单元素列表）。
    """
    return [value] if isinstance(value, str) else list(value)


def _normalize(text: str) -> str:
    """归一化文本用于关键词匹配：去掉所有空白并转小写。

    这样「冒 烟」「冒烟」「冒烟 」都能命中同一条安全红线。

    :param text: 原始文本（用户原话或关键词）。
    :returns: 去空白、小写化后的字符串。
    """
    return "".join(str(text).split()).lower()


def _validate(rule_set: RuleSet, known_tools: set[str] | None) -> None:
    """加载期校验，把「永远不命中」的错误挡在启动时。

    检查项：规则 id 重复、``action`` 不是已注册工具、未知订单状态、区间未给边界、
    空关键词、``any_of`` 为空、前置条件参数缺失、权限未写成 ``domain:action``。

    :param rule_set: 已通过结构校验的规则全集。
    :param known_tools: 已注册工具名集合；为 ``None`` 时跳过工具名校验。
    :raises RuleValidationError: 存在任一问题，消息里最多列出前 15 条。
    """
    seen: set[str] = set()
    problems: list[str] = []

    for rule in rule_set.rules:
        if rule.id in seen:
            problems.append(f"规则 id 重复：{rule.id}")
        seen.add(rule.id)

        for action in rule.match.action or []:
            if known_tools is not None and action not in known_tools:
                problems.append(f"{rule.id}: action={action!r} 不是已注册工具")

        for status in rule.match.order_status or []:
            if status not in _ORDER_STATUSES:
                problems.append(f"{rule.id}: 未知订单状态 {status!r}")

        for field in ("amount_cents", "days_since_delivered"):
            value = getattr(rule.match, field)
            if value is not None and value.lte is None and value.gt is None:
                problems.append(f"{rule.id}: {field} 未给出 lte/gt 任一边界")

        for keyword in rule.match.query_contains_any or []:
            if not str(keyword).strip():
                problems.append(f"{rule.id}: query_contains_any 含空关键词")

        if rule.match.any_of is not None and not rule.match.any_of:
            problems.append(f"{rule.id}: any_of 为空")

        for precondition in rule.preconditions:
            if precondition.kind == "status_in" and not precondition.values:
                problems.append(f"{rule.id}: status_in 缺少 values")
            if precondition.kind == "status_in" and set(precondition.values) - _ORDER_STATUSES:
                problems.append(f"{rule.id}: status_in 含未知状态")
            if precondition.kind == "amount_lte" and precondition.amount_cents is None:
                problems.append(f"{rule.id}: amount_lte 缺少 amount_cents")

        for permission in rule.required_permissions:
            if not permission or ":" not in permission:
                problems.append(
                    f"{rule.id}: required_permissions 需形如 domain:action（{permission!r}）"
                )

    if problems:
        preview = "\n  - ".join(problems[:15])
        raise RuleValidationError(f"规则文件有 {len(problems)} 处问题：\n  - {preview}")


def load_rule_set(
    path: Path | str,
    *,
    known_tools: set[str] | None = None,
) -> RuleSet:
    """加载并校验规则文件。

    :param known_tools: 已注册工具名；给出时校验每条 ``action``，防止「规则写了个不存在的工具，
        于是永远不生效」。
    """
    rule_path = Path(path)
    if not rule_path.is_file():
        raise RuleValidationError(f"规则文件不存在：{rule_path}")
    try:
        raw: Any = yaml.safe_load(rule_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RuleValidationError(f"规则 YAML 解析失败：{rule_path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RuleValidationError(f"规则文件顶层必须是映射：{rule_path}")
    try:
        rule_set = RuleSet.model_validate(raw)
    except ValidationError as exc:
        raise RuleValidationError(f"规则结构非法：{rule_path}: {exc}") from exc
    _validate(rule_set, known_tools)
    return rule_set


Match.model_rebuild()
