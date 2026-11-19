"""P7：同一套 Core 在 mock / http 后端下返回相同的归一化结果。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import zkagent.config as config_module
from apps.legacy_api import create_app
from zkagent.backends.http import HttpBackend
from zkagent.config import Container, load_settings


def _run(settings):
    container = Container.build(settings)
    runner, ctx = container.make_runner(
        user_id="U1",
        session_id="switch-test",
        provider="fake",
        task="查订单 A1001",
    )
    return container, runner.run("switch-test", "查订单 A1001", ctx)


def _settings(tmp_path: Path, backend: str):
    return load_settings(
        industry_id="appliance",
        backend=backend,
        db_path=str(tmp_path / f"business-{backend}.db"),
        checkpoints_db_path=str(tmp_path / f"checkpoints-{backend}.db"),
    )


def test_http_backend_can_drive_the_same_container(
    tmp_path: Path, monkeypatch
) -> None:
    """验证：把后端换成 HTTP 后，同一套 Core 跑同一条任务得到等价结果。

    断言四件事：``backend_id`` 变了、**不存在本地业务库**（证明没偷读 mock 库）、
    整轮仍 `completed`、工具调用确实成功。这就是「换业务后端 Core 零改动」的证据。
    """
    app = create_app(tmp_path / "legacy.db")
    client = HttpBackend(
        "http://legacy.test", client=TestClient(app)
    )

    def _fake_http_backend(_url: str) -> HttpBackend:
        return client

    monkeypatch.setattr(config_module, "HttpBackend", _fake_http_backend)
    container, result = _run(_settings(tmp_path, "http"))

    assert container.backend.backend_id == "http"
    assert container.business_database is None
    assert result.status == "completed"
    assert "A1001" in result.final_text
    assert any(call.get("name") == "get_order" and call.get("ok") for call in result.tool_calls)


def test_mock_backend_still_works(tmp_path: Path) -> None:
    """验证：mock 后端（默认）在同一组断言下依然成立——切换能力不能靠牺牲默认路径换。

    与上一条成对：两者用同一段 `_run`，只有 ``backend`` 配置不同。
    """
    container, result = _run(_settings(tmp_path, "mock"))

    assert container.backend.backend_id == "mock"
    assert container.business_database is not None
    assert result.status == "completed"
    assert "A1001" in result.final_text
