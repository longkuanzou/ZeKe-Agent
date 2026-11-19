"""P7 legacy 业务系统演示服务。

它故意使用一套**与归一化协议不同的方言**：

- 请求：``{tool, params, actor}``
- 成功：``{code: 0, msg: "ok", data: {...}}``
- 失败：``{code: 1002, msg: "...", data: ...}``

真正的 ``HttpBackend`` 在客户端完成方言 → ``ToolResult`` 的翻译；Core 不感知。
这个服务只为演示/契约测试，用 MockBackend 提供数据，不是生产业务系统。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from zkagent.backends.http.backend import ERROR_TO_LEGACY_CODE
from zkagent.backends.mock.backend import MockBackend
from zkagent.backends.mock.db import Database
from zkagent.backends.mock.seed import seed_database
from zkagent.config import default_industry_packs_dir, load_industry_pack, pack_file
from zkagent.core.tools.schema import ErrorCode
from zkagent.core.types import ToolContext

__all__ = ["create_app", "app"]


class LegacyInvokeRequest(BaseModel):
    tool: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    actor: dict[str, Any] = Field(default_factory=dict)


def create_app(
    db_path: Path | str | None = None,
    *,
    industry_id: str = "appliance",
) -> FastAPI:
    """构造演示服务；测试可注入临时库，避免污染演示数据。"""
    path = Path(db_path or os.environ.get("LEGACY_DB_PATH", "data/legacy-business.db"))
    pack_dir = default_industry_packs_dir() / industry_id
    pack = load_industry_pack(pack_dir)
    database = Database(path)
    database.initialize()
    if database.count("orders") == 0:
        seed_database(database, pack_file(pack_dir, pack.seed_data))
    backend = MockBackend(database)

    app = FastAPI(title="legacy business system", version="legacy-v1")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "dialect": "legacy-v1",
            "industry": industry_id,
            "db": str(path),
        }

    @app.post("/legacy/invoke")
    def invoke(request: LegacyInvokeRequest) -> dict[str, Any]:
        actor = dict(request.actor or {})
        actor.setdefault("trace_id", "legacy")
        actor.setdefault("session_id", "legacy")
        ctx = ToolContext.model_validate(actor)
        result = backend.invoke(request.tool, dict(request.params), ctx)
        if result.ok:
            return {"code": 0, "msg": result.message or "ok", "data": result.data}
        code = ERROR_TO_LEGACY_CODE.get(result.error_code or ErrorCode.UNKNOWN.value, 1010)
        return {"code": code, "msg": result.message or "legacy business error", "data": result.data}

    return app


app = create_app()
