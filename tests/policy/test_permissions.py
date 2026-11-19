"""S3：调用方权限有一条真实发放路径（环境变量 / CLI / make_runner）。"""

from __future__ import annotations

from pathlib import Path

from zkagent.config import Container, load_settings


def test_permissions_parse_from_env(monkeypatch) -> None:
    """验证：``ZKAGENT_PERMISSIONS`` 的逗号分隔字符串被解析成列表（不是只支持 JSON 数组）。

    环境变量只能传字符串，若只认 JSON，运维配置就得写 ``'["kb:internal"]'``，极易写错。
    """
    monkeypatch.setenv("ZKAGENT_PERMISSIONS", "kb:internal,refund:approve")
    settings = load_settings()
    assert settings.permissions == ["kb:internal", "refund:approve"]


def test_make_runner_injects_permissions(tmp_path: Path) -> None:
    """验证：配置里的 ``permissions`` 被注入到工具上下文的 ``ctx.permissions``。

    权限必须在**会话构建时**落到 ctx，规则与知识工具才有东西可判；模型无法影响它。
    """
    settings = load_settings(
        industry_id="appliance",
        db_path=str(tmp_path / "business.db"),
        checkpoints_db_path=str(tmp_path / "checkpoints.db"),
        permissions=["kb:internal"],
    )
    container = Container.build(settings)
    script = [
        {"tool_calls": [{"name": "get_order", "args": {"order_id": "A1001"}}]},
        {"content_from_tool": "订单查询完成。"},
    ]

    _runner, ctx = container.make_runner(
        user_id="U1", session_id="s1", provider="fake", task="查订单 A1001", script=script
    )

    assert ctx.permissions == ["kb:internal"]


def test_make_runner_accepts_explicit_permissions(tmp_path: Path) -> None:
    """验证：``make_runner(permissions=...)`` 显式传参可覆盖配置默认值（供 CLI/eval 按需提权）。"""
    settings = load_settings(
        industry_id="appliance",
        db_path=str(tmp_path / "business.db"),
        checkpoints_db_path=str(tmp_path / "checkpoints.db"),
    )
    container = Container.build(settings)

    _runner, ctx = container.make_runner(
        user_id="U1", session_id="s2", provider="fake", task="查订单 A1001",
        script=[{"content": "ok"}], permissions=["refund:approve"],
    )

    assert ctx.permissions == ["refund:approve"]
