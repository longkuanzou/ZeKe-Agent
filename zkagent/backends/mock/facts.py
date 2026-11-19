"""从 SQLite 取策略判定所需的事实。

只做**读**：订单状态/金额/签收天数、商品品类、工单状态与事件。
取不到就返回空事实（规则按「不满足」处理），**不猜**默认值。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from zkagent.backends.mock.db import Database
from zkagent.core.policy.facts import FactsProvider, PolicyFacts
from zkagent.core.types import ToolContext

__all__ = ["MockFactsProvider"]

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _days_since(value: str | None) -> int | None:
    """把时间戳换算成「距今多少天」。

    :param value: 库内时间字符串（``YYYY-MM-DDTHH:MM:SS``）；可为空。
    :returns: 整日数（负数按 0 处理）；值为空或格式非法时返回 ``None``，
        让区间规则按「不满足」处理，而不是当成「0 天前」。
    """
    if not value:
        return None
    try:
        moment = datetime.strptime(value, _TIME_FORMAT)
    except ValueError:
        return None
    return max((datetime.now() - moment).days, 0)


class MockFactsProvider(FactsProvider):
    """SQLite 事实提供者。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def facts_for(self, action: str, args: dict[str, Any], ctx: ToolContext) -> PolicyFacts:
        """取出本次工具调用的判定事实。

        只读、无副作用；任何一项查不到就留空，交由规则按「不满足」处理。

        :param action: 即将执行的工具名，用于判断 ``ticket_id`` 是工单号还是退款单号。
        :param args: 模型给出的调用参数（可能只有 ``order_id`` 或只有 ``ticket_id``）。
        :param ctx: 工具上下文；``verified`` 直接取自会话核验结果，不查库推断。
        :returns: 填充好的 :class:`PolicyFacts`（缺失字段为 ``None`` / 空列表）。
        """
        order_id, ticket_id = self._resolve_targets(action, args)
        with self._db.transaction() as conn:
            order = self._row(conn, "orders", "order_id", order_id) if order_id else None
            product = (
                self._row(conn, "products", "sku", str(order["sku"])) if order else None
            )
            ticket = (
                self._row(conn, "service_tickets", "ticket_id", ticket_id) if ticket_id else None
            )
            events: list[str] = []
            if ticket is not None:
                events = [
                    str(row["kind"])
                    for row in conn.execute(
                        "SELECT kind FROM ticket_events WHERE ticket_id = ?", (ticket["ticket_id"],)
                    ).fetchall()
                ]

        return PolicyFacts(
            verified=bool(ctx.verified),
            order_status=str(order["status"]) if order else None,
            amount_cents=int(order["amount_cents"]) if order else None,
            days_since_delivered=_days_since(order["delivered_at"] if order else None),
            category=str(product["category"]) if product else None,
            appliance=str(product["name"]).split(" ")[0] if product else None,
            order_user_id=str(order["user_id"]) if order else None,
            ticket_status=str(ticket["status"]) if ticket else None,
            ticket_events=events,
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _row(conn, table: str, key: str, value: Any) -> dict[str, Any] | None:
        """按主键查一行并转成 dict。

        :param conn: 已开启事务的连接。
        :param table: 表名，仅来自本模块内部常量，不接受外部输入。
        :param key: 列名（``order_id`` / ``sku`` / ``ticket_id``）。
        :param value: 主键值。
        :returns: 行字典；查不到返回 ``None``。
        """
        row = conn.execute(f'SELECT * FROM "{table}" WHERE "{key}" = ?', (value,)).fetchone()
        return dict(row) if row is not None else None

    def _resolve_targets(
        self, action: str, args: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """把「订单号 / 工单号 / 退款单号」统一成 (order_id, ticket_id)。

        模型给的是它知道的编号：退款类经常只给订单号，工单类给 ticket_id。
        这里负责反查，**不要求调用方先查一遍**。
        """
        order_id = args.get("order_id")
        ticket_id = args.get("ticket_id")

        if order_id:
            return str(order_id), (str(ticket_id) if ticket_id else None)
        if not ticket_id:
            return None, None

        # 只有 ticket_id：按动作判断它是工单号还是退款单号，并反查所属订单
        with self._db.transaction() as conn:
            if action in {"get_service_ticket", "schedule_service"}:
                row = conn.execute(
                    "SELECT order_id FROM service_tickets WHERE ticket_id = ?", (ticket_id,)
                ).fetchone()
                return (
                    (str(row["order_id"]) if row is not None else None),
                    str(ticket_id),
                )
            row = conn.execute(
                "SELECT order_id FROM refund_requests WHERE refund_id = ? OR order_id = ? LIMIT 1",
                (ticket_id, ticket_id),
            ).fetchone()
            if row is not None:
                return str(row["order_id"]), None
        return None, str(ticket_id)
