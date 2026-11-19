"""用户链路（``journey``）：目录即事实来源 + 检索可按链路收窄。

两类断言：

1. **行业包规约**（对**每个**行业包生效，含生成包 ``homeapp``）：每篇文档必须落在六条链路
   目录内（跨链路通用的型号身份卡放 ``_shared/``）；每条对外链路至少 2 篇文档、至少 1 条
   评测用例（默认包 ``appliance`` 要求 2 条）；评测集的 gold 必须真实存在。
   没有这些断言，「按用户链路组织知识」就只是口头承诺——新增文档随手丢在根目录也不会有人发现。
2. **运行期行为**：``journeys`` 过滤只能**收窄**检索范围，不能越权；
   未归位文档不进入任何链路（避免「没归位的文档混进某条链路」）。

## 为什么 journey 由目录推导而不是写在 meta.yaml

路径与元数据各写一份，迟早会不一致（文件在 ``install/``、声明写着 ``usage``）。
这里把「目录」定为唯一事实来源，``meta.yaml`` 只负责 ``doc_type`` 与可见性。
"""

from __future__ import annotations

from collections import Counter

from conftest import write_knowledge

from zkagent.config import pack_file
from zkagent.core.rag.embedder import HashEmbedder
from zkagent.core.rag.knowledge_tool import KNOWLEDGE_TOOL_NAME, KnowledgeSearchTool
from zkagent.core.rag.llamaindex_engine import LlamaIndexRetriever
from zkagent.core.rag.manifest import scan_documents
from zkagent.core.rag.metadata import (
    CONSULT_JOURNEYS,
    DocMeta,
    filter_visible,
    journey_of_doc_id,
    load_knowledge_meta,
)
from zkagent.core.rag.metrics import load_eval_set
from zkagent.core.types import ToolContext

#: 两条链路 + 一篇未归位文档 + 一篇受限文档，用于验证过滤只收窄、不越权。
DOCS: dict[str, str] = {
    "install/site.md": "空调安装需要独立回路与可靠接地，外机位要留检修空间。",
    "usage/filter.md": "空调滤网建议每两周清洗一次，拆下后用清水冲洗阴干。",
    "loose.md": "通用声明：随机附带的纸质说明书与电子版具有同等效力。",
    "internal/limits.md": "内部口径：超出额度的退款必须转人工审核。",
}

META = """version: 1
docs:
  - doc_id: install/site.md
    doc_type: manual
  - doc_id: usage/filter.md
    doc_type: manual
  - doc_id: loose.md
    doc_type: faq
  - doc_id: internal/limits.md
    doc_type: internal
    acl_tags: [internal]
    roles: [agent]
"""


def _metas(pack_dir):
    knowledge = pack_dir / "knowledge"
    docs = scan_documents(knowledge)
    return docs, load_knowledge_meta(knowledge, known_doc_ids=sorted(docs))


# --------------------------------------------------------------------- 行业包规约


def test_every_doc_falls_into_a_journey(any_pack_dir) -> None:
    """每篇文档都必须归位：链路目录，或显式声明为跨链路的 ``_shared/``。

    未归位文档在链路过滤下会「怎么都检索不到」，所以不允许存在；
    但型号身份卡这类**每个链路都要用**的分片不能硬塞进某一条链路，
    放在 ``_shared/`` 并限定为 ``identity`` 类型——这是唯一的豁免。
    """
    _docs, metas = _metas(any_pack_dir)

    unclassified = sorted(
        doc_id
        for doc_id, meta in metas.items()
        if not meta.journey and not doc_id.startswith("_shared/")
    )
    assert unclassified == [], f"以下文档未归入任何用户链路目录：{unclassified[:5]}"

    stray = sorted(
        doc_id
        for doc_id, meta in metas.items()
        if doc_id.startswith("_shared/") and meta.doc_type != "identity"
    )
    assert stray == [], f"_shared/ 只允许放跨链路的型号身份卡，以下分片不合法：{stray[:5]}"


def test_each_consult_journey_has_enough_docs(any_pack_dir) -> None:
    """六条对外链路各至少 2 篇：一条链路只有 1 篇，等于「只有一条路可走」。"""
    _docs, metas = _metas(any_pack_dir)
    counts = Counter(meta.journey for meta in metas.values())

    thin = {
        journey: counts.get(journey, 0)
        for journey in CONSULT_JOURNEYS
        if counts.get(journey, 0) < 2
    }
    assert thin == {}, f"以下链路的文档数不足 2 篇：{thin}"


#: 每条链路的最少评测用例数。自写题目（``appliance``）要求 2 条；
#: 生成包的题目来自上游 KB，**不得伪造**，只要求「有」，缺口记入 KNOWN_ISSUES（M14）。
MIN_EVAL_CASES = {"appliance": 2}
DEFAULT_MIN_EVAL_CASES = 1


def test_eval_set_covers_every_journey(any_pack_dir) -> None:
    """评测集必须为每条链路标注 journey，且每条链路至少有可度量的用例。"""
    eval_set = load_eval_set(pack_file(any_pack_dir, "evals/retrieval.yaml"))

    unlabeled = [case.id for case in eval_set.queries if not case.journey]
    assert unlabeled == [], f"以下用例未标注 journey：{unlabeled}"

    minimum = MIN_EVAL_CASES.get(any_pack_dir.name, DEFAULT_MIN_EVAL_CASES)
    counts = Counter(case.journey for case in eval_set.queries)
    missing = {
        journey: counts.get(journey, 0)
        for journey in CONSULT_JOURNEYS
        if counts.get(journey, 0) < minimum
    }
    assert missing == {}, f"以下链路的评测用例少于 {minimum} 条：{missing}"


