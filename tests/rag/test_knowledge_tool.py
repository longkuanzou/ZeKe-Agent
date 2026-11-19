"""``knowledge_search`` 工具与组合后端（对应 DESIGN §6.3.4 与 P3 Gate 的安全判定）。

最要紧的两条：

1. **模型自报的 ``acl_tags`` / ``allowed_roles`` 一律不生效**——可见性只能来自服务端
   ``ToolContext.permissions``；否则「让模型自报可见性」就等于用提示词代替权限校验；
2. 检索不到内容时返回 ``ok=True`` + ``count=0`` + ``note``，让上层必须如实回答「没有依据」，
   **不得**用空结果伪造成「有依据」。
"""

from __future__ import annotations

import pytest

from zkagent.core.rag.embedder import HashEmbedder
from zkagent.core.rag.knowledge_tool import KNOWLEDGE_TOOL_NAME, KnowledgeSearchTool
from zkagent.core.rag.llamaindex_engine import LlamaIndexRetriever
from zkagent.core.tools.composite import CompositeBackend
from zkagent.core.types import Chunk, ToolContext
from conftest import write_knowledge


@pytest.fixture()
def tool(tmp_path):
    """构造一个已建索引的知识检索工具（hash 嵌入，离线）。"""
    knowledge = write_knowledge(tmp_path)
    retriever = LlamaIndexRetriever(
        industry_id="appliance",
        knowledge_dir=knowledge,
        index_dir=tmp_path / "indexes" / "appliance",
        embedder=HashEmbedder(64),
        embedding_mode="fallback_hash",
        chunk_size=100,
        chunk_overlap=10,
    )
    retriever.index()
    return KnowledgeSearchTool(lambda: retriever, default_top_k=3)


def _ctx(permissions: list[str] | None = None) -> ToolContext:
    return ToolContext(
        trace_id="t1", session_id="s1", user_id="U1", permissions=permissions or []
    )


# --------------------------------------------------------------------------- 工具声明


def test_tool_spec_is_declared_without_building_retriever() -> None:
    """声明工具**不得**触发检索器构建（否则每个命令都要等模型加载）。"""
    built = {"count": 0}

    def factory():
        built["count"] += 1
        raise AssertionError("声明工具时不应构建检索器")

    specs = KnowledgeSearchTool(factory).list_tools()

    assert [spec.name for spec in specs] == [KNOWLEDGE_TOOL_NAME]
    assert specs[0].risk == "low" and specs[0].idempotent is True
    assert "query" in specs[0].parameters["properties"]
    assert specs[0].parameters["required"] == ["query"]
    assert built["count"] == 0


# --------------------------------------------------------------------------- 正常检索


def test_search_returns_citable_chunks(tool) -> None:
    """验证：检索结果带 ``source`` / ``node_id`` 可引用，并把嵌入模式一并上报。"""
    result = tool.invoke(KNOWLEDGE_TOOL_NAME, {"query": "未发货订单只能取消"}, _ctx())

    assert result.ok is True
    assert result.data["count"] >= 1
    assert result.data["embedding_mode"] == "fallback_hash", "降级口径必须随结果上报"
    first = result.data["chunks"][0]
    assert first["source"] == "returns.md"
    assert first["node_id"].startswith("returns.md:")
    assert "text" in first


def test_empty_query_is_rejected(tool) -> None:
    """验证：空白 query 被拒——空查询检索出来的东西毫无意义，还会浪费一轮。"""
    result = tool.invoke(KNOWLEDGE_TOOL_NAME, {"query": "   "}, _ctx())

    assert result.ok is False
    assert result.error_code == "INVALID_ARGUMENT"


def test_unknown_tool_name_is_rejected(tool) -> None:
    """验证：调用非本工具名返回 ``NOT_FOUND``——知识工具只认 ``knowledge_search``。"""
    result = tool.invoke("get_order", {"query": "x"}, _ctx())

    assert result.ok is False
    assert result.error_code == "NOT_FOUND"


