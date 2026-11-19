"""把多个工具来源合并成一个 ``BusinessBackend``。

存在的理由：``knowledge_search`` 来自 Core 的 RAG（**不是**业务 API），而
``ToolExecutor`` 只认一个 ``BusinessBackend``。合并之后：

- ``ToolRegistry`` / ``ToolExecutor`` / 编排接缝都**不需要知道**工具来自哪一侧；
- ``ToolSpec`` 依旧是工具定义的唯一事实来源（合并只是把多份声明拼在一起）；
- 重名工具**直接报错**，避免「同名工具被后注册的悄悄顶掉」这种极难排查的问题。
"""

from __future__ import annotations

from collections.abc import Sequence

from zkagent.core.interfaces import BusinessBackend
from zkagent.core.types import ToolContext, ToolResult, ToolSpec

__all__ = ["CompositeBackend"]


class CompositeBackend(BusinessBackend):
    """按工具名路由到不同来源的后端组合。

    :param primary: 业务后端（订单 / 退款 / 工单 / 物流……）。
    :param extras: 额外来源（如 RAG 的 ``knowledge_search``）。
    """

    backend_id = "composite"

    def __init__(
        self, primary: BusinessBackend, extras: Sequence[BusinessBackend] = ()
    ) -> None:
        self._primary = primary
        self._sources: list[BusinessBackend] = [primary, *extras]

        routes: dict[str, BusinessBackend] = {}
        for source in self._sources:
            for spec in source.list_tools():
                if spec.name in routes:
                    raise ValueError(
                        f"工具重名：{spec.name!r} 同时由多个来源提供，必须显式改名"
                    )
                routes[spec.name] = source
        self._routes = routes

    def list_tools(self) -> list[ToolSpec]:
        """全部来源的工具声明（按名称排序，输出稳定）。"""
        specs = [spec for source in self._sources for spec in source.list_tools()]
        return sorted(specs, key=lambda spec: spec.name)

    def invoke(self, tool_name: str, args: dict, ctx: ToolContext) -> ToolResult:
        """按名字路由；未知工具返回 ``NOT_FOUND``（不猜测、不透传）。"""
        source = self._routes.get(tool_name)
        if source is None:
            return ToolResult(
                ok=False,
                error_code="NOT_FOUND",
                message=f"未知工具：{tool_name}（未在任何来源注册）",
                trace_id=ctx.trace_id,
            )
        return source.invoke(tool_name, args, ctx)

    def source_of(self, tool_name: str) -> BusinessBackend | None:
        """某工具由哪个来源提供（排障用）。"""
        return self._routes.get(tool_name)
