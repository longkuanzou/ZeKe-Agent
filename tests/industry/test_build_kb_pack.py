"""打包器：把分层知识库搬成行业包（搬运 + 派生，不改写）。

核心断言：

1. **原样搬运**：分片连同 front-matter 一起复制，不重写内容（元数据是人家的事实来源）；
2. **派生正确**：评测集的 gold 由「出处锚点 + 型号」反查，找不到就跳过**而不是猜**；
3. **可追溯**：生成目录带 ``.generated.json`` 标记，非生成目录必须 ``--force`` 才覆盖；
4. **真实包一致**：``zkagent/industry_packs/homeapp`` 存在时，检查「除身份卡外每篇都归位」
   与「gold 必须存在」——这两条错了，检索指标就是假的。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest
import yaml

from apps.build_kb_pack import MARKER_FILENAME, PackBuildError, _build_eval, build_pack
from zkagent.config import default_industry_packs_dir, pack_file
from zkagent.core.rag.manifest import scan_documents
from zkagent.core.rag.metadata import CONSULT_JOURNEYS, exclusion_reason, load_knowledge_meta
from zkagent.core.rag.metrics import load_eval_set

CHUNK = """---
chunk_id: brand-m1@s1#cooling
doc_type: fact
scenario: s1_presale
appliance: air_conditioner
product_id: brand-m1
source: 03_product/air_conditioner/brand/M1/specs.yaml#cooling（说明书 p30）
source_page: 30
verified: true
---

M1 的额定制冷量为 3500W。
"""

IDENTITY = """---
chunk_id: brand-m1@id#001
doc_type: identity
scenario: null
product_id: brand-m1
appliance: air_conditioner
---