def test_missing_index_reports_upstream_error(tmp_path) -> None:
    """索引没建时如实报错，**不得**返回空结果冒充「知识库没有依据」。"""
    knowledge = write_knowledge(tmp_path)
    retriever = LlamaIndexRetriever(
        industry_id="appliance",
        knowledge_dir=knowledge,
        index_dir=tmp_path / "missing-index",
        embedder=HashEmbedder(32),
    )
    result = KnowledgeSearchTool(lambda: retriever).invoke(
        KNOWLEDGE_TOOL_NAME, {"query": "保修"}, _ctx()
    )

    assert result.ok is False
    assert result.error_code == "UPSTREAM_ERROR"
    assert "索引不存在" in result.message


# --------------------------------------------------------------------------- 可见性（安全）


def test_restricted_chunk_is_not_returned_without_server_permission(tool) -> None:
    """验证：无服务端权限时受限文档不出现在结果里（默认最小可见集）。"""
    result = tool.invoke(KNOWLEDGE_TOOL_NAME, {"query": "退款 人工审核 额度"}, _ctx())

    assert all(chunk["source"] != "internal/limits.md" for chunk in result.data["chunks"])


def test_model_supplied_acl_tags_are_ignored(tool) -> None:
    """**核心安全用例**：模型在参数里自报 ``acl_tags`` 不得提权，但必须留痕。"""
    result = tool.invoke(
        KNOWLEDGE_TOOL_NAME,
        {"query": "退款 人工审核 额度", "acl_tags": ["internal"], "allowed_roles": ["agent"]},
        _ctx(),  # 服务端权限为空
    )

    assert all(chunk["source"] != "internal/limits.md" for chunk in result.data["chunks"])
    assert result.data["ignored_caller_supplied_filters"] == ["acl_tags", "allowed_roles"]


def test_server_permission_reveals_restricted_chunk(tool) -> None:
    """真正的授权路径：服务端会话持有 ``kb:internal`` 权限。"""
    result = tool.invoke(
        KNOWLEDGE_TOOL_NAME, {"query": "退款 人工审核 额度"}, _ctx(permissions=["kb:internal"])
    )

    assert any(chunk["source"] == "internal/limits.md" for chunk in result.data["chunks"])


def test_doc_types_filter_is_honoured(tool) -> None:
    """验证：``doc_types`` 是**允许**的收窄维度（不像 acl_tags 那样被忽略）——按类型过滤生效。"""
    result = tool.invoke(
        KNOWLEDGE_TOOL_NAME, {"query": "保修 退货 审核", "doc_types": ["policy"]}, _ctx()
    )

    assert all(chunk["doc_type"] == "policy" for chunk in result.data["chunks"])


# --------------------------------------------------------------------------- 无依据


def test_no_basis_returns_note_instead_of_fabricating(tmp_path) -> None:
    """检索不到时必须显式标注「没有依据」，让上层无法把空结果说成有依据。

    **本例验证的是机制**：显式设置较高的相关性闸门（真实模型口径 0.45），
    不相关查询会被闸门拦掉 → ``count=0`` + ``note``。
    默认的 hash 兜底模式**关闭**了闸门（hash 相似度不是相关性信号，见
    ``docs/KNOWN_ISSUES.md``），因此离线用例无法替代真实模型下的验收——
    真实模型下的「无依据」由 `bash tests/gates/run_p3.sh` 的 `nobasis` 单点检查覆盖。
    """
    knowledge = write_knowledge(tmp_path)
    retriever = LlamaIndexRetriever(
        industry_id="appliance",
        knowledge_dir=knowledge,
        index_dir=tmp_path / "indexes" / "appliance",
        embedder=HashEmbedder(64),
        embedding_mode="local_model",  # 声明为真实模型口径 → 闸门生效
        chunk_size=100,
        chunk_overlap=10,
        dense_min_similarity=0.45,
    )
    retriever.index()
    tool = KnowledgeSearchTool(lambda: retriever, default_top_k=3)

    result = tool.invoke(KNOWLEDGE_TOOL_NAME, {"query": "量子纠缠 引力波 中微子振荡"}, _ctx())

    assert result.ok is True
    assert result.data["count"] == 0
    assert result.data["chunks"] == []
    assert "未检索到依据" in result.data["note"]


