"""行业包规则必须能被真实加载：工具名、状态、前置条件全部对得上。

规则写错时最坏的结局不是报错，而是**永远不命中**（等于没有规则）。这里用
「已启用工具清单」做交叉校验，把这种静默失效挡在加载期。
"""

from __future__ import annotations

import pytest

from zkagent.config import (
    default_industry_packs_dir,
    load_industry_pack,
    load_tools_config,
    pack_file,
)
from zkagent.core.policy import RulePolicyEngine, load_rule_set

PACKS = [path for path in default_industry_packs_dir().iterdir() if (path / "pack.yaml").is_file()]


def _known_tools(pack_dir) -> set[str]:
    config = load_industry_pack(pack_dir)
    tools = set(load_tools_config(config, pack_dir).enabled)
    return tools | {"knowledge_search"}


@pytest.mark.parametrize("pack_dir", PACKS, ids=lambda path: path.name)
def test_pack_rules_load(pack_dir) -> None:
    """验证：每个行业包的 ``rules.yaml`` 都能真实加载，且工具名与已启用工具清单对得上。

    规则写错最坏的结局是**永远不命中**，所以这里用「已启用工具清单」做交叉校验，
    把静默失效挡在加载期。规则为空时也要给出显式 ``default.allow`` 而不是崩。
    """
    config = load_industry_pack(pack_dir)
    rule_set = load_rule_set(
        pack_file(pack_dir, config.policies_file), known_tools=_known_tools(pack_dir)
    )
    engine = RulePolicyEngine(rule_set)
    # 规则为空也要能给出显式 default.allow，而不是崩
    decision = engine.evaluate("knowledge_search", {"facts": {}, "permissions": []})
    assert decision.rule_id


def test_homeapp_identity_gate_is_wired() -> None:
    """验证：homeapp 的**身份门禁**确实接上了——未核验查订单被拒并引导 ``verify_user``，
    核验后放行。

    注意校验的是 ``apps/pack_templates`` 的模板而非生成包，避免旧包掩盖模板问题。
    """
    pack_dir = default_industry_packs_dir() / "homeapp"
    if not (pack_dir / "pack.yaml").is_file():
        pytest.skip("尚未生成 homeapp 行业包")
    config = load_industry_pack(pack_dir)
    # 生成包的 policies 由模板复制；以模板为准校验，避免旧包掩盖问题
    template = pack_dir.parents[2] / "apps" / "pack_templates" / "homeapp" / config.policies_file
    rule_set = load_rule_set(template, known_tools=_known_tools(pack_dir))
    engine = RulePolicyEngine(rule_set)

    unverified = engine.evaluate(
        "get_order", {"facts": {"verified": False}, "permissions": [], "user_id": "H1"}
    )
    assert unverified.decision == "deny"
    assert unverified.suggested_action == "verify_user"

    verified = engine.evaluate(
        "get_order", {"facts": {"verified": True}, "permissions": [], "user_id": "H1"}
    )
    assert verified.allowed is True, f"核验后不应再被门禁拦住：{verified}"


def test_homeapp_out_of_scope_blocks_fridge() -> None:
    """验证：超出服务范围的品类被 ``scope.out_of_scope`` 拦住，范围内放行。

    homeapp 只覆盖空调/洗衣机/冰箱之外的范围内品类，问「冰箱保修」要被挡，
    问「洗衣机不排水」要放行——防止用相邻品类的政策张冠李戴。
    """
    pack_dir = default_industry_packs_dir() / "homeapp"
    if not (pack_dir / "pack.yaml").is_file():
        pytest.skip("尚未生成 homeapp 行业包")
    template = (
        pack_dir.parents[2] / "apps" / "pack_templates" / "homeapp" / "policies" / "rules.yaml"
    )
    engine = RulePolicyEngine(load_rule_set(template, known_tools=_known_tools(pack_dir)))

    blocked = engine.evaluate(
        "knowledge_search",
        {"facts": {"query_text": "你们家冰箱保修几年"}, "permissions": []},
    )
    assert blocked.decision == "deny"
    assert blocked.rule_id == "scope.out_of_scope"

    allowed = engine.evaluate(
        "knowledge_search",
        {"facts": {"query_text": "洗衣机不排水怎么办"}, "permissions": []},
    )
    assert allowed.allowed is True
