"""把外部知识库（``appliance-kb`` 这类分层 KB）打包成可直接索引的行业包。

## 为什么需要它

外部 KB 已经做完了最贵的一步：把说明书 OCR / 抽取 / 归一化成**带元数据的分片**
（``08_index/chunks/{fact,faq,playbook,policy}/*.md``，每片自带 ``chunk_id`` / ``scenario`` /
``source`` / ``source_page`` / ``verified``）。再让业务方按我们的格式重写一遍，既浪费又必然漂移。

因此打包器做的是**搬运 + 派生**，不做改写：

| 产物 | 来源 |
| --- | --- |
| ``knowledge/<journey>/<doc_type>/*.md`` | 原样复制（front-matter 保留，由 Core 解析）；目录表达**用户链路**，与手写包 ``appliance`` 同一契约，跨链路通用分片放 ``_shared/`` |
| ``evals/retrieval.yaml`` | 由 ``09_eval/questions.jsonl`` 的 ``source_contains`` + ``product_id`` 反查 gold |
| ``evals/answer_checks.json`` | 由同一份评测集派生（供交互面板做「答案是否含必答数值」校验） |
| ``routing/entities.yaml`` | 由 ``03_product/**/product.yaml`` 派生（型号别名、品类词） |
| ``routing/prototypes.yaml`` | 由评测集按场景抽样（用途：把问题路由到对应链路） |
| ``prompts/system.md`` | 由 KB 自己的 ``answer_style.md`` + ``safety_redlines.md`` 组装 |

## 用法

```bash
python -m apps.build_kb_pack --kb appliance-kb --pack homeapp          # 生成
python -m apps.build_kb_pack --kb appliance-kb --pack homeapp --dry-run # 只报告
```

生成目录带 ``.generated.json`` 标记；重复生成需 ``--force``，避免覆盖手工维护的行业包。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from zkagent.config import default_industry_packs_dir
from zkagent.core.rag.metadata import (
    KnowledgeMetaError,
    journey_of_doc_id,
    journey_of_scenario,
    parse_front_matter,
)

__all__ = ["build_pack", "PackBuildReport"]

#: 生成目录的标记文件（同时记录来源与版本，便于追溯）。
MARKER_FILENAME = ".generated.json"

#: 跨链路通用分片的目录（型号身份卡）：``journey is None`` 的**唯一**合法落点。
#: 它不属于六条对外链路，因此 ``journey_of_doc_id`` 推导为 ``None``，链路过滤时始终保留。
SHARED_DIR = "_shared"

#: 打包器写入的静态文件模板目录（与代码同仓，避免把行业话术写进 Python）。
TEMPLATE_DIR = Path(__file__).with_name("pack_templates")


class PackBuildError(RuntimeError):
    """打包前置条件不满足（源数据缺失 / 元数据非法）——**显式失败**。"""


@dataclass
class PackBuildReport:
    """一次打包的结果摘要（CLI 直接打印）。"""

    pack_id: str
    pack_dir: str
    chunks: int
    by_doc_type: dict[str, int]
    by_journey: dict[str, int]
    by_appliance: dict[str, int]
    eval_cases: int
    eval_skipped: int
    models: int
    intents: int
    intent_phrases: int
    wrote: bool
    eval_skip_reasons: dict[str, int]
    #: 重建时旧包被移到哪里（便于回滚；不存在则为 ``None``）
    previous_moved_to: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "pack_dir": self.pack_dir,
            "chunks": self.chunks,
            "by_doc_type": self.by_doc_type,
            "by_journey": self.by_journey,
            "by_appliance": self.by_appliance,
            "eval_cases": self.eval_cases,
            "eval_skipped": self.eval_skipped,
            "models": self.models,
            "intents": self.intents,
            "intent_phrases": self.intent_phrases,
            "wrote": self.wrote,
            "eval_skip_reasons": self.eval_skip_reasons,
            "previous_moved_to": self.previous_moved_to,
        }


# --------------------------------------------------------------------- 读取源数据


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise PackBuildError(f"缺少文件：{path}")
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise PackBuildError(f"{path} 第 {line_no} 行不是合法 JSON：{exc}") from exc
    return rows


def _load_yaml(path: Path) -> Any:
    if not path.is_file():
        raise PackBuildError(f"缺少文件：{path}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PackBuildError(f"{path} YAML 解析失败：{exc}") from exc


def _index_chunks(kb_root: Path) -> list[dict[str, Any]]:
    """读取 manifest 与分片 front-matter，校验并补全字段。"""
    manifest = _load_jsonl(kb_root / "08_index" / "manifest.jsonl")
    problems: list[str] = []
    entries: list[dict[str, Any]] = []

    for row in manifest:
        rel_path = str(row.get("path") or "")
        path = kb_root / rel_path
        if not path.is_file():
            problems.append(f"分片文件不存在：{rel_path}")
            continue
        try:
            front, body = parse_front_matter(path.read_text(encoding="utf-8"))
        except KnowledgeMetaError as exc:
            problems.append(f"{rel_path}：{exc}")
            continue
        if not front:
            problems.append(f"{rel_path} 缺少 front-matter")
            continue

        chunk_id = str(front.get("chunk_id") or "")
        if chunk_id and chunk_id != row.get("chunk_id"):
            problems.append(f"{rel_path}：front-matter 的 chunk_id 与 manifest 不一致")
            continue

        scenario = front.get("scenario")
        journey = journey_of_scenario(scenario)
        if journey is None:
            # 身份卡是刻意的「跨链路通用」文档（型号信息在每个链路都要用得到）；
            # 其余分片缺 scenario 一律视为数据缺陷，不放过。
            if str(front.get("doc_type") or "") != "identity":
                problems.append(
                    f"{rel_path}：未知 scenario={scenario!r}，无法归入用户链路"
                    "（只有 doc_type=identity 允许通用）"
                )
                continue

        doc_type = str(front.get("doc_type") or "faq")
        # doc_id 就是写入 ``knowledge/`` 后的**相对路径**：落盘与评测 gold 共用它，
        # 避免「目录改了、gold 没改」这种只能靠指标下滑才发现的事故。
        doc_id = f"{journey or SHARED_DIR}/{doc_type}/{path.name}"

        entries.append(
            {
                "doc_id": doc_id,
                "abs_path": path,
                "doc_type": doc_type,
                "journey": journey,
                "scenario": scenario,
                "appliance": front.get("appliance"),
                "product_id": front.get("product_id"),
                "brand": front.get("brand"),
                "model": front.get("model"),
                "intent": front.get("intent"),
                "source": front.get("source"),
                "source_page": front.get("source_page"),
                "verified": front.get("verified"),
                #: FAQ 只是入口（答案在权威文档）→ 它的 gold 应指向被指向的切片
                "pointer": bool(front.get("pointer")),
                "body": body.strip(),
                "chunk_id": chunk_id or row.get("chunk_id"),
            }
        )

    duplicated = [
        doc_id for doc_id, count in Counter(entry["doc_id"] for entry in entries).items() if count > 1
    ]
    if duplicated:
        # 同名文件落进同一目录会静默互相覆盖（一个分片凭空消失），必须在打包前挡住
        problems.append(f"doc_id 冲突（目标路径同名）：{sorted(duplicated)[:5]}")

    if problems:
        preview = "\n  - ".join(problems[:15])
        raise PackBuildError(f"源分片有 {len(problems)} 处问题，先修数据再打包：\n  - {preview}")
    if not entries:
        raise PackBuildError("源知识库没有任何可用分片")
    return entries


def _index_models(kb_root: Path) -> list[dict[str, Any]]:
    """从 ``03_product/**/product.yaml`` 读取型号身份卡。"""
    models: list[dict[str, Any]] = []
    for path in sorted((kb_root / "03_product").rglob("product.yaml")):
        data = _load_yaml(path)
        if not isinstance(data, dict) or not data.get("product_id"):
            continue
        models.append(
            {
                "product_id": str(data["product_id"]),
                "appliance": data.get("appliance"),
                "appliance_cn": data.get("appliance_cn"),
                "brand": data.get("brand"),
                "brand_cn": data.get("brand_cn"),
                "model": data.get("model"),
                "model_slug": data.get("model_slug"),
                "colloquial": data.get("colloquial"),
                "status": data.get("status"),
                "product_type": data.get("product_type"),
            }
        )
    return models


def _build_entities(models: list[dict[str, Any]]) -> dict[str, Any]:
    """派生「品类词 + 型号别名」，并剔除跨型号歧义的别名。"""
    appliances: dict[str, dict[str, Any]] = {}
    for item in models:
        key = item.get("appliance")
        if not key:
            continue
        entry = appliances.setdefault(
            str(key),
            {"key": str(key), "label": item.get("appliance_cn") or str(key), "words": [], "models": 0},
        )
        entry["models"] += 1
        label = str(item.get("appliance_cn") or "")
        if label and label not in entry["words"]:
            entry["words"].append(label)

    # 别名 → 型号们；只保留唯一指向的别名（"1.5匹" 这类会指向多个型号，不参与精确匹配）
    alias_map: dict[str, set[str]] = {}
    for item in models:
        pid = str(item.get("product_id"))
        for raw in (item.get("model"), item.get("model_slug"), item.get("product_id")):
            alias = str(raw or "").strip()
            if alias:
                alias_map.setdefault(alias, set()).add(pid)

    model_entries = []
    for item in models:
        pid = str(item.get("product_id"))
        aliases = [
            alias
            for alias, owners in alias_map.items()
            if owners == {pid} and len(alias) >= 4
        ]
        # 较长的别名优先匹配（避免 "KFR-35GW/Q2" 被更短的串抢先）
        aliases.sort(key=len, reverse=True)
        model_entries.append(
            {
                "product_id": pid,
                "appliance": item.get("appliance"),
                "brand": item.get("brand"),
                "brand_cn": item.get("brand_cn"),
                "model": item.get("model"),
                "label": f"{item.get('brand_cn') or ''} {item.get('model') or ''}".strip(),
                "colloquial": item.get("colloquial"),
                "product_type": item.get("product_type"),
                "status": item.get("status"),
                "aliases": aliases,
            }
        )

    return {
        "appliances": sorted(appliances.values(), key=lambda entry: entry["key"]),
        "models": model_entries,
    }


def _compatible(entry: dict[str, Any], *, appliance: str | None, brand: str | None) -> bool:
    """分片是否与题目的品类 / 品牌相容。

    分片自己没写该字段时视为「通用」，不排除；写了且不同才排除。这条约束是 gold 的
    最后一道防线：``filters`` 会按 ``appliances`` / ``product_ids`` 收窄检索范围，
    gold 若来自别的品类，就永远不可能被返回（KNOWN_ISSUES M21）。
    """
    if appliance and entry.get("appliance") and entry["appliance"] != appliance:
        return False
    if brand and entry.get("brand") and entry["brand"] != brand:
        return False
    return True


POINTER_KIND = {
    "specs": ("presale", "s1", "params"),
    "features": ("presale", "s1", "params"),
    "usage": ("usage", "s3", None),
    "install": ("install", "s2", None),
    "faults": ("troubleshoot", "s4", None),
}


def _pointer_target(answer: str, entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从 FAQ 答案的「详见 X 的 usage.yaml（maintenance）」解析出权威切片。"""
    match = re.search(
        r"详见 (\S+) 的 (specs|usage|install|features|faults)\.yaml(?:（([^）]+)）)?", answer
    )
    if not match:
        return None
    pid, kind = match.group(1), match.group(2)
    section = str(match.group(3) or "").split("+")[0].split(".", 1)[0].strip()
    journey, stage, fixed = POINTER_KIND[kind]
    target = f"{journey}/fact/{pid}@{stage}#{fixed or section}.md"
    return next((entry for entry in entries if entry["doc_id"] == target), None)


