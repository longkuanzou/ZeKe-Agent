"""规则模型与判定引擎：白名单算子、优先级、冲突裁决、加载期校验。"""

from __future__ import annotations

import pytest

from zkagent.core.policy import PolicyFacts, RulePolicyEngine, RuleValidationError, load_rule_set


def _engine(tmp_path, rules: str, *, known_tools: set[str] | None = None) -> RulePolicyEngine:
    path = tmp_path / "rules.yaml"
    path.write_text("version: 1\nrules:\n" + rules, encoding="utf-8")
    return RulePolicyEngine(load_rule_set(path, known_tools=known_tools))


def _facts(**overrides) -> PolicyFacts:
    base = {
        "verified": True,
        "order_status": "delivered",
        "amount_cents": 50000,
        "days_since_delivered": 3,
        "order_user_id": "U1",
    }
    base.update(overrides)
    return PolicyFacts(**base)


def test_default_allow_is_explicit(tmp_path) -> None:
    """验证：未命中任何规则时返回 ``default.allow`` 并**留痕**。

    防「规则没配上」被误认为「规则判定通过」——放行也必须能说清是哪条规则放的。
    """
    engine = _engine(tmp_path, "  []\n")
    decision = engine.evaluate("get_order", {"facts": _facts(), "permissions": [], "user_id": "U1"})
    assert decision.allowed is True
    assert decision.decision == "allow"
    assert decision.rule_id == "default.allow", "没命中规则必须留痕，不能伪装成规则通过"


def test_priority_then_effect_strength(tmp_path) -> None:
    """验证：``priority`` 数值小者优先——同为 ``create_refund_ticket``，
    priority 10 的 allow 压过 priority 20 的 deny。

    排序键是 ``(priority, EFFECTS.index(effect))``，本用例只覆盖第一维。
    """
    rules = """
  - id: low.deny
    priority: 20
    match: {action: [create_refund_ticket]}
    effect: deny
  - id: high.allow
    priority: 10
    match: {action: [create_refund_ticket]}
    effect: allow
"""
    engine = _engine(tmp_path, rules)
    assert engine.evaluate("create_refund_ticket", {"facts": _facts()}).rule_id == "high.allow"


def test_same_priority_deny_wins(tmp_path) -> None:
    """验证：同优先级时按 effect 强度裁决，``deny`` 压过 ``allow``。

    覆盖排序键的第二维（``EFFECTS`` 顺序：deny > escalate > require_confirmation > allow）。
    与上一条合起来才完整。
    """
    rules = """
  - id: same.allow
    priority: 10
    match: {action: [cancel_order]}
    effect: allow
  - id: same.deny
    priority: 10
    match: {action: [cancel_order]}
    effect: deny
"""
    engine = _engine(tmp_path, rules)
    decision = engine.evaluate("cancel_order", {"facts": _facts()})
    assert decision.decision == "deny" and decision.rule_id == "same.deny"


def test_permission_is_checked_before_effect(tmp_path) -> None:
    """验证：权限检查**先于** effect——缺权限直接 deny（rule_id 带 ``:permission``），
    有权限才落到 ``escalate`` + ``requires_hitl``。

    同一条规则两种结局：没有 ``refund:approve`` 的人连「转人工」的资格都没有。
    """
    rules = """
  - id: refund.high_value
    match: {action: [create_refund_ticket], amount_cents: {gt: 100000}}
    effect: escalate
    required_permissions: [refund:approve]
"""
    engine = _engine(tmp_path, rules)
    decision = engine.evaluate(
        "create_refund_ticket",
        {"facts": _facts(amount_cents=699900), "permissions": [], "user_id": "U1"},
    )
    assert decision.allowed is False
    assert decision.rule_id == "refund.high_value:permission"
    assert "refund:approve" in decision.reason

    allowed = engine.evaluate(
        "create_refund_ticket",
        {"facts": _facts(amount_cents=699900), "permissions": ["refund:approve"], "user_id": "U1"},
    )
    assert allowed.decision == "escalate" and allowed.requires_hitl is True


def test_verified_precondition_blocks_unverified(tmp_path) -> None:
    """验证：``verified`` 前置条件未满足时 deny 并引导 ``verify_user``（Poka-yoke）。

    注意第二组断言：核验通过后命中的是规则本体（``order.requires_verified``）而不是
    ``:precondition``——前置条件只决定「门禁是否触发」，不改变规则自身的 effect。
    """
    rules = """
  - id: order.requires_verified
    match: {action: [get_order]}
    effect: deny
    preconditions: [{kind: verified}]
    suggested_action: verify_user
"""
    engine = _engine(tmp_path, rules)
    unverified = engine.evaluate("get_order", {"facts": _facts(verified=False), "user_id": "U1"})
    assert unverified.decision == "deny"
    assert unverified.rule_id == "order.requires_verified:precondition"
    assert unverified.suggested_action == "verify_user"

    verified = engine.evaluate("get_order", {"facts": _facts(verified=True), "user_id": "U1"})
    assert verified.rule_id == "order.requires_verified", "门禁通过后不应落到 deny:precondition"
    assert verified.decision == "deny", "该规则本身是 deny，门禁只决定是否触发"


def test_owner_match_precondition(tmp_path) -> None:
    """验证：``owner_match`` 前置条件——非本人订单被拒，本人订单放行。

    防越权查他人订单；``order_user_id`` 缺失也算不满足。
    """
    rules = """
  - id: order.owner
    match: {action: [get_order]}
    effect: allow
    preconditions: [{kind: owner_match}]
"""
    engine = _engine(tmp_path, rules)
    denied = engine.evaluate(
        "get_order", {"facts": _facts(order_user_id="U2"), "user_id": "U1"}
    )
    assert denied.decision == "deny"
    assert denied.rule_id == "order.owner:precondition"

    allowed = engine.evaluate(
        "get_order", {"facts": _facts(order_user_id="U1"), "user_id": "U1"}
    )
    assert allowed.decision == "allow" and allowed.rule_id == "order.owner"