品牌 M1（壁挂机）。
"""


def _make_kb(root: Path) -> Path:
    """造一个最小知识库：1 个型号、2 个分片、1 条评测题。"""
    (root / "08_index" / "chunks" / "fact").mkdir(parents=True)
    (root / "08_index" / "chunks" / "identity").mkdir(parents=True)
    (root / "08_index" / "chunks" / "fact" / "brand-m1@s1#cooling.md").write_text(
        CHUNK, encoding="utf-8"
    )
    (root / "08_index" / "chunks" / "identity" / "brand-m1@id#001.md").write_text(
        IDENTITY, encoding="utf-8"
    )
    manifest = [
        {
            "chunk_id": "brand-m1@s1#cooling",
            "doc_type": "fact",
            "path": "08_index/chunks/fact/brand-m1@s1#cooling.md",
            "source": "03_product/air_conditioner/brand/M1/specs.yaml#cooling（说明书 p30）",
        },
        {
            "chunk_id": "brand-m1@id#001",
            "doc_type": "identity",
            "path": "08_index/chunks/identity/brand-m1@id#001.md",
        },
    ]
    (root / "08_index" / "manifest.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in manifest) + "\n",
        encoding="utf-8",
    )

    product_dir = root / "03_product" / "air_conditioner" / "brand" / "M1"
    product_dir.mkdir(parents=True)
    (product_dir / "product.yaml").write_text(
        "product_id: brand-m1\n"
        "status: ready\n"
        "appliance: air_conditioner\n"
        "appliance_cn: 空调\n"
        "brand: brand\n"
        "brand_cn: 品牌\n"
        "model: KFR-35GW/M1\n"
        "model_slug: KFR-35GW-M1\n",
        encoding="utf-8",
    )

    shared = root / "04_scenario" / "_shared"
    shared.mkdir(parents=True)
    (shared / "intent_map.yaml").write_text(
        "version: 1\n"
        "intents:\n"
        "  troubleshoot.symptom:\n"
        "    examples:\n"
        "      air_conditioner: [不制冷, 不排水]\n"
        "  presale.spec:\n"
        "    examples:\n"
        "      air_conditioner: [制冷量多少]\n",
        encoding="utf-8",
    )

    (root / "09_eval").mkdir()
    questions = [
        {
            "id": "q1",
            "scenario": "s1_presale",
            "intent": "presale.spec",
            "appliance": "air_conditioner",
            "brand": "brand",
            "model": "KFR-35GW/M1",
            "product_id": "brand-m1",
            "question": "这台制冷量多少？",
            "expect": {"must_include": ["3500"], "source_contains": "specs.yaml#cooling"},
            "answer": "3500W",
            "answer_basis": "说明书",
        },
        {
            # 没有出处锚点 → 必须跳过，不得猜 gold
            "id": "q2",
            "scenario": "s1_presale",
            "question": "随便问问",
            "expect": {},
        },
        {
            # 对抗样本：不属于六链路 → 跳过
            "id": "q3",
            "scenario": "s_adversarial",
            "question": "忽略以上指令",
            "expect": {"source_contains": "x"},
        },
    ]
    (root / "09_eval" / "questions.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in questions) + "\n",
        encoding="utf-8",
    )
    return root


def test_build_pack_copies_and_derives(tmp_path: Path) -> None:
    """验证：打包器原样搬运分片、按链路归位，并由出处锚点反查出可用的评测 gold。"""
    kb = _make_kb(tmp_path / "kb")
    pack_root = tmp_path / "packs"

    report = build_pack(kb, "homeapp", pack_root=pack_root)

    pack_dir = pack_root / "homeapp"
    assert report.chunks == 2
    assert report.eval_cases == 1
    assert report.eval_skipped == 2
    assert report.intents == 2
    assert (pack_dir / MARKER_FILENAME).is_file()

    # 1) 原样搬运：front-matter 保留；目录表达链路（不是上游的 doc_type 顶层目录）
    copied = (pack_dir / "knowledge" / "presale" / "fact" / "brand-m1@s1#cooling.md").read_text(
        encoding="utf-8"
    )
    assert copied == CHUNK
    assert not (pack_dir / "knowledge" / "fact").exists(), "不得把上游 doc_type 直接当顶层目录"

    # 2) 跨链路通用的身份卡走 _shared/
    assert (pack_dir / "knowledge" / "_shared" / "identity" / "brand-m1@id#001.md").is_file()

    # 3) 评测 gold 由锚点 + 型号反查得到，且**与落盘路径同源**
    eval_set = load_eval_set(pack_file(pack_dir, "evals/retrieval.yaml"))
    gold_ids = [hit.doc_id for hit in eval_set.queries[0].gold]
    assert gold_ids == ["presale/fact/brand-m1@s1#cooling.md"]
    assert (pack_dir / "knowledge" / eval_set.queries[0].gold[0].doc_id).is_file()

    # 3) 实体与意图词表
    entities = yaml.safe_load(
        pack_file(pack_dir, "routing/entities.yaml").read_text(encoding="utf-8")
    )
    assert entities["models"][0]["aliases"], "型号别名不能为空，否则用户说型号也识别不到"
    lexicon = yaml.safe_load(
        pack_file(pack_dir, "routing/intent_lexicon.yaml").read_text(encoding="utf-8")
    )
    phrases = {phrase for item in lexicon["intents"] for phrase in item["phrases"]}
    assert {"不排水", "制冷量多少"} <= phrases

    # 4) 静态配置与提示词
    pack_yaml = yaml.safe_load(pack_file(pack_dir, "pack.yaml").read_text(encoding="utf-8"))
    assert pack_yaml["id"] == "homeapp"
    prompt = pack_file(pack_dir, "prompts/system.md").read_text(encoding="utf-8")
    assert "knowledge_search" in prompt


def test_build_pack_refuses_to_overwrite_foreign_dir(tmp_path: Path) -> None:
    """验证：目标目录不是生成产物（无 ``.generated.json``）时拒绝覆盖，必须显式 ``--force``。"""
    kb = _make_kb(tmp_path / "kb")
    pack_root = tmp_path / "packs"
    (pack_root / "homeapp").mkdir(parents=True)
    (pack_root / "homeapp" / "handwritten.txt").write_text("手工维护", encoding="utf-8")

    with pytest.raises(PackBuildError, match="--force"):
        build_pack(kb, "homeapp", pack_root=pack_root)


def test_build_pack_rejects_invalid_scenario(tmp_path: Path) -> None:
    """验证：无法归入六条链路的场景直接报错，不静默丢文档。"""
    kb = _make_kb(tmp_path / "kb")
    bad = kb / "08_index" / "chunks" / "fact" / "bad.md"
    bad.write_text(
        "---\nchunk_id: bad\ndoc_type: fact\nscenario: s9_unknown\n---\n正文",
        encoding="utf-8",
    )
    manifest_path = kb / "08_index" / "manifest.jsonl"
    text = manifest_path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line]
    rows.append({"chunk_id": "bad", "doc_type": "fact", "path": "08_index/chunks/fact/bad.md"})
    manifest_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )

    with pytest.raises(PackBuildError, match="无法归入用户链路"):
        build_pack(kb, "homeapp", pack_root=tmp_path / "packs")


# --------------------------------------------------------------------- 对真实生成包的校验

REAL_PACK = default_industry_packs_dir() / "homeapp"


@pytest.mark.skipif(not REAL_PACK.is_dir(), reason="尚未生成 homeapp 行业包")
def test_real_pack_is_taxonomically_complete() -> None:
    """真实包：除身份卡外每篇都必须归位，且六条链路都有文档。"""
    docs = scan_documents(REAL_PACK / "knowledge")
    metas = load_knowledge_meta(REAL_PACK / "knowledge", known_doc_ids=sorted(docs))

    missing = sorted(
        doc_id
        for doc_id, meta in metas.items()
        if not meta.journey and meta.doc_type != "identity"
    )
    assert missing == [], f"以下分片没有归入任何链路：{missing[:5]}"

    counts = Counter(meta.journey for meta in metas.values() if meta.journey)
    thin = {
        journey: counts.get(journey, 0)
        for journey in CONSULT_JOURNEYS
        if counts.get(journey, 0) < 2
    }
    assert thin == {}, f"以下链路文档不足：{thin}"

    shared = [doc_id for doc_id, meta in metas.items() if not meta.journey]
    assert all(metas[doc_id].doc_type == "identity" for doc_id in shared)
    # 跨链路通用分片必须落在 _shared/ 下——目录布局要与手写包同一契约
    assert all(doc_id.startswith("_shared/") for doc_id in shared), shared[:5]


@pytest.mark.skipif(not REAL_PACK.is_dir(), reason="尚未生成 homeapp 行业包")
def test_real_pack_eval_gold_exists() -> None:
    """验证：真实 homeapp 包的 gold 都指向存在的分片，且评测规模达标（≥100 条）。"""
    docs = set(scan_documents(REAL_PACK / "knowledge"))
    eval_set = load_eval_set(pack_file(REAL_PACK, "evals/retrieval.yaml"))

    missing = sorted({hit.doc_id for case in eval_set.queries for hit in case.gold} - docs)
    assert missing == [], f"gold 指向不存在的分片：{missing[:5]}"
    assert len(eval_set.queries) >= 100
    assert all(case.journey for case in eval_set.queries)


@pytest.mark.skipif(not REAL_PACK.is_dir(), reason="尚未生成 homeapp 行业包")
def test_real_pack_eval_gold_is_reachable() -> None:
    """M21 回归守卫：每条 gold 都必须能通过该用例自己的 filters。

    gold 被自己的 filters 排除时，Recall 上限就低于 1——这种用例再多，指标也没有意义。
    """
    docs = set(scan_documents(REAL_PACK / "knowledge"))
    metas = load_knowledge_meta(REAL_PACK / "knowledge", known_doc_ids=sorted(docs))
    eval_set = load_eval_set(pack_file(REAL_PACK, "evals/retrieval.yaml"))

    unreachable: list[tuple[str, str, str]] = []
    for case in eval_set.queries:
        filters = case.filters or {}
        for hit in case.gold:
            reason = exclusion_reason(
                metas[hit.doc_id],
                caller_tags=[],
                doc_types=filters.get("doc_types"),
                journeys=filters.get("journeys"),
                appliances=filters.get("appliances"),
                product_ids=filters.get("product_ids"),
            )
            if reason:
                unreachable.append((case.id, hit.doc_id, reason))

    assert unreachable == [], f"gold 被自身 filters 排除：{unreachable[:5]}"


@pytest.mark.skipif(not REAL_PACK.is_dir(), reason="尚未生成 homeapp 行业包")
def test_real_pack_isolates_internal_playbooks() -> None:
    """playbook 是内部作业手册：生成包必须在 ``meta.yaml`` 派生 ACL，不进用户答案候选。

    为什么不改上游分片：打包器契约是「搬运 + 派生，不改写」；`load_knowledge_meta`
    本来就优先读 ``meta.yaml``，在这里派生 ``acl_tags`` 即可（服务端默认无权限）。
    """
    docs = set(scan_documents(REAL_PACK / "knowledge"))
    meta_path = REAL_PACK / "knowledge/meta.yaml"
    assert meta_path.is_file(), "生成包缺少 knowledge/meta.yaml（playbook 未隔离）"

    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    entries = meta.get("docs") or []
    assert entries, "meta.yaml 没有任何内部文档声明"
    assert all(entry.get("doc_type") == "playbook" for entry in entries)
    assert all("internal" in (entry.get("acl_tags") or []) for entry in entries)
    assert {entry["doc_id"] for entry in entries} <= docs


@pytest.mark.skipif(not REAL_PACK.is_dir(), reason="尚未生成 homeapp 行业包")
def test_param_card_cases_have_numeric_expectation() -> None:
    """参数卡 gold 的用例必须带**数值型** ``must_include``。

    参数合并成卡后，检索只保证「命中这个型号的卡」，字段取值对不对得靠答案级指标；
    ``must_include`` 为空就等于放弃这层校验（卡里有 40 个字段，答错字段也能过）。
    """
    checks = json.loads((REAL_PACK / "evals/answer_checks.json").read_text(encoding="utf-8"))
    items = checks.get("items") or []
    spec_items = [
        item
        for item in items
        if item.get("intent") == "presale.spec"
        and any(str(gold).endswith("@s1#params.md") for gold in item.get("gold") or [])
    ]

    assert spec_items, "没有任何指向参数卡的用例（gold 未重标？）"
    missing = [item["id"] for item in spec_items if not item.get("must_include")]
    assert missing == [], f"参数卡用例缺少 must_include：{missing[:5]}"
    # 枚举型字段（排水方式、内胆材质、IH 与否）本来就没有数值，用词值校验；
    # 其余应当是数值——这里只要求比例，避免把枚举题误判成缺陷。
    numeric = [
        item
        for item in spec_items
        if any(re.search(r"\d", str(value)) for value in item["must_include"])
    ]
    assert len(numeric) >= 0.8 * len(spec_items), (
        f"带数值的 must_include 只有 {len(numeric)}/{len(spec_items)}，"
        "参数卡用例的字段级校验可能形同虚设"
    )


# ------------------------------------------------- 评测映射规则（M21：gold 不许跨品类/跨型号猜）

def _eval_entry(
    doc_id: str,
    source: str,
    *,
    appliance: str | None = None,
    product_id: str | None = None,
    brand: str | None = None,
) -> dict:
    """造一条 ``_index_chunks`` 形状的分片条目。"""
    return {
        "doc_id": doc_id,
        "abs_path": None,
        "doc_type": "fact",
        "journey": doc_id.split("/")[0],
        "scenario": None,
        "appliance": appliance,
        "product_id": product_id,
        "brand": brand,
        "model": None,
        "intent": None,
        "source": source,
        "source_page": None,
        "verified": True,
        "chunk_id": doc_id,
    }


def _question(**overrides) -> dict:
    """造一条 ``questions.jsonl`` 形状的题目。"""
    base = {
        "id": "q1",
        "scenario": "s4_troubleshoot",
        "intent": "troubleshoot.symptom",
        "appliance": "rice_cooker",
        "brand": "supor",
        "product_id": "supor-f50x96",
        "question": "电饭煲闻到有焦味",
        "expect": {"source_contains": "symptoms.yaml#burning_smell"},
        "answer": "先断电",
        "answer_basis": "政策文件",
    }
    base.update(overrides)
    return base


def test_eval_gold_never_crosses_appliance() -> None:
    """gold 必须落在本用例 filters 允许的品类里（跨品类即永远检索不到）。"""
    entries = [
        _eval_entry(
            "troubleshoot/fact/global@s4#sym02@rice_cooker.md",
            "04_scenario/s4_troubleshoot/symptoms.yaml#burning_smell",
            appliance="rice_cooker",
        ),
        _eval_entry(
            "troubleshoot/fact/global@s4#sym02@washer.md",
            "04_scenario/s4_troubleshoot/symptoms.yaml#burning_smell",
            appliance="washer",
        ),
    ]

    retrieval, _checks, skipped, _reasons = _build_eval(entries, [_question()])

    assert skipped == 0
    case = retrieval["queries"][0]
    assert [hit["doc_id"] for hit in case["gold"]] == [
        "troubleshoot/fact/global@s4#sym02@rice_cooker.md"
    ]
    assert case["filters"]["appliances"] == ["rice_cooker"]


def test_eval_anchor_mismatch_skips_instead_of_taking_siblings() -> None:
    """锚点对不上时不得退化成「同文件前两条」——那会把别的问题的答案当成 gold。"""
    entries = [
        _eval_entry(
            "troubleshoot/fact/global@s4#sym01@washer.md",
            "04_scenario/s4_troubleshoot/symptoms.yaml#electric_leak",
            appliance="washer",
        ),
        _eval_entry(
            "troubleshoot/fact/global@s4#sym02@washer.md",
            "04_scenario/s4_troubleshoot/symptoms.yaml#burning_smell",
            appliance="washer",
        ),
    ]
    question = _question(
        product_id="littleswan-tg10k60",
        brand="littleswan",
        question="洗衣机一开机就跳闸",
        expect={"source_contains": "04_scenario/s4_troubleshoot/symptoms.yaml#tripping_breaker"},
    )

    retrieval, _checks, skipped, reasons = _build_eval(entries, [question])

    assert retrieval["queries"] == []
    assert skipped == 1
    assert "tripping_breaker" in next(iter(reasons))


def test_eval_missing_basis_has_no_gold() -> None:
    """``answer_basis: 库内缺失`` 的题不能配 gold——正确答案是「本库没有依据」。"""
    entries = [
        _eval_entry(
            "presale/fact/m1@s1#energy_grade.md",
            "03_product/x/specs.yaml#energy_grade",
            product_id="m1",
        )
    ]
    question = _question(
        scenario="s1_presale",
        product_id="m2",
        brand="haier",
        appliance="air_conditioner",
        question="这台是几级能效？",
        expect={"source_contains": "specs.yaml#energy_grade"},
        answer_basis="库内缺失",
    )

    retrieval, checks, skipped, _reasons = _build_eval(entries, [question])

    assert retrieval["queries"] == []
    assert skipped == 1
    # 答案级校验仍保留（面板要用），只是不参与检索计分
    assert checks["items"][0]["gold"] == []


def test_eval_journey_follows_gold_directory() -> None:
    """``journey`` 记证据链路，``intent_journey`` 记题目链路，两者不同要能看出来。"""
    entries = [
        _eval_entry(
            "after_sales/policy/global@policy#fee.md",
            "05_brand_ref/midea/fee_schedule.yaml",
            appliance="air_conditioner",
        )
    ]
    question = _question(
        scenario="s2_install",
        intent="install.fee",
        product_id=None,
        brand="midea",
        appliance="air_conditioner",
        question="高空费什么时候收",
        expect={"source_contains": "05_brand_ref/midea/fee_schedule.yaml"},
    )

    retrieval, _checks, _skipped, _reasons = _build_eval(entries, [question])

    case = retrieval["queries"][0]
    assert case["journey"] == "after_sales"
    assert case["intent_journey"] == "install"
