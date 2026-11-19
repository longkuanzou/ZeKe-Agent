"""业务库（SQLite）的建表、连接与幂等存储实现。

库文件：按行业包隔离（见 ``zkagent/config.py::_runtime_db_paths``）——
默认包 ``appliance`` 用 ``data/business.db``，其他包用 ``data/business-<id>.db``。
表（15 张）：

| 类别 | 表 |
| --- | --- |
| 主体 | ``users`` / ``user_homes`` / ``orders`` / ``products`` |
| 售前 | ``inventory`` / ``promotions`` |
| 履约 | ``logistics`` / ``logistics_events`` / ``invoices`` |
| 售后 | ``service_tickets`` / ``ticket_events`` / ``refund_requests`` |
| 治理 | ``idempotency_keys`` / ``approvals`` / ``audit_logs`` |

关键约束（DESIGN §7.4）：
- 金额一律用**整数分**（``amount_cents``），禁止浮点；
- 四种状态字段彼此独立：``approvals.status`` / ``approvals.execution_status`` /
  ``refund_requests.status`` / ``ToolResult.ok``，不得混用；
- 检查点库 ``data/checkpoints.db`` 属 P2/P4（由编排框架自管），本阶段不创建。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "Database",
    "SqliteIdempotencyStore",
    "SCHEMA_SQL",
    "REQUIRED_TABLES",
    "utc_now",
]

#: 业务库必须存在的表（``db check`` 与升级提示的唯一清单）。
REQUIRED_TABLES: tuple[str, ...] = (
    "users",
    "user_homes",
    "orders",
    "products",
    "inventory",
    "promotions",
    "logistics",
    "logistics_events",
    "service_tickets",
    "ticket_events",
    "refund_requests",
    "invoices",
    "idempotency_keys",
    "approvals",
    "audit_logs",
)

#: 与 ``refund_requests`` 等业务表共用的时间格式。
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def utc_now() -> str:
    """返回与种子数据同格式的当前时间字符串（秒级，无时区后缀）。"""
    return datetime.now(timezone.utc).strftime(_TIME_FORMAT)


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    phone_masked TEXT NOT NULL,
    level        TEXT NOT NULL DEFAULT 'standard',
    created_at   TEXT NOT NULL
);

-- 房屋条件：安装适配的判定依据（门宽 / 电梯 / 水电位 / 外机位）
-- 为什么不塞进 users：一个用户可能有多套房，且这些字段只服务安装，不属于账户属性
CREATE TABLE IF NOT EXISTS user_homes (
    home_id        TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users(user_id),
    city           TEXT NOT NULL,
    district       TEXT NOT NULL DEFAULT '',
    floor          INTEGER NOT NULL CHECK (floor >= 1),
    has_elevator   INTEGER NOT NULL CHECK (has_elevator IN (0, 1)),
    door_width_cm  INTEGER NOT NULL CHECK (door_width_cm > 0),
    water_inlet    INTEGER NOT NULL CHECK (water_inlet IN (0, 1)),
    drain          INTEGER NOT NULL CHECK (drain IN (0, 1)),
    outdoor_slot   TEXT NOT NULL DEFAULT '',   -- 有 / 无 / 需吊装
    power_spec     TEXT NOT NULL DEFAULT '',   -- 10A / 16A / 独立回路
    is_default     INTEGER NOT NULL DEFAULT 1 CHECK (is_default IN (0, 1)),
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id       TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users(user_id),
    product        TEXT NOT NULL,
    sku            TEXT NOT NULL,
    amount_cents   INTEGER NOT NULL CHECK (amount_cents >= 0),
    status         TEXT NOT NULL
                   CHECK (status IN ('created', 'paid', 'shipped', 'delivered', 'cancelled')),
    created_at     TEXT NOT NULL,
    delivered_at   TEXT,
    address_masked TEXT NOT NULL
);

-- 商品目录：售前「硬参数」的唯一来源（型号/容量/能效/尺寸/价格/功能）
-- 与知识库的分工：政策与流程在 knowledge/，**参数只在这里**，避免两处各写一份而漂移
CREATE TABLE IF NOT EXISTS products (
    sku           TEXT PRIMARY KEY,
    category      TEXT NOT NULL
                  CHECK (category IN ('aircon', 'fridge', 'washer',
                                      'rice_cooker', 'phone', 'tablet')),
    model         TEXT NOT NULL,
    name          TEXT NOT NULL,
    price_cents   INTEGER NOT NULL CHECK (price_cents >= 0),
    capacity      TEXT NOT NULL DEFAULT '',
    energy_grade  TEXT NOT NULL DEFAULT '',
    dimensions_mm TEXT NOT NULL DEFAULT '',
    features_json TEXT NOT NULL DEFAULT '[]',
    install_notes TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL CHECK (status IN ('on_sale', 'off_shelf')),
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_products_category ON products(category);

-- 库存：只回答「有没有货、什么时候补」，不参与价格与推荐排序
CREATE TABLE IF NOT EXISTS inventory (
    sku          TEXT PRIMARY KEY REFERENCES products(sku),
    warehouse    TEXT NOT NULL,
    qty          INTEGER NOT NULL CHECK (qty >= 0),
    restock_eta  TEXT,                          -- NULL = 无需补货
    updated_at   TEXT NOT NULL
);

-- 促销：价格之外的「到手价」构成（直降 / 满减 / 赠品 / 以旧换新 / 分期）
-- 起止时间必须存，否则「现在还有活动吗」「价保补差」都无从判定
CREATE TABLE IF NOT EXISTS promotions (
    promo_id        TEXT PRIMARY KEY,
    sku             TEXT REFERENCES products(sku),   -- NULL 表示品类级活动
    category        TEXT
                    CHECK (category IS NULL
                           OR category IN ('aircon', 'fridge', 'washer',
                                           'rice_cooker', 'phone', 'tablet')),
    kind            TEXT NOT NULL
                    CHECK (kind IN ('discount', 'threshold', 'gift', 'trade_in', 'installment')),
    amount_cents    INTEGER NOT NULL DEFAULT 0 CHECK (amount_cents >= 0),
    threshold_cents INTEGER NOT NULL DEFAULT 0 CHECK (threshold_cents >= 0),
    gift            TEXT NOT NULL DEFAULT '',
    start_at        TEXT NOT NULL,
    end_at          TEXT NOT NULL,
    stackable       INTEGER NOT NULL DEFAULT 0 CHECK (stackable IN (0, 1)),
    note            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS logistics (
    order_id    TEXT PRIMARY KEY REFERENCES orders(order_id),
    carrier     TEXT NOT NULL,
    tracking_no TEXT NOT NULL,
    status      TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 物流轨迹：`logistics` 只存「最新状态」，轨迹与停滞判定必须靠事件流水
CREATE TABLE IF NOT EXISTS logistics_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT NOT NULL REFERENCES orders(order_id),
    happened_at TEXT NOT NULL,
    node        TEXT NOT NULL,                  -- 揽收 / 运输中 / 派送中 / 已签收 / 异常
    description TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_logistics_events_order
    ON logistics_events(order_id, happened_at);

CREATE TABLE IF NOT EXISTS service_tickets (
    ticket_id      TEXT PRIMARY KEY,
    order_id       TEXT NOT NULL REFERENCES orders(order_id),
    user_id        TEXT NOT NULL REFERENCES users(user_id),
    type           TEXT NOT NULL CHECK (type IN ('install', 'repair', 'maintenance')),
    description    TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL
                   CHECK (status IN ('created', 'scheduled', 'completed', 'cancelled')),
    scheduled_time TEXT,
    created_at     TEXT NOT NULL
);

-- 工单事件：状态只能表达「进行到哪一格」，事件才能回答「卡在哪一步、卡了多久」
-- （备件等待、师傅已联系、二次上门等，都放这里而不是往 status 枚举里塞值）
CREATE TABLE IF NOT EXISTS ticket_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT NOT NULL REFERENCES service_tickets(ticket_id),
    happened_at TEXT NOT NULL,
    kind        TEXT NOT NULL
                CHECK (kind IN ('created', 'scheduled', 'assigned', 'part_wait',
                                'repaired', 'closed', 'cancelled')),
    note        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_ticket_events_ticket
    ON ticket_events(ticket_id, happened_at);

CREATE TABLE IF NOT EXISTS refund_requests (
    refund_id       TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES orders(order_id),
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    items_json      TEXT NOT NULL DEFAULT '[]',
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    reason          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL
                    CHECK (status IN ('pending_confirmation', 'approved', 'rejected', 'completed')),
    idempotency_key TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 同一幂等键只允许落一笔退款单（与 idempotency_keys 表形成双重防护）
CREATE UNIQUE INDEX IF NOT EXISTS ux_refund_requests_idem
    ON refund_requests(idempotency_key) WHERE idempotency_key IS NOT NULL;

-- 发票：政策在知识库，**状态只能在数据里**（「我的发票开了吗」没有表就只能背政策）
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id   TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL REFERENCES orders(order_id),
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    kind         TEXT NOT NULL CHECK (kind IN ('normal', 'special')),
    title        TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    status       TEXT NOT NULL
                 CHECK (status IN ('not_requested', 'pending', 'issued', 'red_flushed')),
    issued_at    TEXT,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_invoices_order ON invoices(order_id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    idem_key     TEXT PRIMARY KEY,
    tool_name    TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- HITL 审批：status 是审批生命周期，execution_status 是批准后的业务执行结果，两者正交
CREATE TABLE IF NOT EXISTS approvals (
    approval_id          TEXT PRIMARY KEY,
    thread_id            TEXT,
    session_id           TEXT,
    action               TEXT NOT NULL,
    payload_json         TEXT NOT NULL DEFAULT '{}',
    params_fingerprint   TEXT NOT NULL,
    status               TEXT NOT NULL
                         CHECK (status IN ('pending', 'approved', 'rejected',
                                           'expired', 'cancelled')),
    requester_id         TEXT,
    approver_id          TEXT,
    approver_mode        TEXT NOT NULL DEFAULT 'dev_unauthenticated',
    approved_payload_json TEXT,
    consumed_at          TEXT,
    execution_status     TEXT NOT NULL DEFAULT 'not_started'
                         CHECK (execution_status IN ('not_started', 'succeeded', 'failed')),
    execution_error_code TEXT,
    -- 判定归因：审批列表要能说清「为什么需要审批」，不能只给一个动作名
    reason               TEXT NOT NULL DEFAULT '',
    rule_id              TEXT NOT NULL DEFAULT '',
    -- 审批人必须同时具备的权限（JSON 数组）；空数组表示不限制
    required_permissions TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL,
    resolved_at          TEXT
);

CREATE TABLE IF NOT EXISTS audit_logs (
    audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id    TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT,
    before_json TEXT,
    after_json  TEXT,
    decision    TEXT,
    rule_id     TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_audit_logs_trace ON audit_logs(trace_id);
"""


