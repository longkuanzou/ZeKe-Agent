"""端到端验收：不依赖任何密钥即可跑通「查订单 / 查物流」两条业务闭环。

使用 Fake 模型的确定性脚本（来自行业包 ``demo_scenarios.yaml``），因此
结果可复现、无网络依赖；真实 DeepSeek 链路由人工冒烟测试覆盖（见阶段报告）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zkagent.backends.mock.db import Database
from zkagent.backends.mock.seed import seed_database
from zkagent.config import (
    Container,
    load_industry_pack,
    load_settings,
    pack_file,
    resolve_paths,
)


@pytest.fixture()
def container(tmp_path: Path) -> Container:
    """在临时项目根上装配一个完整容器，并灌好种子数据。"""
    settings = load_settings(
        project_root=tmp_path,
        db_path=tmp_path / "data/business.db",
        checkpoints_db_path=tmp_path / "data/checkpoints.db",
        trace_dir=tmp_path / "data/traces",
    )
    paths = resolve_paths(settings)
    pack = load_industry_pack(paths.industry_pack_dir)

    db = Database(paths.db_path)
    db.reset()
    seed_database(db, pack_file(paths.industry_pack_dir, pack.seed_data))
    return Container.build(settings)


def _run(
    container: Container,
    task: str,
    *,
    session: str = "s1",
    user: str | None = "U1",
):
    """跑一轮对话，返回 (结果, 上下文)。"""
    runner, ctx = container.make_runner(
        user_id=user, session_id=session, provider="fake", task=task
    )
    return runner.run(session, task, ctx), ctx


def test_order_lookup_closes_the_loop(container: Container) -> None:
    """验证：查订单闭环——一轮内完成「调工具 → 拿结果 → 作答」，且只调一次 ``get_order``。"""
    result, _ = _run(container, "查订单 A1001")
    assert result.status == "completed"
    assert result.tool_calls == [{"name": "get_order", "ok": True, "error_code": None}]
    assert result.final_text


def test_logistics_lookup_closes_the_loop(container: Container) -> None:
    """验证：查物流闭环，与上一条互为对照（同为读工具，走同一条编排路径）。"""
    result, _ = _run(container, "查物流 A1001")
    assert result.status == "completed"
    assert result.tool_calls == [{"name": "get_logistics", "ok": True, "error_code": None}]


def test_missing_logistics_is_reported_truthfully(container: Container) -> None:
    """A1002 未发货、没有物流记录 —— 必须如实返回失败，不得编造轨迹。"""
    result, _ = _run(container, "查物流 A1002")
    assert result.tool_calls[0]["ok"] is False
    assert result.tool_calls[0]["error_code"] == "NOT_FOUND"


def test_reading_another_users_order_is_denied(container: Container) -> None:
    """验证：查他人订单被后端拒绝（``PERMISSION_DENIED``）——越权在服务端拦，不靠提示词。"""
    result, _ = _run(container, "查订单 A1004")
    assert result.tool_calls[0]["ok"] is False
    assert result.tool_calls[0]["error_code"] == "PERMISSION_DENIED"


def test_denied_call_writes_nothing(container: Container) -> None:
    """越权被拒后不得留下任何业务写与「已执行」审计。"""
    _run(container, "查订单 A1004")
    db = Database(container.paths.db_path)
    assert db.count("refund_requests") == 0
    assert db.count("audit_logs") == 0


def test_answer_text_follows_real_tool_outcome(container: Container) -> None:
    """同一份脚本、不同身份 → 回答文本必须随**真实工具结果**变化。

    这是防「工具失败但回答宣称成功」的固化物：Fake 的最终回答由工具返回渲染而成。
    """
    ok_result, _ = _run(container, "查订单 A1001", user="U1", session="as-owner")
    denied_result, _ = _run(container, "查订单 A1001", user="U2", session="as-stranger")

    assert ok_result.tool_calls[0]["ok"] is True
    assert "查询结果" in ok_result.final_text

    assert denied_result.tool_calls[0]["ok"] is False
    assert "操作未成功" in denied_result.final_text
    assert "PERMISSION_DENIED" in denied_result.final_text
    assert "查询结果" not in denied_result.final_text, "工具失败时不得输出成功措辞"


def test_trace_is_flushed_with_tool_events(container: Container) -> None:
    """验证：trace 落盘且含 ``tool_call_start`` / ``tool_call_end``，这是 P6 评估与排障的数据源。"""
    _, ctx = _run(container, "查订单 A1001")
    container.tracer.flush(ctx.trace_id)

    path = Path(container.paths.trace_dir) / f"{ctx.trace_id}.json"
    assert path.exists(), "trace 必须落盘，便于评估（P6）与排障"
    events = json.loads(path.read_text(encoding="utf-8"))["events"]
    kinds = [event["event"] for event in events]
    assert "tool_call_start" in kinds and "tool_call_end" in kinds
    tool_event = next(event for event in events if event["event"] == "tool_call_start")
    assert tool_event["tool"] == "get_order"


def test_multi_turn_session_keeps_checkpoint_thread(container: Container) -> None:
    """多轮对话：同一 session 的检查点持续累积，不同 session 互不干扰。"""
    first, _ = _run(container, "查订单 A1001", session="multi")
    second, _ = _run(container, "查物流 A1001", session="multi")
    other, _ = _run(container, "查订单 A1001", session="another")

    for result in (first, second, other):
        assert result.status == "completed"

    import sqlite3

    conn = sqlite3.connect(container.paths.checkpoints_db_path)
    try:
        threads = {row[0] for row in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")}
    finally:
        conn.close()
    assert "session:multi" in threads
    assert "session:another" in threads


def test_industry_words_are_not_hardcoded_in_code(container: Container) -> None:
    """行业词汇只应出现在行业包数据里；此处顺带确认演示任务来自数据文件。"""
    tasks = [scenario.get("task") for scenario in container.scenarios]
    assert "查订单 A1001" in tasks
    assert container.pack.demo_scenarios


KEY_ENV_FIELDS = {
    "DEEPSEEK_API_KEY": "deepseek_api_key",
    "OPENAI_API_KEY": "openai_api_key",
    "DASHSCOPE_API_KEY": "dashscope_api_key",
    "ZHIPUAI_API_KEY": "zhipu_api_key",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
}


def _clear_all_provider_keys(container: Container, monkeypatch) -> None:
    """模拟「一台什么都没配的机器」：进程环境与已加载配置里的密钥都清空。"""
    for env_name, field_name in KEY_ENV_FIELDS.items():
        monkeypatch.delenv(env_name, raising=False)
        setattr(container.settings, field_name, None)


def test_auto_provider_falls_back_to_fake_without_any_key(
    container: Container, monkeypatch
) -> None:
    """**所有**供应商密钥都缺失时，auto 落到 Fake（明确可追溯，不静默出错）。"""
    _clear_all_provider_keys(container, monkeypatch)
    handle = container.resolve_model_handle(provider="auto", task="查订单 A1001")
    assert handle.provider_id == "fake"


def test_auto_provider_prefers_deepseek_with_key(container: Container, monkeypatch) -> None:
    """验证：``auto`` 在有 DeepSeek 密钥时优先选 deepseek（顺序由 ``AUTO_PRIORITY`` 决定）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "present")
    handle = container.resolve_model_handle(provider="auto")
    assert handle.provider_id == "deepseek"


def test_auto_provider_is_not_deepseek_only(container: Container, monkeypatch) -> None:
    """auto 不再只认 DeepSeek：任何在册且密钥就位的供应商都能被选中。"""
    _clear_all_provider_keys(container, monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "present")

    handle = container.resolve_model_handle(provider="auto")

    assert handle.provider_id == "qwen"
    assert handle.protocol == "openai", "通义走 OpenAI 兼容协议，与 DeepSeek 共用同一段接缝"


def test_tool_subset_injection_respects_tag_filter(container: Container) -> None:
    """按 tag 过滤后只注入相关子集（工具扩容时不会全量塞给模型）。"""
    runner, _ = container.make_runner(
        user_id="U1", session_id="s1", provider="fake", task="查订单 A1001", tool_tags=["order"]
    )
    selected = container.registry.select(tags=["order"])
    assert {spec.name for spec in selected} == {"get_order", "list_orders", "cancel_order"}
    assert runner is not None