def test_ranges_and_status(tmp_path) -> None:
    """验证：状态枚举 + 数值区间（``lte``/``gt``）组合匹配。

    未发货退款被拒并引导 ``cancel_order``；已签收、7 天内、≤1000 元则自动放行。
    """
    rules = """
  - id: refund.block_unshipped
    match: {action: [create_refund_ticket], order_status: [created, paid]}
    effect: deny
    suggested_action: cancel_order
  - id: refund.auto
    match:
      action: [create_refund_ticket]
      order_status: [delivered]
      days_since_delivered: {lte: 7}
      amount_cents: {lte: 100000}
    effect: allow
"""
    engine = _engine(tmp_path, rules)
    blocked = engine.evaluate(
        "create_refund_ticket", {"facts": _facts(order_status="paid", days_since_delivered=None)}
    )
    assert blocked.decision == "deny" and blocked.suggested_action == "cancel_order"

    auto = engine.evaluate(
        "create_refund_ticket", {"facts": _facts(order_status="delivered", days_since_delivered=7)}
    )
    assert auto.decision == "allow" and auto.rule_id == "refund.auto"


def test_prior_tool_missing_and_any_of(tmp_path) -> None:
    """验证：``prior_tool_missing`` 要求先查过订单（防凭空退款），
    ``any_of`` 任一子条件满足即命中 escalate。
    """
    rules = """
  - id: refund.requires_lookup
    match:
      action: [create_refund_ticket]
      prior_tool_missing: get_order
    effect: deny
    suggested_action: get_order
  - id: refund.high_or_overdue
    match:
      action: [create_refund_ticket]
      any_of:
        - amount_cents: {gt: 100000}
        - days_since_delivered: {gt: 7}
    effect: escalate
"""
    engine = _engine(tmp_path, rules)
    missing = engine.evaluate("create_refund_ticket", {"facts": _facts(prior_tools=[])})
    assert missing.decision == "deny" and missing.suggested_action == "get_order"

    looked_up = engine.evaluate(
        "create_refund_ticket", {"facts": _facts(prior_tools=["get_order"], amount_cents=200000)}
    )
    assert looked_up.decision == "escalate" and looked_up.rule_id == "refund.high_or_overdue"


def test_query_keyword_match_is_space_insensitive(tmp_path) -> None:
    """验证：``query_contains_any`` 关键词匹配**忽略空格**——「冒 烟」也能命中「冒烟」。

    防用户书写差异（空格、断词）绕过安全红线。
    """
    rules = """
  - id: safety.stop
    match:
      action: [knowledge_search]
      query_contains_any: [冒烟, 漏电]
    effect: escalate
"""
    engine = _engine(tmp_path, rules)
    hit = engine.evaluate(
        "knowledge_search", {"facts": _facts(query_text="空调 冒 烟 了怎么办")}
    )
    assert hit.decision == "escalate"
    miss = engine.evaluate("knowledge_search", {"facts": _facts(query_text="空调滤网怎么洗")})
    assert miss.rule_id == "default.allow"


@pytest.mark.parametrize(
    "rules, message",
    [
        ("  - id: bad\n    match: {action: [no_such_tool]}\n    effect: deny\n", "不是已注册工具"),
        (
            "  - id: bad\n    match: {action: [get_order], order_status: [unknown]}\n"
            "    effect: deny\n",
            "未知订单状态",
        ),
        (
            "  - id: bad\n    match: {action: [get_order], amount_cents: {}}\n    effect: deny\n",
            "未给出 lte/gt",
        ),
        (
            "  - id: bad\n    match: {action: [get_order]}\n    effect: deny\n"
            "    required_permissions: [refund]\n",
            "domain:action",
        ),
        (
            "  - id: dup\n    match: {action: [get_order]}\n    effect: deny\n"
            "  - id: dup\n    match: {action: [list_orders]}\n    effect: allow\n",
            "重复",
        ),
    ],
)
def test_invalid_rules_fail_at_load(tmp_path, rules: str, message: str) -> None:
    """验证：非法规则在**加载期**就失败，共五种：工具未注册、未知订单状态、
    区间未给 ``lte``/``gt``、权限格式非 ``domain:action``、规则 id 重复。

    规则写错最坏的结局不是报错，而是**永远不命中**（等于没有规则），
    所以必须在启动时炸掉，而不是留到运行期静默失效。
    """
    with pytest.raises(RuleValidationError, match=message):
        _engine(tmp_path, rules, known_tools={"get_order", "list_orders"})


def test_unknown_field_fails_at_load(tmp_path) -> None:
    """验证：字段名拼错（``order_statuz``）在加载期报错，不被当成「永不命中」放过。

    与上一条同属加载期校验，只是走的是模型严格校验（extra=forbid）分支。
    """
    rules = """
  - id: typo
    match: {action: [get_order], order_statuz: [paid]}
    effect: deny
"""
    with pytest.raises(RuleValidationError, match="规则结构非法"):
        _engine(tmp_path, rules, known_tools={"get_order"})


def test_empty_rules_file_is_valid(tmp_path) -> None:
    """验证：只有 ``version`` 没有 ``rules`` 的空规则文件合法，判定落到 ``default.allow``。

    行业包可以只配知识、不配策略，此时必须放行而不是崩溃。
    """
    path = tmp_path / "rules.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    engine = RulePolicyEngine(load_rule_set(path))
    assert engine.evaluate("anything", {"facts": _facts()}).rule_id == "default.allow"
