"""P7：3c 第二行业包必须可加载、可查单，并与 appliance 数据/索引隔离。"""

from __future__ import annotations

from pathlib import Path

from zkagent.backends.mock.db import Database
from zkagent.backends.mock.seed import seed_database
from zkagent.config import Container, load_industry_pack, load_settings, pack_file

PACK_DIR = Path(__file__).resolve().parents[2] / "zkagent" / "industry_packs" / "3c"


def test_3c_pack_loads_and_answers_order(tmp_path: Path) -> None:
    """验证：第二行业包（3c）可加载、可灌库、可跑通查单，且仍走 mock 后端。"""
    db = Database(tmp_path / "business-3c.db")
    db.reset()
    pack = load_industry_pack(PACK_DIR)
    seed_database(db, pack_file(PACK_DIR, pack.seed_data))

    settings = load_settings(
        industry_id="3c",
        db_path=str(db.path),
        checkpoints_db_path=str(tmp_path / "checkpoints-3c.db"),
    )
    container = Container.build(settings)
    runner, ctx = container.make_runner(
        user_id="U9",
        session_id="3c-test",
        provider="fake",
        task="查订单 B2001",
    )
    result = runner.run("3c-test", "查订单 B2001", ctx)

    assert result.status == "completed"
    assert "B2001" in result.final_text
    assert any(call.get("name") == "get_order" and call.get("ok") for call in result.tool_calls)
    assert container.backend.backend_id == "mock"


def test_3c_product_search_uses_phone_category(tmp_path: Path) -> None:
    """验证：3c 包的商品目录用自身品类（phone），不是照搬 appliance 的家电品类。"""
    db = Database(tmp_path / "business-3c.db")
    db.reset()
    pack = load_industry_pack(PACK_DIR)
    seed_database(db, pack_file(PACK_DIR, pack.seed_data))

    from zkagent.backends.mock.backend import MockBackend
    from zkagent.core.types import ToolContext

    backend = MockBackend(db)
    ctx = ToolContext(trace_id="t3c", session_id="s3c", user_id="U9", channel_id="cli")
    result = backend.invoke("search_products", {"category": "phone"}, ctx)

    assert result.ok is True
    assert result.data["count"] >= 1
    assert result.data["products"][0]["sku"] == "PHN-4999-X"
