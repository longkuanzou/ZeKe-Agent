"""后端契约测试同时跑 MockBackend 与 HttpBackend（P7）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.legacy_api import create_app
from zkagent.backends.http import HttpBackend
from zkagent.backends.mock.backend import MockBackend


@pytest.fixture(params=["mock", "http"])
def backend(request: pytest.FixtureRequest, business_db):
    if request.param == "mock":
        yield MockBackend(business_db)
        return

    app = create_app(business_db.path)
    client = TestClient(app)
    http = HttpBackend("http://legacy.test", client=client)
    try:
        yield http
    finally:
        http.close()
