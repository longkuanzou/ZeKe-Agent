"""纯函数规则引擎：``facts + rules → PolicyDecision``。

三条纪律：

1. **判定不依赖模型**——同一 facts 必得同一结论，可穷举单测；
2. **无 I/O**——事实由 :class:`~zkagent.core.policy.facts.FactsProvider` 预先取好；
3. **默认放行要留痕**——没命中任何规则时返回 ``default.allow``，并在 reason 里写明，
   避免「规则没配上」被误认为「规则判定通过」。
"""

from __future__ import annotations

from typing import Any

from zkagent.core.interfaces import PolicyEngine
from zkagent.core.policy.errors import PolicyError
from zkagent.core.policy.facts import PolicyFacts
from zkagent.core.policy.rules import Precondition, Rule, RuleSet
from zkagent.core.types import PolicyDecision

__all__ = ["RulePolicyEngine"]


class RulePolicyEngine(PolicyEngine):
    """按 ``policies/rules.yaml`` 判定的引擎。"""

    def __init__(self, rule_set: RuleSet) -> None:
        self._rules = rule_set

    @property
    def rule_set(self) -> RuleSet:
        """当前加载的规则全集（只读，供诊断与 CLI 打印）。

        :returns: 构造引擎时传入的 :class:`RuleSet`。
        """
        return self._rules

    @property
    def settings(self):
        """审批设置（four_eyes / ttl），供 HITL 层复用同一份配置。"""
        return self._rules.settings

    def evaluate(self, action: str, context: dict[str, Any]) -> PolicyDecision:
        """对一次工具调用给出判定。

        ``context`` 约定：

        - ``facts``: :class:`PolicyFacts` 或其 dict；
        - ``permissions``: 调用方权限（**服务端投影**，不取自模型）；
        - ``user_id``: 当前用户（``owner_match`` 前置条件用）。
        """
        if not action:
            raise PolicyError("策略判定缺少 action")

        facts_raw = context.get("facts")
        try:
            facts = (
                facts_raw
                if isinstance(facts_raw, PolicyFacts)
                else PolicyFacts.model_validate(facts_raw or {})
            )
        except Exception as exc:  # pydantic 的 ValidationError 不应穿透到编排层
            raise PolicyError(f"策略事实非法：{exc}") from exc

        permissions = {str(item) for item in (context.get("permissions") or [])}
        user_id = context.get("user_id")

        for rule in self._rules.by_priority():
            if not rule.match.matches(action, facts):
                continue

            missing = [item for item in rule.required_permissions if item not in permissions]
            if missing:
                return self._deny(
                    action,
                    rule,
                    reason=f"缺少权限：{', '.join(missing)}",
                    rule_id=f"{rule.id}:permission",
                )

            failed = next(
                (item for item in rule.preconditions if not _precondition_ok(item, facts, user_id)),
                None,
            )
            if failed is not None:
                return self._deny(
                    action,
                    rule,
                    reason=f"前置条件未满足：{failed.kind}",
                    rule_id=f"{rule.id}:precondition",
                )

            return self._decide(action, rule)

        return PolicyDecision(
            action=action,
            allowed=True,
            decision="allow",
            reason="未命中任何规则（default.allow）",
            rule_id="default.allow",
        )

    # ------------------------------------------------------------------ 组装结论

    def _decide(self, action: str, rule: Rule) -> PolicyDecision:
        """按规则的 ``effect`` 组装结论（权限与前置条件此时都已通过）。

        :param action: 工具名。
        :param rule: 命中的规则。
        :returns: ``allow`` 直接放行；``deny`` 走 :meth:`_deny`；其余两种 effect
            置 ``requires_hitl=True``，由编排层挂起等人工。
        """
        reason = rule.description or rule.id
        if rule.effect == "allow":
            return PolicyDecision(
                action=action,
                allowed=True,
                decision="allow",
                reason=reason,
                rule_id=rule.id,
                required_permissions=list(rule.required_permissions),
            )
        if rule.effect == "deny":
            return self._deny(action, rule, reason=reason)
        # require_confirmation / escalate 都需要人工介入
        return PolicyDecision(
            action=action,
            allowed=False,
            decision=rule.effect,
            reason=reason,
            rule_id=rule.id,
            required_permissions=list(rule.required_permissions),
            requires_hitl=True,
            suggested_action=rule.suggested_action,
        )

    @staticmethod
    def _deny(
        action: str,
        rule: Rule,
        *,
        reason: str,
        rule_id: str | None = None,
    ) -> PolicyDecision:
        """组装 ``deny`` 结论（不可执行，且没有审批后路）。

        :param action: 工具名。
        :param rule: 命中的规则，用于带出 ``suggested_action`` 与所需权限。
        :param reason: 拒绝理由，会原样回给模型。
        :param rule_id: 覆盖 ``rule_id``，用于 ``:permission`` / ``:precondition``
            这类「由某条规则派生出的拒绝」。
        :returns: ``allowed=False``、``requires_hitl=False`` 的判定。
        """
        return PolicyDecision(
            action=action,
            allowed=False,
            decision="deny",
            reason=reason,
            rule_id=rule_id or rule.id,
            required_permissions=list(rule.required_permissions),
            suggested_action=rule.suggested_action,
        )


def _precondition_ok(
    precondition: Precondition,
    facts: PolicyFacts,
    user_id: str | None,
) -> bool:
    """前置条件求值：**缺失即不满足**。"""
    if precondition.kind == "verified":
        return facts.verified
    if precondition.kind == "owner_match":
        return bool(facts.order_user_id) and facts.order_user_id == user_id
    if precondition.kind == "status_in":
        return facts.order_status in set(precondition.values)
    if precondition.kind == "amount_lte":
        return (
            facts.amount_cents is not None
            and precondition.amount_cents is not None
            and facts.amount_cents <= precondition.amount_cents
        )
    raise PolicyError(f"未知前置条件：{precondition.kind}")
