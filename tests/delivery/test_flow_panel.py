"""数据流面板的判定逻辑（不依赖真实模型与真实索引）。

面板有两处「说错比不说更糟」的地方，这里重点钉住：

1. **路由**：关键词命中优先；语义最近邻置信不足时**不限定链路**——
   误收窄会把正确答案直接过滤掉（实测过一次，代价很高）；
2. **校验**：回答里的引用编号必须来自本轮片段；同一句问题在不同型号下有不同标准答案，
   必须先用型号消歧。
"""

from __future__ import annotations

import types

from apps.flow import Panel, _compact, _cosine


class _Embedder:
    """固定向量的嵌入替身：只关心余弦，不需要模型。"""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vector) for _ in texts]


def _panel(**kwargs) -> Panel:
    return Panel(**kwargs)


# --------------------------------------------------------------------- 路由


def _route_panel(vectors: list[list[float]], intents=None) -> Panel:
    prototypes = [
        {"journey": "presale", "question": "这台制冷量多少", "intent": "presale.spec"},
        {"journey": "usage", "question": "怎么清洗", "intent": "usage.maintenance"},
    ]
    return Panel(
        bundle=types.SimpleNamespace(embedder=_Embedder([1.0, 0.0])),
        prototypes=prototypes,
        prototype_vectors=vectors,
        intents=intents or [],
    )


def test_keyword_hit_wins_over_semantic() -> None:
    """验证：关键词命中优先于语义相似，且取**最长**命中词（越具体越准）。"""
    panel = _route_panel(
        vectors=[[0.6, 0.8], [1.0, 0.0]],  # 语义上更像 usage
        intents=[
            {
                "intent": "troubleshoot.symptom",
                "journey": "troubleshoot",
                "phrases": ["排水", "不排水"],
            }
        ],
    )

    result = panel.route("洗衣机不排水怎么处理")

    assert result["chosen"] == "troubleshoot"
    assert result["confidence"] == "high"
    assert result["keyword"]["phrase"] == "不排水", "应取最长命中词，越具体越好"
    assert "意图词" in result["reason"]


def test_semantic_route_requires_margin() -> None:
    """两条链路得分接近时**不限定链路**：宁可全库检索，也不误收窄。"""
    panel = _route_panel(vectors=[[1.0, 0.0], [1.0, 0.0]])

    result = panel.route("随便问问")

    assert result["scores"][0]["score"] == result["scores"][1]["score"]
    assert result["chosen"] is None
    assert result["confidence"] == "low"


def test_semantic_route_when_confident() -> None:
    """验证：仅语义、且领先幅度足够时才收窄链路（``margin > 0.06``），置信度标 ``medium``。"""
    panel = _route_panel(vectors=[[1.0, 0.0], [0.2, 1.0]])

    result = panel.route("随便问问")

    assert result["chosen"] == "presale"
    assert result["confidence"] == "medium"
    assert result["margin"] and result["margin"] > 0.06


def test_manual_journey_overrides_everything() -> None:
    """验证：手动指定链路优先级最高，覆盖关键词与语义结果。"""
    panel = _route_panel(
        vectors=[[1.0, 0.0], [0.2, 1.0]],
        intents=[{"intent": "x", "journey": "troubleshoot", "phrases": ["不排水"]}],
    )

    result = panel.route("洗衣机不排水", manual="install")

    assert result["chosen"] == "install"
    assert result["confidence"] == "manual"


# --------------------------------------------------------------------- 校验


CHECKS = [
    {
        "id": "q-m1",
        "question": "这台制冷量多少？",
        "product_id": "m1",
        "must_include": ["3500"],
        "must_not_include": ["5000"],
        "reference_answer": "3500W",
        "basis": "说明书 p30",
    },
    {
        "id": "q-m2",
        "question": "这台制冷量多少？",
        "product_id": "m2",
        "must_include": ["5200"],
        "must_not_include": [],
        "reference_answer": "5200W",
        "basis": "说明书 p31",
    },
]


def _chunks(**overrides) -> list[dict]:
    base = {
        "index": 1,
        "doc_id": "fact/m1.md",
        "source_page": 30,
        "verified": True,
        "origin": "specs.yaml#cooling",
    }
    base.update(overrides)
    return [base]


