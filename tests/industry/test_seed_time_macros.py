"""M16：种子时间宏在灌库时展开，不再写死生成时刻。"""

from __future__ import annotations

from datetime import datetime, timedelta

from zkagent.backends.mock.seed import _resolve_macro  # noqa: PLC2701 - 直接验证解析器


def test_macro_resolves_relative_to_now() -> None:
    """验证：``@now-2h`` / ``@now+5d`` / ``@now-30m`` 按给定 ``now`` 正确换算。"""
    now = datetime(2026, 10, 26, 12, 0, 0)

    assert _resolve_macro("@now-2h", now=now) == "2026-10-26T10:00:00"
    assert _resolve_macro("@now+5d", now=now) == "2026-10-31T12:00:00"
    assert _resolve_macro("@now-30m", now=now) == "2026-10-26T11:30:00"


def test_non_macro_values_are_untouched() -> None:
    """验证：普通时间字符串与 ``None`` 原样返回，不被误当成宏。"""
    assert _resolve_macro("2026-01-01T00:00:00") == "2026-01-01T00:00:00"
    assert _resolve_macro(None) is None


def test_generated_homeapp_seed_uses_macros() -> None:
    """验证：生成包的种子仍用 ``@now`` 宏，不再写死生成日期（否则数据随时间老化）。"""
    from zkagent.config import default_industry_packs_dir

    path = default_industry_packs_dir() / "homeapp" / "seed_data" / "orders.yaml"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    assert "@now" in text
    assert datetime.now().strftime("%Y-%m-%d") not in text, "不应再写死生成日期"


def test_macro_is_within_expected_window() -> None:
    """验证：``@now-5d`` 展开后与「5 天前」相差在 2 分钟内，防止时区或格式偏差。"""
    resolved = _resolve_macro("@now-5d")
    moment = datetime.strptime(resolved, "%Y-%m-%dT%H:%M:%S")
    assert abs((datetime.now() - moment) - timedelta(days=5)) < timedelta(minutes=2)