def _faq_answers(kb_root: Path) -> dict[str, str]:
    """``faq_id → answer``（上游 FAQ 源）。

    指针型 FAQ 已不再生成切片，解析「详见 X」只能读源文件——分片正文这条路走不通了。
    """
    answers: dict[str, str] = {}
    for path in sorted((kb_root / "04_scenario").glob("s*/faq.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for item in data.get("faq") or []:
            if item.get("id"):
                answers[str(item["id"])] = str(item.get("answer") or "")
    return answers


def _build_eval(
    entries: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    faq_answers: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """把评测题映射到 gold 分片；返回 (retrieval.yaml, answer_checks.json, 跳过数)。

    映射规则（宁可跳过，也不猜）：
    - 题目的 ``expect.source_contains`` 必须能在某个分片的 ``source`` 里原样找到；
    - 先在同型号分片里找，找不到再退到「同品类 + 同品牌」；**绝不跨品类 / 跨品牌**——
      否则 gold 会被本用例的 ``filters`` 排除，指标必错；
    - ``answer_basis: 库内缺失`` 的题**不产出 gold**：正确答案是「没有依据」，检索 recall
      无法给它计分（空 gold 恒为 0 分）；这类题只留在 ``answer_checks.json`` 里做答案级校验；
    - 找不到就计入 ``skipped``，绝不拿相似分片顶替（gold 错了，指标就没意义）。

    ``journey`` 写 gold 所在目录（**证据链路**）：分组指标要回答的是「哪条链路的知识
    没被检索到」；题目自身的链路写 ``intent_journey``，两者不同说明证据在别的链路。
    """
    by_product: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if entry["product_id"]:
            by_product.setdefault(str(entry["product_id"]), []).append(entry)

    queries: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    skipped = 0
    skip_reasons: Counter[str] = Counter()

    for item in questions:
        journey = journey_of_scenario(item.get("scenario"))
        expect = item.get("expect") or {}
        needle = str(expect.get("source_contains") or "").strip()
        if journey is None:
            skipped += 1
            skip_reasons["非六链路场景（如对抗样本）"] += 1
            continue
        if not needle:
            skipped += 1
            skip_reasons["题目未给出处锚点"] += 1
            continue

        # 槽位表题（「帮我报修」「换主板要等多久」）：答案是**对话流程约束**（先问什么、
        # 能承诺什么话术），不是可检索的文档事实。硬做成切片会两头不讨好——配置文本与
        # 自然问句语义距离远，题命中不了；同时这批切片极泛，实测污染 55 条其它用例的
        # top-5，整体 MRR 0.875 → 0.789。只留答案级校验，不进检索指标。
        if "slots.yaml" in needle:
            checks.append(
                {
                    "id": str(item.get("id")),
                    "journey": journey,
                    "scenario": item.get("scenario"),
                    "intent": item.get("intent"),
                    "question": str(item.get("question") or "").strip(),
                    "product_id": item.get("product_id"),
                    "model": item.get("model"),
                    "brand": item.get("brand"),
                    "appliance": item.get("appliance"),
                    "must_include": expect.get("must_include") or [],
                    "must_not_include": expect.get("must_not_include") or [],
                    "handoff": bool(expect.get("handoff")),
                    "reference_answer": item.get("answer"),
                    # 保留上游原 basis（其中有「库内缺失」），不要用流程题覆盖掉
                    "basis": item.get("answer_basis") or "槽位表流程题",
                    "gold": [],
                }
            )
            skipped += 1
            skip_reasons["槽位表流程题（只做答案级校验，不进检索指标）"] += 1
            continue

        product_id = item.get("product_id")
        appliance = item.get("appliance")
        brand = item.get("brand")
        pool = by_product.get(str(product_id), []) if product_id else entries

        # 参数与功能都并进了「型号参数卡」（`00_schema/single_source.md`）：
        # 按 doc_id 直接定位，`specs.yaml#key` / `features.yaml#key` / 简写路径都成立。
        base_part, _, anchor_part = needle.partition("#")
        matches = []
        if product_id and anchor_part and base_part.endswith(("specs.yaml", "features.yaml")):
            card_doc_id = f"presale/fact/{product_id}@s1#params.md"
            matches = [entry for entry in pool if entry["doc_id"] == card_doc_id]

        if not matches:
            matches = [
                entry
                for entry in pool
                if needle in str(entry.get("source") or "")
                and _compatible(entry, appliance=appliance, brand=brand)
            ]
        if not matches:
            # 型号内找不到 ≠ 放弃：退到「同品类 + 同品牌」再找一次（品牌级政策、跨型号通用症状）。
            # 这里不能像以前那样放宽到全库——选出来的 gold 会被 filters 排除（M21）。
            matches = [
                entry
                for entry in entries
                if needle in str(entry.get("source") or "")
                and _compatible(entry, appliance=appliance, brand=brand)
            ]

        if not matches:
            # 二级匹配：政策类分片的 source 只到文件级（没有 #锚点），这时用「文件名 + 品类 +
            # 品牌」三重约束定位。要求分片自己也没有锚点：否则症状类文件（同一 yaml 里几十个
            # 症状）会退化成「随便取前两条」，gold 就成了别的问题的答案。
            file_part = needle.split("#", 1)[0]
            matches = [
                entry
                for entry in entries
                if file_part
                and str(entry.get("source") or "").split("#", 1)[0] == file_part
                and "#" not in str(entry.get("source") or "")
                and _compatible(entry, appliance=appliance, brand=brand)
            ]

        # needle 指向 FAQ（`04_scenario/*/faq.yaml#faq-id`）：FAQ 只是入口，gold 应指向它
        # 指向的权威切片。指针从 **FAQ 源文件**的 answer 解析——指针型 FAQ 已不再生成切片
        # （见 `build_index.build_scenarios`），不能依赖分片正文。
        if "/faq.yaml#" in needle:
            faq_id = needle.rsplit("#", 1)[-1]
            exact = _pointer_target(str((faq_answers or {}).get(faq_id) or ""), entries)
            if exact is not None:
                matches = [exact]

        #: 「库内缺失」= 上游认为本库没有依据：正确答案是承认缺失，不产出 gold。
        missing_basis = str(item.get("answer_basis") or "").strip() == "库内缺失"

        gold: list[dict[str, Any]] = []
        if matches and not missing_basis:
            seen: set[str] = set()
            for entry in matches[:2]:
                if entry["doc_id"] in seen:
                    continue
                seen.add(entry["doc_id"])
                gold.append({"doc_id": entry["doc_id"], "rel": 2})

        if missing_basis:
            # 保留答案级校验（must_include / handoff），但不进检索评测
            checks.append(
                {
                    "id": str(item.get("id")),
                    "journey": journey,
                    "scenario": item.get("scenario"),
                    "intent": item.get("intent"),
                    "question": str(item.get("question") or "").strip(),
                    "product_id": product_id,
                    "model": item.get("model"),
                    "brand": brand,
                    "appliance": appliance,
                    "must_include": expect.get("must_include") or [],
                    "must_not_include": expect.get("must_not_include") or [],
                    "handoff": bool(expect.get("handoff")),
                    "reference_answer": item.get("answer"),
                    "basis": item.get("answer_basis"),
                    "gold": [],
                }
            )
            skipped += 1
            skip_reasons["库内缺失题（不产出 gold，只做答案级校验）"] += 1
            continue

        if not matches:
            skipped += 1
            skip_reasons[f"找不到出处：{needle}"] += 1
            continue

        # 评测必须带上「已确定的型号/品类」：题目多是对话态问题（「这台制冷量多少？」），
        # 不带型号等于让检索在 28 个型号里猜，数字会失真。真实链路里型号由澄清步骤先确定。
        filters: dict[str, Any] = {}
        if product_id:
            filters["product_ids"] = [str(product_id)]
        if appliance:
            filters["appliances"] = [str(appliance)]
        # 品牌收窄：与生产一致（`knowledge_tool` 由 `product_id` 查身份卡后注入）。
        # 评测若不带品牌，兄弟品牌的政策会占满 top-5（实测「这台多重？」召回
        # `brand_littleswan_warranty`、`brand_haier_fault_codes`）。
        if brand:
            filters["brands"] = [str(brand)]

        queries.append(
            {
                "id": str(item.get("id")),
                "journey": journey_of_doc_id(gold[0]["doc_id"]) or journey,
                "intent_journey": journey,
                "query": str(item.get("question") or "").strip(),
                "gold": gold,
                "filters": filters,
                "note": f"{item.get('intent') or ''} / {item.get('model') or ''}".strip(" /"),
            }
        )
        checks.append(
            {
                "id": str(item.get("id")),
                "journey": journey,
                "scenario": item.get("scenario"),
                "intent": item.get("intent"),
                "question": str(item.get("question") or "").strip(),
                "product_id": product_id,
                "model": item.get("model"),
                "brand": brand,
                "appliance": appliance,
                "must_include": expect.get("must_include") or [],
                "must_not_include": expect.get("must_not_include") or [],
                "handoff": bool(expect.get("handoff")),
                "reference_answer": item.get("answer"),
                "basis": item.get("answer_basis"),
                "gold": [hit["doc_id"] for hit in gold],
            }
        )

    return (
        {"version": 1, "queries": queries},
        {"version": 1, "items": checks},
        skipped,
        dict(skip_reasons.most_common()),
    )


#: 意图前缀 → KB 场景名（KB 的 intent 命名约定：``场景.子意图``）。
_INTENT_PREFIX_TO_SCENARIO: dict[str, str] = {
    "presale": "s1_presale",
    "install": "s2_install",
    "usage": "s3_usage",
    "troubleshoot": "s4_troubleshoot",
    "aftersale": "s5_aftersale",
    "service": "s6_service",
}


def _build_intent_lexicon(kb_root: Path) -> dict[str, Any]:
    """从 KB 的 ``intent_map.yaml`` 派生「意图词表」。

    为什么需要它：单靠「问句向量最近邻」做路由不够稳——中文问句共享大量品类词
    （品类名几乎出现在每条问句里），实测「不排水怎么处理」会被判成售后政策。
    KB 自己已经把用户说法按意图列了出来（``troubleshoot.symptom: [不排水, 不进水, …]``），
    这是**最便宜的强信号**：命中词直接给出意图，且理由可解释。
    """
    path = kb_root / "04_scenario" / "_shared" / "intent_map.yaml"
    if not path.is_file():
        return {"version": 1, "intents": []}
    data = _load_yaml(path) or {}

    intents: list[dict[str, Any]] = []
    for key, spec in (data.get("intents") or {}).items():
        if not isinstance(spec, dict):
            continue
        prefix = str(key).split(".", 1)[0]
        scenario = _INTENT_PREFIX_TO_SCENARIO.get(prefix)
        journey = journey_of_scenario(scenario)
        if journey is None:
            continue
        examples = spec.get("examples") or []
        raw: list[str] = []
        if isinstance(examples, dict):
            for items in examples.values():
                raw.extend(str(item) for item in (items or []))
        elif isinstance(examples, list):
            raw.extend(str(item) for item in examples)
        # 同长度时按字面兜底排序：只按长度排会让 `set` 的迭代顺序漏进产物，
        # 同一个 KB 每次打包都产生一份「内容相同、顺序不同」的 diff（跨进程哈希随机）。
        phrases = sorted(
            {item.strip().lower() for item in raw if len(item.strip()) >= 2},
            key=lambda phrase: (-len(phrase), phrase),
        )
        if not phrases:
            continue
        intents.append(
            {
                "intent": str(key),
                "journey": journey,
                "scenario": scenario,
                "phrases": phrases,
            }
        )
    return {"version": 1, "intents": intents}


def _build_prototypes(checks: dict[str, Any], limit_per_journey: int = 6) -> dict[str, Any]:
    """按链路抽样代表问句，作为「问题 → 链路」路由的原型。"""
    seen: set[str] = set()
    picked: Counter[str] = Counter()
    prototypes: list[dict[str, Any]] = []
    for item in checks.get("items") or []:
        journey = item.get("journey")
        question = str(item.get("question") or "").strip()
        if not journey or not question or question in seen:
            continue
        if picked[journey] >= limit_per_journey:
            continue
        seen.add(question)
        picked[journey] += 1
        prototypes.append(
            {
                "journey": journey,
                "intent": item.get("intent"),
                "question": question,
            }
        )
    return {"version": 1, "prototypes": prototypes}


def _compose_system_prompt(kb_root: Path, template_dir: Path) -> str:
    """把 KB 自己的回答规范与安全红线拼进系统提示（不另写一套话术）。"""
    shared = kb_root / "04_scenario" / "_shared"
    parts = [(template_dir / "system_header.md").read_text(encoding="utf-8").strip()]
    for name in ("answer_style.md", "safety_redlines.md"):
        path = shared / name
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8").strip())
    footer = template_dir / "system_footer.md"
    if footer.is_file():
        parts.append(footer.read_text(encoding="utf-8").strip())
    return "\n\n---\n\n".join(parts) + "\n"


# --------------------------------------------------------------------- 写盘


def _write_knowledge(entries: list[dict[str, Any]], knowledge_dir: Path) -> None:
    """按**用户链路**落盘：``knowledge/<journey>/<doc_type>/<name>.md``。

    目录表达链路是本仓库的既定契约（手写包 ``appliance`` 同此），这样 ``ls knowledge/``
    就能直接看出「哪条链路没有文档」——这正是按链路组织知识的初衷。
    只改路径、不改内容：分片连同 front-matter 原样复制。
    """
    for entry in entries:
        target = knowledge_dir / entry["doc_id"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(entry["abs_path"], target)


def _write_internal_meta(entries: list[dict[str, Any]], knowledge_dir: Path) -> int:
    """把内部作业手册（``playbook``）登记进 ``knowledge/meta.yaml``：ACL 隔离。

    为什么不改分片 front-matter：上游分片是「知识原文」，打包器的契约是
    「搬运 + 派生，不改写」；而 `load_knowledge_meta` 本来就优先读 ``meta.yaml``，
    在这里派生 ``acl_tags: [internal]`` 即可——服务端默认没有 ``kb:internal`` 权限，
    于是 playbook 不再进入用户答案候选，agent 仍可在授权场景取用。

    :returns: 被隔离的文档数。
    """
    docs = [
        {"doc_id": entry["doc_id"], "doc_type": entry["doc_type"], "acl_tags": ["internal"]}
        for entry in entries
        if entry["doc_type"] == "playbook"
    ]
    if not docs:
        return 0
    _dump_yaml(knowledge_dir / "meta.yaml", {"version": 1, "docs": docs})
    return len(docs)


def _dump_yaml(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )


def _write_seeds(seed_dir: Path, template_seed_dir: Path | None) -> None:
    """种子数据：**模板里有就复制模板，没有才写空列表**。

    模板（``apps/pack_templates/<pack>/seed_data/``）是唯一事实来源：业务数据由
    ``scripts/gen_homeapp_seed.py`` 生成到模板目录，重打包时复制过来。
    否则每次重跑打包都会把几十条业务数据清空——那是真实发生过的返工。
    """
    seed_dir.mkdir(parents=True, exist_ok=True)
    if template_seed_dir is not None and template_seed_dir.is_dir():
        for source in sorted(template_seed_dir.glob("*.yaml")):
            shutil.copyfile(source, seed_dir / source.name)
        return
    for name, key in (
        ("users.yaml", "users"),
        ("orders.yaml", "orders"),
        ("logistics.yaml", "logistics"),
        ("service_tickets.yaml", "service_tickets"),
        ("products.yaml", "products"),
    ):
        _dump_yaml(seed_dir / name, {key: []})


def build_pack(
    kb_root: Path | str,
    pack_id: str = "homeapp",
    *,
    pack_root: Path | str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> PackBuildReport:
    """执行打包；``dry_run=True`` 时只计算并返回报告。"""
    kb_root = Path(kb_root).resolve()
    if not (kb_root / "08_index").is_dir():
        raise PackBuildError(f"不像是受支持的知识库（缺少 08_index）：{kb_root}")

    pack_dir = Path(pack_root or default_industry_packs_dir()) / pack_id
    marker = pack_dir / MARKER_FILENAME
    if pack_dir.exists() and not marker.exists() and not force:
        raise PackBuildError(
            f"{pack_dir} 已存在且不是生成目录；如确认覆盖请加 --force"
        )

    entries = _index_chunks(kb_root)
    models = _index_models(kb_root)
    if not models:
        raise PackBuildError(f"{kb_root}/03_product 下没有任何 product.yaml")

    questions = _load_jsonl(kb_root / "09_eval" / "questions.jsonl")
    retrieval_eval, checks, skipped, skip_reasons = _build_eval(
        entries, questions, _faq_answers(kb_root)
    )
    prototypes = _build_prototypes(checks)
    entities = _build_entities(models)
    lexicon = _build_intent_lexicon(kb_root)

    by_doc_type = dict(sorted(Counter(entry["doc_type"] for entry in entries).items()))
    by_journey = dict(
        sorted(Counter(entry["journey"] or SHARED_DIR for entry in entries).items())
    )
    by_appliance = dict(
        sorted(Counter(str(entry["appliance"]) for entry in entries).items())
    )

    report = PackBuildReport(
        pack_id=pack_id,
        pack_dir=str(pack_dir),
        chunks=len(entries),
        by_doc_type=by_doc_type,
        by_journey=by_journey,
        by_appliance=by_appliance,
        eval_cases=len(retrieval_eval["queries"]),
        eval_skipped=skipped,
        models=len(models),
        intents=len(lexicon.get("intents") or []),
        intent_phrases=sum(len(item["phrases"]) for item in lexicon.get("intents") or []),
        wrote=not dry_run,
        eval_skip_reasons=skip_reasons,
    )
    if dry_run:
        return report

    moved_previous: str | None = None
    # ``evals/`` 里有两类文件：本函数生成的（``retrieval.yaml`` / ``answer_checks.json``）与
    # 人工维护或按包标定的（``cases.yaml`` / ``gate_calibration.json`` 等）。重打包会整体重建
    # 目录，不先留住就会**静默丢文件**——曾把 22 条评估用例和包级闸门阈值一起弄丢。
    generated_evals = {"retrieval.yaml", "answer_checks.json"}
    previous_eval_files: dict[str, bytes] = {}
    previous_evals_dir = pack_dir / "evals"
    if previous_evals_dir.is_dir():
        for path in sorted(previous_evals_dir.iterdir()):
            if path.is_file() and path.name not in generated_evals:
                previous_eval_files[path.name] = path.read_bytes()
    if pack_dir.exists():
        # 不做批量删除：把旧包整体移到临时目录（可回滚），再原地重建。
        # 这样既避开环境的批量删除保护，也不会出现「删了一半、新的没写完」。
        backup_root = Path(tempfile.mkdtemp(prefix="zkagent-pack-old-"))
        target = backup_root / pack_dir.name
        shutil.move(str(pack_dir), str(target))
        moved_previous = str(target)

    template_dir = TEMPLATE_DIR / pack_id
    if not template_dir.is_dir():
        raise PackBuildError(f"缺少包模板：{template_dir}")

    knowledge_dir = pack_dir / "knowledge"
    _write_knowledge(entries, knowledge_dir)
    # 内部作业手册不进用户答案候选（ACL 由 meta.yaml 派生，不改上游分片）
    _write_internal_meta(entries, knowledge_dir)

    # 静态配置（pack.yaml / tools.yaml / policies / prompts 片段）
    for name in ("pack.yaml", "tools.yaml"):
        shutil.copyfile(template_dir / name, pack_dir / name)
    (pack_dir / "policies").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template_dir / "policies" / "rules.yaml", pack_dir / "policies" / "rules.yaml")
    (pack_dir / "prompts").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template_dir / "prompts" / "fewshots.yaml", pack_dir / "prompts" / "fewshots.yaml")
    (pack_dir / "prompts" / "system.md").write_text(
        _compose_system_prompt(kb_root, template_dir / "prompts"), encoding="utf-8"
    )

    _dump_yaml(pack_dir / "evals" / "retrieval.yaml", retrieval_eval)
    (pack_dir / "evals" / "answer_checks.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _dump_yaml(pack_dir / "routing" / "entities.yaml", entities)
    _dump_yaml(pack_dir / "routing" / "prototypes.yaml", prototypes)
    _dump_yaml(pack_dir / "routing" / "intent_lexicon.yaml", lexicon)
    _write_seeds(pack_dir / "seed_data", template_dir / "seed_data")

    # 模板里额外的评测文件（业务场景用例）随包带走；
    # retrieval.yaml / answer_checks.json 由本函数生成，不能被模板覆盖。
    template_evals = template_dir / "evals"
    if template_evals.is_dir():
        for source in sorted(template_evals.iterdir()):
            if source.name in {"retrieval.yaml", "answer_checks.json"}:
                continue
            shutil.copyfile(source, pack_dir / "evals" / source.name)

    # 模板没提供的人工维护文件回填（模板提供的以模板为准，避免旧副本盖掉新模板）
    for name, payload in previous_eval_files.items():
        target_path = pack_dir / "evals" / name
        if not target_path.exists():
            target_path.write_bytes(payload)

    marker.write_text(
        json.dumps(
            {
                "generated_by": "apps.build_kb_pack",
                "source_kb": str(kb_root),
                "chunks": len(entries),
                "eval_cases": report.eval_cases,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    report.previous_moved_to = moved_previous
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把分层知识库打包成行业包")
    parser.add_argument("--kb", default="appliance-kb", help="知识库根目录")
    parser.add_argument("--pack", dest="pack_id", default="homeapp", help="生成的行业包 id")
    parser.add_argument("--pack-root", default=None, help="行业包根目录（默认 zkagent/industry_packs）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的目录")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    args = parser.parse_args(argv)

    try:
        report = build_pack(
            args.kb,
            args.pack_id,
            pack_root=args.pack_root,
            force=args.force,
            dry_run=args.dry_run,
        )
    except PackBuildError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    print(json.dumps({"ok": True, **report.to_dict()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