def test_verify_extracts_citations_and_passes_answer_check() -> None:
    """验证：引用编号被抽出并映射到来源，答案校验按型号命中 ``q-m1`` 并通过。"""
    panel = _panel(checks=CHECKS)

    out = panel._verify("这台制冷量多少？", "额定制冷量 3500W [1]。", _chunks(), ["m1"])

    assert out["cited"] == [1]
    assert out["sources"][0]["doc_id"] == "fact/m1.md"
    assert out["invalid_citations"] == []
    assert out["answer_check"]["id"] == "q-m1"
    assert out["answer_check"]["passed"] is True


def test_verify_disambiguates_same_question_by_product() -> None:
    """验证：同一句问题在不同型号下标准答案不同，必须先用 ``product_ids`` 消歧再校验。"""
    panel = _panel(checks=CHECKS)

    out = panel._verify("这台制冷量多少？", "5200W [1]", _chunks(), ["m2"])

    assert out["answer_check"]["id"] == "q-m2"
    assert out["answer_check"]["passed"] is True


def test_verify_flags_missing_must_include() -> None:
    """验证：答案缺少 ``must_include`` 关键值时判失败，并列出缺的项。"""
    panel = _panel(checks=CHECKS)

    out = panel._verify("这台制冷量多少？", "大概三千多瓦 [1]", _chunks(), ["m1"])

    assert out["answer_check"]["passed"] is False
    assert out["answer_check"]["missing"] == ["3500"]


def test_verify_flags_forbidden_value() -> None:
    """验证：答案出现 ``must_not_include`` 的禁止值（张冠李戴的型号参数）时判失败。"""
    panel = _panel(checks=CHECKS)

    out = panel._verify("这台制冷量多少？", "3500W，不是 5000W [1]", _chunks(), ["m1"])

    assert out["answer_check"]["passed"] is False
    assert out["answer_check"]["forbidden_hit"] == ["5000"]


def test_verify_flags_citation_out_of_range() -> None:
    """验证：引用编号超出本轮片段范围时记为无效引用并告警。"""
    panel = _panel(checks=[])

    out = panel._verify("随便问", "结论 [7]", _chunks(), [])

    assert out["invalid_citations"] == [7]
    assert any("不存在的编号" in warning for warning in out["warnings"])


def test_verify_warns_when_no_citation() -> None:
    """验证：答案没有任何引用标注时告警——「说了但没给依据」要能被看见。"""
    panel = _panel(checks=[])

    out = panel._verify("随便问", "没有任何标注的结论", _chunks(), [])

    assert out["cited"] == []
    assert any("没有标注任何引用" in warning for warning in out["warnings"])


def test_verify_warns_on_unverified_evidence() -> None:
    """验证：引用到未人工核对的片段时单独告警（``unverified_used``），不与「引用无效」混为一谈。"""
    panel = _panel(checks=[])

    out = panel._verify("随便问", "结论 [1]", _chunks(verified=False), [])

    assert out["unverified_used"] == [1]
    assert any("尚未人工核对" in warning for warning in out["warnings"])


# --------------------------------------------------------------------- 上下文


def test_build_messages_numbers_chunks_and_carries_provenance() -> None:
    """验证：拼上下文时给片段编号，并带上来源、页码与「已核对 / 未核对」标记。"""
    panel = _panel(system_prompt="SYSTEM")

    messages = panel._build_messages(
        "这台制冷量多少？",
        [
            {
                "index": 1,
                "doc_id": "fact/m1.md",
                "text": "额定 3500W",
                "origin": "specs.yaml#cooling",
                "source_page": 30,
                "verified": True,
            },
            {"index": 2, "doc_id": "policy/x.md", "text": "未核对内容", "verified": False},
        ],
        model_hint="品牌 M1",
        appliance_hint="空调",
    )

    assert messages[0] == {"role": "system", "content": "SYSTEM"}
    user = messages[1]["content"]
    assert "[1] 来源：fact/m1.md" in user
    assert "第 30 页" in user
    assert "【已核对】" in user
    assert "【未核对】" in user
    assert "已知型号：品牌 M1" in user
    assert "已知品类：空调" in user


# --------------------------------------------------------------------- 小工具


def test_compact_ignores_formatting() -> None:
    """验证：答案比对前先归一化（``3,510 W。`` 与 ``3510W`` 等价），避免格式差异误判。"""
    assert _compact("3,510 W。") == _compact("3510W")


def test_cosine_handles_zero_vector() -> None:
    """验证：零向量余弦返回 0 而非除零崩溃（嵌入降级时会出现全零向量）。"""
    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert abs(_cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9
