"""M20：评估默认使用临时库，不能改动演示数据。"""

from __future__ import annotations

from pathlib import Path

import pytest

from zkagent.backends.mock.db import Database
from zkagent.backends.mock.seed import seed_database
from zkagent.config import Container, load_industry_pack, load_settings, pack_file
from zkagent.core.evals.runner import EvalRunner

APPLIANCE = Path(__file__).resolve().parents[2] / "zkagent" / "industry_packs" / "appliance"


@pytest.mark.skipif(not APPLIANCE.is_dir(), reason="缺少 appliance 包")
def test_eval_uses_temp_db_and_keeps_demo_data(tmp_path: Path) -> None:
    """验证：评估默认用临时库，改隔离库的状态不会污染演示库（M20）。"""
    original_path = tmp_path / "business.db"
    db = Database(original_path)
    db.reset()
    config = load_industry_pack(APPLIANCE)
    seed_database(db, pack_file(APPLIANCE, config.seed_data))
    before = db.count("orders")

    settings = load_settings(
        industry_id="appliance",
        db_path=str(original_path),
        checkpoints_db_path=str(tmp_path / "checkpoints.db"),
    )
    container = Container.build(settings)
    runner = EvalRunner(container, provider="fake", isolate=True)
    try:
        assert Path(runner.db_path) != original_path
        assert container.backend.database.path == Path(runner.db_path)

        # 在隔离库里改订单状态，演示库必须完全不受影响
        with container.backend.database.transaction() as conn:
            conn.execute("UPDATE orders SET status = 'cancelled' WHERE order_id = 'A1001'")
        with db.connect() as conn:
            status = conn.execute(
                "SELECT status FROM orders WHERE order_id = 'A1001'"
            ).fetchone()["status"]
        assert status != "cancelled"
        assert db.count("orders") == before
    finally:
        runner.close()


@pytest.mark.skipif(not APPLIANCE.is_dir(), reason="缺少 appliance 包")
def test_eval_can_disable_isolation(tmp_path: Path) -> None:
    """验证：``isolate=False`` 时直接用原库——需要复现问题时可以关掉隔离。"""
    original_path = tmp_path / "business.db"
    db = Database(original_path)
    db.reset()
    config = load_industry_pack(APPLIANCE)
    seed_database(db, pack_file(APPLIANCE, config.seed_data))

    settings = load_settings(
        industry_id="appliance",
        db_path=str(original_path),
        checkpoints_db_path=str(tmp_path / "checkpoints.db"),
    )
    container = Container.build(settings)
    runner = EvalRunner(container, provider="fake", isolate=False)
    try:
        assert Path(runner.db_path) == original_path
        assert container.backend.database.path == original_path
    finally:
        runner.close()
