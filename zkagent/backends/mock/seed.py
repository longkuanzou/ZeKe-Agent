"""按行业包 ``seed_data/`` 灌入种子数据。

全部数据来自行业包的 YAML 文件（**不在代码里写死任何业务数据**）。
写入使用 ``INSERT OR REPLACE``，因此可重复执行而不会因主键冲突失败。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from zkagent.backends.mock.db import Database

#: ``@now-2h`` / ``@now+5d`` / ``@now-30m``：灌库时解析，避免种子随时间老化（M16）
_TIME_MACRO = re.compile(r"^@now([+-])(\d+)([hdm])$")
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _resolve_macro(value: Any, *, now: datetime | None = None) -> Any:
    """把种子里的相对时间宏解析成绝对时间戳。

    支持 ``@now-2h`` / ``@now+5d`` / ``@now-30m``（单位 h/d/m）。宏在**灌库时**
    才展开，因此重新 seed 会得到「相对当下」的新时间，缓解种子随时间老化（M16）。

    :param value: 种子字段值；非字符串或非宏格式原样返回。
    :param now: 参照时间，默认当前时间（截到秒），便于测试注入。
    :returns: 解析后的 ``YYYY-MM-DDTHH:MM:SS`` 字符串，或原值。
    """
    if not isinstance(value, str):
        return value
    match = _TIME_MACRO.match(value.strip())
    if match is None:
        return value
    sign, amount, unit = match.groups()
    delta = timedelta(
        hours=int(amount) if unit == "h" else 0,
        days=int(amount) if unit == "d" else 0,
        minutes=int(amount) if unit == "m" else 0,
    )
    moment = (now or datetime.now().replace(microsecond=0)) + (delta if sign == "+" else -delta)
    return moment.strftime(_TIME_FORMAT)

__all__ = ["seed_database", "SEED_FILES", "OPTIONAL_SEED_FILES"]


#: 种子文件 → (目标表, 列清单)。列顺序与 YAML 字段一一对应。
SEED_FILES: dict[str, tuple[str, tuple[str, ...]]] = {
    # 顺序即插入顺序：被引用的表必须写在引用方之前（外键是开启的）
    "users.yaml": (
        "users",
        ("user_id", "name", "phone_masked", "level", "created_at"),
    ),
    "user_homes.yaml": (
        "user_homes",
        (
            "home_id", "user_id", "city", "district", "floor", "has_elevator",
            "door_width_cm", "water_inlet", "drain", "outdoor_slot", "power_spec",
            "is_default", "updated_at",
        ),
    ),
    "products.yaml": (
        "products",
        (
            "sku", "category", "model", "name", "price_cents", "capacity",
            "energy_grade", "dimensions_mm", "features_json", "install_notes",
            "status", "updated_at",
        ),
    ),
    "inventory.yaml": (
        "inventory",
        ("sku", "warehouse", "qty", "restock_eta", "updated_at"),
    ),
    "promotions.yaml": (
        "promotions",
        (
            "promo_id", "sku", "category", "kind", "amount_cents", "threshold_cents",
            "gift", "start_at", "end_at", "stackable", "note",
        ),
    ),
    "orders.yaml": (
        "orders",
        (
            "order_id", "user_id", "product", "sku", "amount_cents",
            "status", "created_at", "delivered_at", "address_masked",
        ),
    ),
    "logistics.yaml": (
        "logistics",
        ("order_id", "carrier", "tracking_no", "status", "updated_at"),
    ),
    "logistics_events.yaml": (
        "logistics_events",
        ("order_id", "happened_at", "node", "description"),
    ),
    "service_tickets.yaml": (
        "service_tickets",
        (
            "ticket_id", "order_id", "user_id", "type", "description",
            "status", "scheduled_time", "created_at",
        ),
    ),
    "ticket_events.yaml": (
        "ticket_events",
        ("ticket_id", "happened_at", "kind", "note"),
    ),
    "refund_requests.yaml": (
        "refund_requests",
        (
            "refund_id", "order_id", "user_id", "items_json", "amount_cents",
            "reason", "status", "idempotency_key", "created_at", "updated_at",
        ),
    ),
    "invoices.yaml": (
        "invoices",
        (
            "invoice_id", "order_id", "user_id", "kind", "title",
            "amount_cents", "status", "issued_at", "updated_at",
        ),
    ),
}

#: 并非每个行业包都需要的种子文件：缺失时视为 0 行，不报错。
#:
#: 为什么允许缺：``appliance`` 是订单/退款演示包，没有库存、促销、发票这些表的数据；
#: 强制它补齐 7 个空文件只会制造噪音。需要这些数据的包（如 ``homeapp``）由测试断言
#: 「每张表都必须有行」来兜底，避免「悄悄为空」。
OPTIONAL_SEED_FILES: frozenset[str] = frozenset(
    {
        "user_homes.yaml",
        "inventory.yaml",
        "promotions.yaml",
        "logistics_events.yaml",
        "ticket_events.yaml",
        "refund_requests.yaml",
        "invoices.yaml",
    }
)


def _load_records(path: Path) -> list[dict[str, Any]]:
    """读取种子 YAML；顶层必须只有一个键，其值为记录列表。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or len(data) != 1:
        raise ValueError(f"种子文件格式非法（顶层应为单个列表键）：{path}")
    records = next(iter(data.values()))
    if records is None:
        return []
    if not isinstance(records, list):
        raise ValueError(f"种子文件内容应为列表：{path}")
    return records


def seed_database(db: Database, seed_dir: Path | str) -> dict[str, int]:
    """把 ``seed_dir`` 下的种子数据写入业务库。

    :returns: 表名 → 写入行数。
    """
    seed_dir = Path(seed_dir)
    counts: dict[str, int] = {}
    with db.transaction() as conn:
        for filename, (table, columns) in SEED_FILES.items():
            path = seed_dir / filename
            if not path.exists():
                if filename in OPTIONAL_SEED_FILES:
                    counts[table] = 0
                    continue
                raise FileNotFoundError(f"缺少种子文件：{path}")
            records = _load_records(path)
            if not records:
                counts[table] = 0
                continue
            placeholders = ", ".join("?" for _ in columns)
            column_sql = ", ".join(f'"{column}"' for column in columns)
            sql = f'INSERT OR REPLACE INTO "{table}" ({column_sql}) VALUES ({placeholders})'
            conn.executemany(
                sql,
                [
                    tuple(_resolve_macro(record.get(column)) for column in columns)
                    for record in records
                ],
            )
            counts[table] = len(records)
    return counts