# --------------------------------------------------------------------------- 组合后端


class _StubBackend:
    """最小业务后端桩：只声明一个工具。"""

    backend_id = "stub"

    def __init__(self, name: str = "get_order") -> None:
        self._name = name

    def list_tools(self):
        from zkagent.core.types import ToolSpec

        return [ToolSpec(name=self._name, description="桩工具", tags=["order"])]

    def invoke(self, tool_name, args, ctx):
        from zkagent.core.types import ToolResult

        return ToolResult(ok=True, data={"backend": "stub", "tool": tool_name})


def test_composite_routes_by_tool_name(tool) -> None:
    """验证：组合后端按工具名路由——业务工具走业务后端，``knowledge_search`` 走 RAG。"""
    backend = CompositeBackend(_StubBackend(), [tool])

    names = [spec.name for spec in backend.list_tools()]
    assert names == ["get_order", KNOWLEDGE_TOOL_NAME]

    business = backend.invoke("get_order", {}, _ctx())
    knowledge = backend.invoke(KNOWLEDGE_TOOL_NAME, {"query": "保修一年"}, _ctx())

    assert business.data["backend"] == "stub"
    assert knowledge.ok is True and knowledge.data["count"] >= 1
    assert backend.source_of(KNOWLEDGE_TOOL_NAME) is tool


def test_composite_rejects_duplicate_tool_names(tool) -> None:
    """重名工具必须报错，不能让后注册的悄悄顶掉前一个。"""
    clash = _StubBackend(KNOWLEDGE_TOOL_NAME)

    with pytest.raises(ValueError, match="工具重名"):
        CompositeBackend(clash, [tool])


def test_composite_reports_unknown_tool(tool) -> None:
    """验证：两个后端都没有的工具返回 ``NOT_FOUND``，而不是抛异常中断对话。"""
    backend = CompositeBackend(_StubBackend(), [tool])

    result = backend.invoke("no_such_tool", {}, _ctx())

    assert result.ok is False and result.error_code == "NOT_FOUND"


# --------------------------------------------------------------------------- 品牌与链路回退


class _StubRetriever:
    """最小检索器：记录每次的 filters，按「是否收窄了链路」决定返回空还是有结果。"""

    embedding_mode = "local_model"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def brand_of_product(self, product_id: str) -> str | None:
        return "midea" if product_id == "midea-kfr-35gw-q2" else None

    def search(self, query, top_k=None, filters=None):
        self.calls.append(dict(filters or {}))
        if (filters or {}).get("journeys"):
            return []
        return [
            Chunk(
                text="整机包修 6 年",
                source="after_sales/policy/x.md",
                node_id="after_sales/policy/x.md:0",
                score=0.9,
                metadata={"doc_type": "policy", "journey": "after_sales"},
            )
        ]


def test_brand_is_resolved_from_product_id() -> None:
    """品牌由服务端按型号查身份卡解析，模型只能给型号（M25）。"""
    retriever = _StubRetriever()
    tool = KnowledgeSearchTool(lambda: retriever, default_top_k=3)

    tool.invoke(
        KNOWLEDGE_TOOL_NAME, {"query": "保修几年", "product_id": "midea-kfr-35gw-q2"}, _ctx()
    )

    assert retriever.calls[0]["brands"] == ["midea"]


def test_journey_filter_falls_back_when_empty() -> None:
    """按链路检不到时放开链路再检一次，并上报 ``journey_fallback``（路由可能过窄）。"""
    retriever = _StubRetriever()
    tool = KnowledgeSearchTool(lambda: retriever, default_top_k=3)

    result = tool.invoke(KNOWLEDGE_TOOL_NAME, {"query": "保修几年", "journey": "install"}, _ctx())

    assert [call["journeys"] for call in retriever.calls] == [["install"], []]
    assert result.data["journey_fallback"] is True
    assert result.data["count"] == 1