class Database:
    """业务库访问入口：建表、事务、查询与幂等存储。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ 基础设施

    def connect(self) -> sqlite3.Connection:
        """建立连接（启用外键约束；行以 ``sqlite3.Row`` 返回）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """事务上下文：正常提交，异常回滚（保证失败不留半截写）。"""
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        """建表（幂等：已存在的表不会被改动；缺列则补齐）。

        ``CREATE TABLE IF NOT EXISTS`` 不会给旧库加列，因此 P4 新增的
        ``approvals.reason`` / ``approvals.rule_id`` 用一次显式迁移补上。
        """
        with self.transaction() as conn:
            conn.executescript(SCHEMA_SQL)
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(approvals)").fetchall()
            }
            for name, ddl in (
                ("reason", "TEXT NOT NULL DEFAULT ''"),
                ("rule_id", "TEXT NOT NULL DEFAULT ''"),
                ("required_permissions", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if name not in columns:
                    conn.execute(f'ALTER TABLE approvals ADD COLUMN "{name}" {ddl}')

    def reset(self) -> None:
        """删除全部业务表并重建（开发 / 测试用，**不可**用于生产）。"""
        with self.transaction() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall():
                conn.execute(f'DROP TABLE IF EXISTS "{row["name"]}"')
            conn.execute("PRAGMA foreign_keys = ON")
        self.initialize()

    # ------------------------------------------------------------------ 查询辅助

    def table_names(self) -> list[str]:
        """返回业务表名（不含 SQLite 内部表）。"""
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
        return [row["name"] for row in rows]

    def count(self, table: str) -> int:
        """统计表行数（表名会做白名单校验，避免拼接注入）。"""
        self._assert_known_table(table)
        with self.transaction() as conn:
            return int(conn.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()["n"])

    def rows(self, table: str, limit: int | None = None) -> list[dict[str, Any]]:
        """读取表内容（按主键顺序），用于 ``zkagent db inspect``。"""
        self._assert_known_table(table)
        sql = f'SELECT * FROM "{table}"'
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self.transaction() as conn:
            return [dict(row) for row in conn.execute(sql).fetchall()]

    @staticmethod
    def _assert_known_table(table: str) -> None:
        """校验表名在白名单内，避免调试接口被拼进任意表名。

        :param table: 待校验的表名（来自测试/脚本，不进 SQL 字符串拼接之外的路径）。
        :raises ValueError: 表名不在允许集合内，消息里给出可选值。
        """
        known = set(REQUIRED_TABLES)
        if table not in known:
            raise ValueError(f"未知表名：{table!r}（可选：{', '.join(sorted(known))}）")


class SqliteIdempotencyStore:
    """把幂等键落到 ``idempotency_keys`` 表，保证**跨进程**可验证。

    实现 ``zkagent.core.tools.executor.IdempotencyStore`` 协议：
    ``get`` 返回 ``(request_hash, result_dict)`` 或 ``None``；``put`` 写入记录。
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, key: str) -> tuple[str, dict[str, Any]] | None:
        """按幂等键读取历史记录。"""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT request_hash, result_json FROM idempotency_keys WHERE idem_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return str(row["request_hash"]), json.loads(row["result_json"])

    def put(self, key: str, tool_name: str, request_hash: str, result: dict[str, Any]) -> None:
        """写入幂等记录；键已存在时不覆盖（避免并发下互相改写）。"""
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO idempotency_keys "
                "(idem_key, tool_name, request_hash, result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, tool_name, request_hash, json.dumps(result, ensure_ascii=False), utc_now()),
            )
