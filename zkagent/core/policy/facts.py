"""策略判定所需的事实（facts）。

**为什么单独一层**：规则要判「订单是什么状态、签收几天、金额多少、是否已核验」，
这些事实来自业务库。Core 不能 import 任何后端，所以定义协议，由后端实现。
这样换后端时 Rules 与 Engine 一行不改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from zkagent.core.types import ToolContext

__all__ = ["PolicyFacts", "FactsProvider"]


class PolicyFacts(BaseModel):
    """一次判定所需的全部事实；**缺失即视为不满足**（不猜默认值）。"""

    verified: bool = Field(default=False, description="当前会话是否已通过身份核验")
    order_status: str | None = Field(
        default=None, description="created/paid/shipped/delivered/cancelled"
    )
    amount_cents: int | None = Field(default=None, description="订单金额（分）")
    days_since_delivered: int | None = Field(default=None, description="签收至今天数；未签收为 None")
    category: str | None = Field(default=None, description="品类键（aircon/washer/rice_cooker…）")
    appliance: str | None = Field(default=None, description="品类中文名（兼容规则写法）")
    order_user_id: str | None = Field(default=None, description="订单归属用户，用于 owner_match")
    prior_tools: list[str] = Field(default_factory=list, description="本轮已成功调用过的工具名")
    query_text: str = Field(default="", description="用户原话（安全红线与越界品类判定用）")
    ticket_status: str | None = None
    ticket_events: list[str] = Field(default_factory=list)

    def text(self) -> str:
        """归一化后的用户原话：去空白，便于关键词命中。"""
        return "".join(self.query_text.split()).lower()


class FactsProvider(ABC):
    """事实提供者：按「动作 + 参数 + 上下文」取判定所需事实。

    实现方负责**只读**查询；取不到就返回空 ``PolicyFacts``（规则会按「不满足」处理），
    不得伪造状态。
    """

    @abstractmethod
    def facts_for(self, action: str, args: dict[str, Any], ctx: ToolContext) -> PolicyFacts:
        """返回该次调用的事实。"""
