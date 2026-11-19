"""pytest 共享 fixture。

约定：测试**不得**依赖真实网络与真实密钥；需要外部资源时一律注入替身。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from zkagent.backends.mock.backend import MockBackend
from zkagent.backends.mock.db import Database, SqliteIdempotencyStore
from zkagent.backends.mock.seed import seed_database
from zkagent.config import default_industry_packs_dir, load_industry_pack, pack_file
from zkagent.core.types import ToolContext

REPO_ROOT = Path(__file__).resolve().parents[1]
PACK_DIR = default_industry_packs_dir() / "appliance"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """仓库根目录（绝对路径）。"""
    return REPO_ROOT


@pytest.fixture(scope="session")
def pack_dir() -> Path:
    """默认行业包目录。"""
    return PACK_DIR


#: 所有带 ``pack.yaml`` 的行业包（手写包与生成包一视同仁）。
ALL_PACK_DIRS = sorted(
    path for path in default_industry_packs_dir().iterdir() if (path / "pack.yaml").is_file()
)


@pytest.fixture(params=ALL_PACK_DIRS, ids=lambda path: path.name)
def any_pack_dir(request: pytest.FixtureRequest) -> Path:
    """参数化到每个行业包：契约断言必须同时覆盖手写包与生成包。

    只测 ``appliance`` 会漏掉生成包的目录布局问题——那是真实发生过的。
    """
    return request.param


@pytest.fixture()
def fake_tree(tmp_path: Path) -> Callable[[str, str], Path]:
    """在临时目录中造一棵假源码树，用于验证静态扫描器「有牙齿」。

    返回的工厂函数签名：``make(相对路径, 文件内容) -> 绝对路径``。
    """

    def _make(relative_path: str, content: str) -> Path:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    return _make


@pytest.fixture()
def business_db(tmp_path: Path) -> Database:
    """每个用例一份独立的临时业务库，并已灌入行业包种子数据。"""
    db = Database(tmp_path / "business.db")
    db.reset()
    config = load_industry_pack(PACK_DIR)
    seed_database(db, pack_file(PACK_DIR, config.seed_data))
    return db


@pytest.fixture()
def backend(business_db: Database) -> MockBackend:
    """基于临时库的模拟后端。"""
    return MockBackend(business_db)


@pytest.fixture()
def idem_store(business_db: Database) -> SqliteIdempotencyStore:
    """落库的幂等存储（跨进程可验证）。"""
    return SqliteIdempotencyStore(business_db)


@pytest.fixture()
def ctx() -> ToolContext:
    """默认以 U1 身份调用（U1 拥有 A1001 / A1002 / A1003 / A1005 / A1006）。"""
    return ToolContext(
        trace_id="trace0000000", session_id="sess-1", 
        user_id="U1", channel_id="cli"
    )


@pytest.fixture()
def noop_sleep() -> Callable[[float], None]:
    """替代 ``time.sleep``，让重试用例不真的等待。"""

    def _sleep(_seconds: float) -> None:
        return None

    return _sleep
