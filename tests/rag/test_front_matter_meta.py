"""Markdown front-matter 元数据：第二元数据来源与「品类 / 型号」过滤。

外部知识库（``appliance-kb``）把元数据写在分片的 front-matter 里，若要求它再复制一份
``meta.yaml``，就是两份真相。这里固化三件事：

1. front-matter 会被解析，且**正文不含 front-matter**（否则元数据词会污染嵌入与 BM25）；
2. 优先级是 ``meta.yaml`` > front-matter > defaults；``journey`` 目录优先于 ``scenario``；
3. 品类 / 型号过滤只能收窄：**无归属的通用文档始终保留**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zkagent.core.rag.metadata import (
    DocMeta,
    KnowledgeMetaError,
    filter_visible,
    journey_of_scenario,
    load_knowledge_meta,
    parse_front_matter,
)

FRONT = """---
chunk_id: m1@s1#cooling
doc_type: fact
scenario: s1_presale
appliance: air_conditioner
product_id: brand-m1
source: 03_product/m1/specs.yaml#cooling（说明书 p12）
source_page: 12
verified: false
---

M1 的额定制冷量为 3500W。
"""


# --------------------------------------------------------------------- 解析


def test_parse_front_matter_returns_meta_and_body() -> None:
    """验证：front-matter 被解析成元数据，且**正文不含**元数据块。

    元数据词若留在正文里，会污染嵌入与 BM25（「chunk_id」也会被当成内容词）。
    """
    meta, body = parse_front_matter(FRONT)

    assert meta["chunk_id"] == "m1@s1#cooling"
    assert meta["source_page"] == 12
    assert "3500W" in body
    assert "chunk_id" not in body, "正文不得含元数据，否则会稀释真实内容"
    assert not body.startswith("---")


def test_parse_front_matter_without_block_returns_original() -> None:
    """验证：没有 front-matter 的文档原样返回——普通 Markdown 不受影响。"""
    meta, body = parse_front_matter("普通正文")

    assert meta == {}
    assert body == "普通正文"


def test_parse_front_matter_missing_delimiter_fails_loudly() -> None:
    """验证：缺少结束分隔符时报错——半截 front-matter 会把正文整段吞成元数据。"""
    with pytest.raises(KnowledgeMetaError, match="结束分隔符"):
        parse_front_matter("---\na: 1\n正文没有结束分隔符")


def test_parse_front_matter_invalid_yaml_fails_loudly() -> None:
    """验证：front-matter 里 YAML 非法时报错，不静默跳过（否则元数据静默丢失）。"""
    with pytest.raises(KnowledgeMetaError, match="YAML"):
        parse_front_matter("---\na: [1,\n---\n正文")


def test_journey_of_scenario() -> None:
    """验证：``scenario`` 能映射到链路（含 ``s6_service → repair_request`` 的改名），
    无法识别时返回 ``None``（作为跨链路通用文档，不参与链路收窄）。
    """
    assert journey_of_scenario("s4_troubleshoot") == "troubleshoot"
    assert journey_of_scenario("s6_service") == "repair_request"
    assert journey_of_scenario("s_adversarial") is None
    assert journey_of_scenario(None) is None


# --------------------------------------------------------------------- 加载与优先级


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_load_reads_front_matter_without_meta_yaml(tmp_path: Path) -> None:
    """验证：没有 ``meta.yaml`` 时，front-matter 就是元数据来源（含 source/source_page 溯源）。

    外部知识库只写 front-matter，不强求再复制一份 meta.yaml，避免两份真相。
    """
    _write(tmp_path, "fact/m1.md", FRONT)

    metas = load_knowledge_meta(tmp_path, known_doc_ids=["fact/m1.md"])
    meta = metas["fact/m1.md"]

    assert meta.doc_type == "fact"
    assert meta.journey == "presale", "目录推不出链路时用 scenario"
    assert meta.appliance == "air_conditioner"
    assert meta.product_id == "brand-m1"
    assert meta.source_page == 12
    assert meta.verified is False
    assert "p12" in (meta.source or "")


def test_single_scope_models_projects_to_product_id(tmp_path: Path) -> None:
    """M22：单型号 FAQ 的 ``scope_models`` 必须投影成 ``product_id``。

    不投影的话，这类「只适用于某型号」的分片不受型号过滤约束，会在别的型号的查询里
    排到前面（实测 top-5 里 20% 是别型号的 FAQ）。
    """
    _write(
        tmp_path,
        "presale/faq/f.md",
        "---\ndoc_type: faq\nscenario: s1_presale\nproduct_id: null\n"
        "model: m1\nscope_models:\n- m1\n---\n正文\n",
    )

    meta = load_knowledge_meta(tmp_path, known_doc_ids=["presale/faq/f.md"])["presale/faq/f.md"]

    assert meta.product_id == "m1"


def test_multi_scope_models_is_not_narrowed(tmp_path: Path) -> None:
    """多型号 ``scope_models`` 表达不了（``product_id`` 是单值）时留空：宁可不过滤，也不错误收窄。"""
    _write(
        tmp_path,
        "presale/faq/f.md",
        "---\ndoc_type: faq\nscenario: s1_presale\nscope_models:\n- m1\n- m2\n---\n正文\n",
    )

    meta = load_knowledge_meta(tmp_path, known_doc_ids=["presale/faq/f.md"])["presale/faq/f.md"]

    assert meta.product_id is None


def test_meta_yaml_wins_over_front_matter(tmp_path: Path) -> None:
    """验证：优先级 ``meta.yaml`` > front-matter——可见性由显式配置决定，溯源仍取自文档自身。"""
    _write(tmp_path, "fact/m1.md", FRONT)
    _write(
        tmp_path,
        "meta.yaml",
        "version: 1\n"
        "docs:\n"
        "  - doc_id: fact/m1.md\n"
        "    doc_type: policy\n"
        "    acl_tags: [internal]\n"
        "    roles: [agent]\n",
    )

    meta = load_knowledge_meta(tmp_path, known_doc_ids=["fact/m1.md"])["fact/m1.md"]

    assert meta.doc_type == "policy", "meta.yaml 是显式配置，覆盖文档自述"
    assert meta.acl_tags == ["internal"]
    # 可见性由 meta.yaml 决定，但溯源信息仍来自文档自身
    assert meta.verified is False
    assert meta.product_id == "brand-m1"


def test_directory_beats_scenario_for_journey(tmp_path: Path) -> None:
    """目录能推出链路时，以目录为准（路径是本项目的既定事实来源）。"""
    _write(
        tmp_path,
        "install/m1.md",
        "---\ndoc_type: manual\nscenario: s1_presale\n---\n正文",
    )

    meta = load_knowledge_meta(tmp_path, known_doc_ids=["install/m1.md"])["install/m1.md"]

    assert meta.journey == "install"


# --------------------------------------------------------------------- 过滤


METAS = {
    "fact/m1.md": DocMeta(
        doc_id="fact/m1.md", doc_type="fact", appliance="air_conditioner", product_id="m1"
    ),
    "fact/m2.md": DocMeta(
        doc_id="fact/m2.md", doc_type="fact", appliance="washer", product_id="m2"
    ),
    "faq/all.md": DocMeta(doc_id="faq/all.md", doc_type="faq"),
    "policy/secret.md": DocMeta(
        doc_id="policy/secret.md", doc_type="policy", acl_tags=["internal"]
    ),
}


def test_appliance_filter_keeps_generic_docs() -> None:
    """验证：按品类收窄时**无归属的通用文档始终保留**——过滤只能收窄，不能误删通用品类知识。"""
    visible = filter_visible(METAS, caller_tags=[], appliances=["air_conditioner"])

    assert visible == {"fact/m1.md", "faq/all.md"}, "通用文档不因品类收窄而消失"


def test_product_filter_keeps_generic_docs() -> None:
    """验证：按型号收窄同样保留通用文档（与上一条同理，换成 product_ids 维度）。"""
    visible = filter_visible(METAS, caller_tags=[], product_ids=["m2"])

    assert visible == {"fact/m2.md", "faq/all.md"}


def test_brand_filter_excludes_other_brands_only() -> None:
    """品牌过滤只排除「明确属于其他品牌」的文档；无品牌归属的通用文档保留（M25）。

    品牌政策是品牌级文档（``product_id`` 为空），没有这一维就会串到兄弟品牌：
    美的产品问保修，排第一的是海尔质保。
    """
    metas = {
        "policy/midea.md": DocMeta(doc_id="policy/midea.md", doc_type="policy", brand="midea"),
        "policy/haier.md": DocMeta(doc_id="policy/haier.md", doc_type="policy", brand="haier"),
        "faq/all.md": DocMeta(doc_id="faq/all.md", doc_type="faq"),
        "faq/both.md": DocMeta(doc_id="faq/both.md", doc_type="faq", brands=["midea", "haier"]),
    }

    visible = filter_visible(metas, caller_tags=[], brands=["midea"])

    assert visible == {"policy/midea.md", "faq/all.md", "faq/both.md"}


def test_brands_read_from_front_matter(tmp_path: Path) -> None:
    """多品牌 FAQ 用 ``brands`` 列表表达：一条 FAQ 覆盖多品牌，不拆条、不复制。"""
    _write(
        tmp_path,
        "after_sales/faq/f.md",
        "---\ndoc_type: faq\nscenario: s5_aftersale\nbrands: [haier, midea]\n---\n正文\n",
    )

    meta = load_knowledge_meta(tmp_path, known_doc_ids=["after_sales/faq/f.md"])[
        "after_sales/faq/f.md"
    ]

    assert meta.brands == ["haier", "midea"]
    assert meta.brand is None


def test_filters_cannot_grant_access() -> None:
    """过滤只能收窄：指定品类也拿不到受限文档。"""
    visible = filter_visible(
        METAS, caller_tags=[], appliances=["air_conditioner"], doc_types=["policy"]
    )

    assert visible == set()

    allowed = filter_visible(METAS, caller_tags=["internal"], doc_types=["policy"])
    assert allowed == {"policy/secret.md"}