def test_eval_gold_docs_exist(any_pack_dir) -> None:
    """gold 写错路径不会报错、只会让指标悄悄变差——必须在测试里挡住。"""
    docs, _metas_map = _metas(any_pack_dir)
    eval_set = load_eval_set(pack_file(any_pack_dir, "evals/retrieval.yaml"))

    missing = sorted(
        {hit.doc_id for case in eval_set.queries for hit in case.gold} - set(docs)
    )
    assert missing == [], f"评测集 gold 引用了不存在的文档：{missing}"


# --------------------------------------------------------------------- 推导与过滤


def test_journey_inference_from_path() -> None:
    """验证：链路由**首段目录**推导；根目录与未知目录返回 ``None``，不猜链路。"""
    assert journey_of_doc_id("after_sales/warranty/warranty_policy.md") == "after_sales"
    assert journey_of_doc_id("repair_request/intake.md") == "repair_request"
    assert journey_of_doc_id("internal/limits.md") == "internal"
    assert journey_of_doc_id("loose.md") is None, "根目录文件不属于任何链路"
    assert journey_of_doc_id("unknown/doc.md") is None, "未知目录不得被猜成某条链路"


def test_filter_visible_by_journey_narrows_only() -> None:
    """验证：链路过滤只做收窄——通用（未归位）文档始终可见。"""
    metas = {
        "install/a.md": DocMeta(doc_id="install/a.md", journey="install"),
        "usage/b.md": DocMeta(doc_id="usage/b.md", journey="usage"),
        "loose.md": DocMeta(doc_id="loose.md"),
        "internal/c.md": DocMeta(
            doc_id="internal/c.md", journey="internal", acl_tags=["internal"]
        ),
    }

    # 未归位 = 跨链路通用：设了链路也保留，否则「型号身份卡」这类文档会在路由后消失
    assert filter_visible(metas, caller_tags=[], journeys=["install"]) == {
        "install/a.md",
        "loose.md",
    }
    assert filter_visible(metas, caller_tags=[], journeys=[]) == {
        "install/a.md",
        "usage/b.md",
        "loose.md",
    }

    # 链路过滤**不能**提权：无 internal 标签时，指定 internal 链路也拿不到受限文档
    # （通用文档 loose.md 仍会保留——它不是「被授权」，而是本来就不属于任何链路）
    internal_only = filter_visible(metas, caller_tags=[], journeys=["internal"])
    assert "internal/c.md" not in internal_only
    assert internal_only == {"loose.md"}
    assert filter_visible(metas, caller_tags=["internal"], journeys=["internal"]) == {
        "internal/c.md",
        "loose.md",
    }


# --------------------------------------------------------------------- 运行期：引擎与工具


def _retriever(tmp_path) -> LlamaIndexRetriever:
    knowledge = write_knowledge(tmp_path, DOCS, META)
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
    return retriever


def test_engine_search_narrows_by_journey(tmp_path) -> None:
    """验证：运行期按链路收窄生效，通用文档仍保留，``stats()`` 把未归位单独计为 ``unclassified``。

    未归位单独计数，是为了防止「没归位的文档被悄悄算进某条链路」从而虚高链路覆盖。
    """
    retriever = _retriever(tmp_path)

    usage_hits = retriever.search(
        "滤网 清洗 空调", top_k=5, filters={"journeys": ["usage"]}, mode="sparse"
    )
    assert usage_hits, "usage 链路内应有命中"
    assert all(hit.source.startswith("usage/") for hit in usage_hits)

    install_hits = retriever.search(
        "独立回路 接地 空调", top_k=5, filters={"journeys": ["install"]}, mode="sparse"
    )
    assert install_hits
    assert all(hit.source.startswith("install/") for hit in install_hits)

    # 通用文档（未归位）不受链路过滤影响：用只出现在它身上的词验证
    generic_hits = retriever.search(
        "纸质说明书 效力", top_k=5, filters={"journeys": ["usage"]}, mode="sparse"
    )
    assert any(hit.source == "loose.md" for hit in generic_hits)

    # 概览里的链路分布：通用文档单独计数，不会被悄悄算进某条链路
    assert retriever.stats()["journeys"] == {
        "install": 1,
        "internal": 1,
        "unclassified": 1,
        "usage": 1,
    }


def test_knowledge_tool_passes_journey_filter(tmp_path) -> None:
    """验证：工具侧 ``journey`` 参数被透传到检索，且每个片段都带 ``journey`` 便于上层引用。"""
    retriever = _retriever(tmp_path)
    tool = KnowledgeSearchTool(lambda: retriever, default_top_k=5)
    ctx = ToolContext(trace_id="t1", session_id="s1", user_id="U1")

    result = tool.invoke(KNOWLEDGE_TOOL_NAME, {"query": "空调滤网清洗", "journey": "usage"}, ctx)

    assert result.ok is True
    assert result.data["journey"] == "usage"
    assert result.data["chunks"], "usage 链路内应有可引用片段"
    assert all(chunk["source"].startswith("usage/") for chunk in result.data["chunks"])
    assert all(chunk["journey"] == "usage" for chunk in result.data["chunks"])


def test_knowledge_tool_rejects_unknown_journey(tmp_path) -> None:
    """非法链路显式报错（而不是静默忽略），模型才有机会纠正参数。"""
    tool = KnowledgeSearchTool(lambda: _retriever(tmp_path), default_top_k=3)
    ctx = ToolContext(trace_id="t1", session_id="s1", user_id="U1")

    result = tool.invoke(KNOWLEDGE_TOOL_NAME, {"query": "保修", "journey": "marketing"}, ctx)

    assert result.ok is False
    assert result.error_code == "INVALID_ARGUMENT"
    assert "presale" in result.message
