"""``knowledge_search`` 工具：把 ``Retriever`` 包成 Agent 可调用的标准工具。

**本模块框架无关**（只依赖自研契约），因此可以放在 Core 里被 Registry / Executor 直接使用。

## 安全边界（本文件最重要的一段）

检索结果里可能有受限文档，因此**可见标签只能来自服务端**：

| 参数 | 谁控制 | 说明 |
| --- | --- | --- |
| ``query`` | 模型 | 检索问题，无权限含义 |
| ``doc_types`` | 模型 | 类型白名单（``policy`` / ``fact`` / ``faq``），只能**收窄**，不能用于提权 |
| ``acl_tags`` | **服务端** | 由 ``ToolContext.permissions`` 投影（``kb:internal`` → ``internal``） |
| ``brands`` | **服务端** | 由 ``product_id`` 查身份卡解析；模型自报品牌无效 |
| ``allowed_roles`` | **服务端** | 同上，来自会话身份 |

模型若在参数里塞 ``acl_tags`` / ``allowed_roles``，**一律忽略**并记录到返回值里（``ignored_caller_supplied_filters``），
因为「让模型自报可见性 = 用提示词代替权限校验」。这一条有专门的越权测试。

## 检索不到依据时

``ok=True`` 但 ``chunks`` 为空，并给出 ``note`` 提示「知识库中没有依据」——
**不得**让模型在这种情况下编造答案（术语与引用由系统提示约束）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from zkagent.core.interfaces import Retriever
from zkagent.core.rag.embedder import FALLBACK_MODE
from zkagent.core.rag.errors import RagError
from zkagent.core.rag.metadata import CONSULT_JOURNEYS, project_acl_tags
from zkagent.core.tools.schema import ErrorCode
from zkagent.core.types import ToolContext, ToolResult, ToolSpec

__all__ = ["KNOWLEDGE_TOOL_NAME", "KnowledgeSearchTool"]

#: 归一化标准工具名（DESIGN §7.3 知识类）。
KNOWLEDGE_TOOL_NAME = "knowledge_search"

#: 模型**不允许**自报的参数（出现了就忽略并留痕）。
_SERVER_ONLY_PARAMS = ("acl_tags", "allowed_roles")

_DESCRIPTION = (
    "检索企业知识库，返回带来源的文本片段。适用于政策、流程、时效、费用、注意事项类问题"
    "（如保修期限、退换货条件、安装条件、保养方法、故障排查步骤）。"
    "可按 journey 收窄到某条用户链路：presale(售前) / install(安装) / usage(使用) / "
    "troubleshoot(故障排查) / after_sales(售后政策) / repair_request(报修建单)。"
    "**型号与具体参数（容量/能效/尺寸/价格/库存/是否支持某功能的确切配置）请用 "
    "search_products / get_product，不要用本工具**。"
    "返回的每段都带 source（文档路径），回答时必须引用 source，不得凭记忆作答；"
    "若返回为空，说明知识库中没有依据，必须明确告知而不是编造。"
)


class KnowledgeSearchTool:
    """把检索器包成工具：惰性构建检索器 + 服务端强制可见性。

    :param retriever_factory: 惰性工厂。**只有真正检索时才调用**，
        因此命令启动、注册工具等操作不会加载嵌入模型（否则 `zkagent tools list` 会白等几秒）。
    :param default_top_k: 默认返回条数。
    """

    def __init__(
        self,
        retriever_factory: Callable[[], Retriever],
        *,
        default_top_k: int = 5,
        timeout_s: float = 15.0,
    ) -> None:
        self._factory = retriever_factory
        self._retriever: Retriever | None = None
        self._default_top_k = max(1, default_top_k)
        self._timeout_s = timeout_s

    # ------------------------------------------------------------------ 工具声明

    def list_tools(self) -> list[ToolSpec]:
        """返回工具声明（**不触发**检索器构建）。"""
        return [
            ToolSpec(
                name=KNOWLEDGE_TOOL_NAME,
                description=_DESCRIPTION,
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "要在知识库中检索的问题，尽量保留业务关键词",
                        },
                        "doc_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "可选：只在这些文档类型中检索（policy/fact/faq）；"
                                "playbook 是内部作业手册，默认不可见"
                            ),
                        },
                        "journey": {
                            "type": "string",
                            "enum": list(CONSULT_JOURNEYS),
                            "description": (
                                "可选：只检索某条用户链路。售前=presale、安装=install、"
                                "使用=usage、故障排查=troubleshoot、售后政策=after_sales、"
                                "报修建单=repair_request。不确定时不要传。"
                            ),
                        },
                        "appliance": {
                            "type": "string",
                            "description": "可选：只检索某个品类（如 air_conditioner / washer / rice_cooker）",
                        },
                        "product_id": {
                            "type": "string",
                            "description": "可选：只检索某个型号（需先由商品查询获得 product_id）",
                        },
                    },
                    "required": ["query"],
                },
                risk="low",
                idempotent=True,
                timeout_s=self._timeout_s,
                tags=["kb", "read"],
            )
        ]

    # ------------------------------------------------------------------ 调用

    def invoke(self, tool_name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行检索；失败如实返回错误码，不返回「看起来成功」的空结果。"""
        if tool_name != KNOWLEDGE_TOOL_NAME:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.NOT_FOUND.value,
                message=f"本工具只处理 {KNOWLEDGE_TOOL_NAME}，收到：{tool_name}",
                trace_id=ctx.trace_id,
            )

        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.INVALID_ARGUMENT.value,
                message="query 不能为空",
                trace_id=ctx.trace_id,
            )

        doc_types = [str(item) for item in (args.get("doc_types") or [])]
        journey = str(args.get("journey") or "").strip().lower()
        if journey and journey not in CONSULT_JOURNEYS:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.INVALID_ARGUMENT.value,
                message=(
                    f"journey 取值非法：{journey!r}（可选：{'/'.join(CONSULT_JOURNEYS)}）"
                ),
                trace_id=ctx.trace_id,
            )

        appliance = str(args.get("appliance") or "").strip()
        product_id = str(args.get("product_id") or "").strip()
        ignored = sorted(key for key in _SERVER_ONLY_PARAMS if key in args)

        #: 链路收窄后是否发生过「放开链路」回退（写进返回值，便于排查路由过窄）
        journey_fallback = False
        try:
            retriever = self._ensure()
            # 品牌由服务端按已知型号查身份卡解析：模型可以给型号，但不能自报品牌
            # （品牌政策是品牌级文档，串到兄弟品牌就是答错，见 KNOWN_ISSUES M25）。
            brand = ""
            resolver = getattr(retriever, "brand_of_product", None)
            if product_id and callable(resolver):
                brand = resolver(product_id) or ""
            filters = {
                # 可见性只由服务端决定：模型自报的标签不会被采纳
                "acl_tags": project_acl_tags(ctx.permissions),
                "doc_types": doc_types,
                # 以下三项只是**收窄**检索范围，不含权限语义，因此允许模型指定
                "journeys": [journey] if journey else [],
                "appliances": [appliance] if appliance else [],
                "product_ids": [product_id] if product_id else [],
                "brands": [brand] if brand else [],
            }
            chunks = retriever.search(query, top_k=self._default_top_k, filters=filters)
            if not chunks and filters["journeys"]:
                # 链路收窄把证据滤掉了（评测里有 9 条用例的证据在别的链路）。
                # 宁可放开链路再检一次，也不能因为路由猜错就答「没有依据」。
                relaxed = dict(filters)
                relaxed["journeys"] = []
                chunks = retriever.search(query, top_k=self._default_top_k, filters=relaxed)
                journey_fallback = bool(chunks)
        except RagError as exc:
            # 索引缺失/损坏属于可运维问题：如实报错，让上层决定降级或转人工
            return ToolResult(
                ok=False,
                error_code=ErrorCode.UPSTREAM_ERROR.value,
                message=f"知识检索不可用：{exc}",
                trace_id=ctx.trace_id,
            )

        # 溯源信息（页码 / 依据等级）来自文档元数据，而不是分块本身：
        # 同一篇文档的所有分块共享同一份出处，没必要在每个分块里重复存。
        meta_lookup = getattr(retriever, "doc_meta", None)
        payload_chunks: list[dict[str, Any]] = []
        for chunk in chunks:
            meta = meta_lookup(chunk.source) if callable(meta_lookup) else None
            item: dict[str, Any] = {
                "source": chunk.source,
                "node_id": chunk.node_id,
                "score": round(chunk.score, 6),
                "doc_type": chunk.metadata.get("doc_type"),
                "journey": chunk.metadata.get("journey"),
                "text": chunk.text,
            }
            if meta is not None:
                item.update(
                    {
                        "appliance": meta.appliance,
                        "product_id": meta.product_id,
                        "origin": meta.source,
                        "source_page": meta.source_page,
                        "verified": meta.verified,
                    }
                )
            payload_chunks.append(item)

        degraded = getattr(retriever, "embedding_mode", None) == FALLBACK_MODE
        payload: dict[str, Any] = {
            "query": query,
            "count": len(chunks),
            "journey": journey or None,
            #: true = 按 journey 检不到、已放开链路重新检索（路由可能过窄）
            "journey_fallback": journey_fallback,
            "embedding_mode": getattr(retriever, "embedding_mode", None),
            "degraded": degraded,
            "verified": not degraded
            and not any(item.get("verified") is False for item in payload_chunks),
            "chunks": payload_chunks,
        }
        if degraded:
            # hash 降级只保证「字面接近」，不能声称依据已核实
            payload["note"] = (
                "本次未使用本地嵌入模型（hash 降级），依据未核实；回答时必须明确说明这一点"
            )
        elif any(item.get("verified") is False for item in payload_chunks):
            payload["note"] = "本次结果含**未经验证**的依据（verified=false），回答时必须注明"
        if ignored:
            # 留痕：模型尝试自报可见性参数（不得生效，但必须可审计）
            payload["ignored_caller_supplied_filters"] = ignored
        if not chunks:
            payload["note"] = (
                "知识库中未检索到依据（不得凭猜测作答）"
                + ("；且当前为 hash 降级模式，依据未核实" if degraded else "")
            )

        return ToolResult(ok=True, data=payload, trace_id=ctx.trace_id)

    # ------------------------------------------------------------------ 内部

    def _ensure(self) -> Retriever:
        """惰性构建检索器（首次调用才加载模型 / 读取索引）。"""
        if self._retriever is None:
            self._retriever = self._factory()
        return self._retriever
