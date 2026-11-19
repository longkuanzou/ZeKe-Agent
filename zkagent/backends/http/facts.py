"""HTTP 后端的策略事实提供者。

**不偷读本地 Mock 库**：HTTP 模式下所有事实都通过 ``HttpBackend.invoke`` 查询，
证明「换业务后端后，规则判定仍由后端服务端数据驱动」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from zkagent.core.interfaces import BusinessBackend
from zkagent.core.policy.facts import FactsProvider, PolicyFacts
from zkagent.core.types import ToolContext

__all__ = ["BackendFactsProvider"]


def _days_since(value: str | None) -> int | None:
    """把后端返回的时间戳换算成「距今多少天」。

    后端可能给 ISO8601 带时区（``...Z``）的格式，这里先去时区再与本地时间相减。

    :param value: 后端返回的 ``delivered_at``；可为空。
    :returns: 整日数（负数按 0 处理）；为空或无法解析时返回 ``None``。
    """
    if not value:
        return None
    try:
        delivered = datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
    return max(0, (datetime.now() - delivered).days)


class BackendFactsProvider(FactsProvider):
    """用后端自身的 ``get_order`` / ``get_product`` / ``get_service_ticket`` 拼事实。"""

    def __init__(self, backend: BusinessBackend) -> None:
        self._backend = backend

    def facts_for(self, action: str, args: dict[str, Any], ctx: ToolContext) -> PolicyFacts:
        """通过后端自身的工具拼出判定事实（**不读本地库**）。

        顺序是「反查订单 → 查订单 → 查商品 → 查工单」，每步失败都只让对应字段为空；
        查询本身走 ``HttpBackend``，与真实执行同一条链路。

        :param action: 即将执行的工具名，用于判断 ``ticket_id`` 该反查哪类单据。
        :param args: 模型给出的调用参数。
        :param ctx: 工具上下文，``verified`` 取自会话核验结果。
        :returns: 填充好的 :class:`PolicyFacts`（查不到的字段留空）。
        """
        order_id = str(args.get("order_id") or "").strip()
        ticket_id = str(args.get("ticket_id") or "").strip()

        if not order_id and ticket_id:
            # 只给工单号 / 退款单号时，先反查所属订单；取不到就保持为空。
            lookup = (
                "get_service_ticket"
                if action in {"get_service_ticket", "schedule_service"}
                else "get_refund_ticket"
            )
            result = self._backend.invoke(lookup, {"ticket_id": ticket_id}, ctx)
            if result.ok and isinstance(result.data, dict):
                order_id = str(result.data.get("order_id") or "").strip()

        order: dict[str, Any] | None = None
        if order_id:
            result = self._backend.invoke("get_order", {"order_id": order_id}, ctx)
            if result.ok and isinstance(result.data, dict):
                order = dict(result.data)

        product: dict[str, Any] | None = None
        if order and order.get("sku"):
            result = self._backend.invoke("get_product", {"sku": str(order["sku"])}, ctx)
            if result.ok and isinstance(result.data, dict):
                product = dict(result.data)

        ticket: dict[str, Any] | None = None
        if ticket_id:
            result = self._backend.invoke("get_service_ticket", {"ticket_id": ticket_id}, ctx)
            if result.ok and isinstance(result.data, dict):
                ticket = dict(result.data)

        events: list[str] = []
        if ticket:
            events = [
                str(item.get("kind"))
                for item in ticket.get("events") or []
                if isinstance(item, dict) and item.get("kind")
            ]

        return PolicyFacts(
            verified=bool(ctx.verified),
            order_status=str(order["status"]) if order else None,
            amount_cents=int(order["amount_cents"]) if order else None,
            days_since_delivered=_days_since(order.get("delivered_at") if order else None),
            category=str(product["category"]) if product else None,
            appliance=str(product["name"]).split(" ")[0] if product else None,
            order_user_id=str(order["user_id"]) if order else None,
            ticket_status=str(ticket["status"]) if ticket else None,
            ticket_events=events,
        )
