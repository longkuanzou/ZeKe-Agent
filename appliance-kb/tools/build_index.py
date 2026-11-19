#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""构建检索层：03_product / 04_scenario / 05_brand_ref / 06_platform_policy -> 08_index

产出：
  08_index/chunks/{doc_type}/{chunk_id}.md   带 frontmatter 的切片
  08_index/manifest.jsonl                    全量切片清单（向量化/召回的输入）
  08_index/_build/report.md                  构建报告（覆盖度、缺口统计）

设计原则：
  - 切片按"一个事实一条"，不按页切。参数一条一个切片，检索精度远高于整页切。
  - 溯源字段（source/source_page）必带，回答才能给出依据。
  - status != ready 的型号只产出 identity 切片，不产出参数切片（避免答出空值）。
"""
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_cards import load_names, render_card  # noqa: E402  （同目录工具脚本）

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "08_index"
CHUNKS = INDEX / "chunks"
MANIFEST = INDEX / "manifest.jsonl"

doc_type_dir = {
    "fact": "fact",
    "faq": "faq",
    "playbook": "playbook",
    "policy": "policy",
    "identity": "fact",
}


def load_yaml(p):
    try:
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] YAML 解析失败 {p}: {e}", file=sys.stderr)
        return None


def fmt(v):
    if isinstance(v, dict):
        if {"w", "h", "d"} <= set(v):
            return f"{v['w']}×{v['h']}×{v['d']}{v.get('unit') or ''}（宽×高×深）"
        if "value" in v and "min" in v:
            s = f"{v['value']}"
            if v.get("unit"):
                s += v["unit"]
            return f"{s}（{v.get('min')}~{v.get('max')}）"
        if "value" in v:
            return f"{v['value']}{v.get('unit') or ''}"
        if "min" in v and "max" in v:
            return f"{v['min']}~{v['max']}{v.get('unit') or ''}"
        if "min" in v and "standard" in v:
            return f"静音{v['min']}／高风{v.get('standard')}／超强{v.get('max')}{v.get('unit') or ''}"
    return str(v)


# 政策正文的键名中文映射：正文直接给模型看，`terms.duration.整机` 这种写法
# 检索时几乎匹配不到用户问法。键名转中文 + 一条规则一行，可读性与召回率同时提升。
POLICY_KEY_CN = {
    "cn": "名称",
    "rule": "规定",
    "window": "时间窗",
    "conditions": "条件",
    "exclusions": "除外",
    "note": "备注",
    "appliance_note": "品类说明",
    "terms": "条款",
    "duration": "期限",
    "start_from": "起算",
    "start_fallback": "无凭证时",
    "require_documents": "所需材料",
    "not_free_service": "不予包修",
    "deduction": "扣除",
    "legal_basis": "法律依据",
    "rules": "规则",
    "answer_rules": "回答纪律",
    "todo": "待确认",
    "states": "状态分级",
    "judgement": "判定",
    "returnable": "可否退",
    "caveat": "注意",
    "next": "下一步",
    "tables": "分品类表",
    "codes": "代码表",
    "price": "价格",
    "price_min": "最低价",
    "price_max": "最高价",
    "price_verified": "价格已核实",
    "verified_legal": "法条已核实",
    "verified_platform_detail": "平台细则已核实",
    "severity": "严重度",
    "triage": "判定链",
    "self_help": "可自查",
    "escalate_when": "升级条件",
    "user_phrases": "用户说法",
    "explain": "解释",
    "included": "已含",
    "excluded": "另收",
    "free": "免费",
    "amount": "金额",
    "unit": "单位",
}

#: 正文标题层已经表达过的键，不再重复渲染
POLICY_SKIP_KEYS = {
    "policy_id",
    "brand",
    "applies_to",
    "summary",
    "category",
    "title",
    "aliases",
    "updated",
    "verified",
    "effective_from",
    "source",
    "source_kind",
    "price_basis",
}


def _cn_key(key):
    return POLICY_KEY_CN.get(str(key), str(key))


def render_policy_value(value, indent=0):
    """把政策 YAML 递归渲染成「中文键：值」的可读正文。

    列表里的字典（规则/状态/条款）压成一行一条，避免一条规则被拆成七八行
    key: value —— 那种正文既不像人话，也切不出一段完整语义。
    """
    pad = "  " * indent
    lines = []
    if isinstance(value, dict):
        for key, sub in value.items():
            if isinstance(sub, (dict, list)):
                lines.append(f"{pad}{_cn_key(key)}：")
                lines.extend(render_policy_value(sub, indent + 1))
            else:
                lines.append(f"{pad}{_cn_key(key)}：{fmt(sub)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                head = item.get("cn") or item.get("id") or item.get("rule") or ""
                parts = [
                    f"{_cn_key(k)}：{fmt(v)}"
                    for k, v in item.items()
                    if k not in ("cn", "id") and not isinstance(v, (dict, list))
                ]
                lines.append(f"{pad}- {head}" + ("；" + "；".join(parts) if parts else ""))
                for k, v in item.items():
                    if isinstance(v, (dict, list)):
                        lines.append(f"{pad}  {_cn_key(k)}：")
                        lines.extend(render_policy_value(v, indent + 2))
            else:
                lines.append(f"{pad}- {fmt(item)}")
    else:
        lines.append(f"{pad}{fmt(value)}")
    return lines


def load_faq_questions():
    """FAQ 的问法 → 权威文档（型号 / policy_id）。

    FAQ 不再作为**答案候选**（售前、售后的 FAQ 切片会在 `build_scenarios` 里跳过），
    但用户的口语问法必须保留：把它们拼进参数卡的「用户常问」和政策的「用户常问」，
    否则「保修几年」匹配不到「包修 6 年」。
    """
    policy_by_file = {}
    for policy_path in (sorted((ROOT / "05_brand_ref").rglob("*.yaml"))
                        + sorted((ROOT / "06_platform_policy").glob("*.yaml"))):
        data = load_yaml(policy_path) or {}
        if isinstance(data, dict) and data.get("policy_id"):
            policy_by_file[str(policy_path.relative_to(ROOT))] = data["policy_id"]

    by_product: dict[str, list[str]] = {}
    by_policy: dict[str, list[str]] = {}
    by_slice: dict[tuple[str, str, str], list[str]] = {}
    for path in sorted((ROOT / "04_scenario").glob("s*/faq.yaml")):
        data = load_yaml(path) or {}
        for item in data.get("faq") or []:
            question = str(item.get("question") or "").strip()
            if not question:
                continue
            for model in as_list((item.get("scope") or {}).get("model")):
                by_product.setdefault(str(model), []).append(question)
            for part in re.split(r"\s+/\s+|,\s*", str(item.get("source") or "")):
                base = part.split("#", 1)[0].strip()
                policy_id = policy_by_file.get(base)
                if policy_id:
                    by_policy.setdefault(policy_id, []).append(question)
            # 指针型 FAQ（答案在某个型号文档里）的问法要落到那条 fact 切片上，
            # 否则 FAQ 被移出索引后，这条问法就彻底检索不到了。
            for match in re.finditer(
                r"详见 (\S+) 的 (specs|usage|install|features|faults)\.yaml(?:（([^）]+)）)?",
                str(item.get("answer") or ""),
            ):
                pid, kind = match.group(1), match.group(2)
                section = str(match.group(3) or "").split(".", 1)[0]
                by_slice.setdefault((pid, kind, section), []).append(question)
    return by_product, by_policy, by_slice


def load_alias_table():
    """读取 `00_schema/aliases.yaml`（政策 / 参数字段 / 安装小节 → 用户口语）。"""
    path = ROOT / "00_schema/aliases.yaml"
    if not path.exists():
        return {}
    return load_yaml(path) or {}


def load_policy_aliases():
    """政策 → 用户常用问法（只取政策相关两段）。"""
    data = load_alias_table()
    return {"by_suffix": data.get("by_suffix") or {}, "by_policy": data.get("by_policy") or {}}


def policy_aliases(policy_id, table):
    """精确命中优先，其次按 `policy_id` 的最后一段（`warranty` / `fee_schedule` …）。"""
    by_policy = table.get("by_policy") or {}
    if policy_id in by_policy:
        return [str(x) for x in (by_policy[policy_id] or [])]
    suffix = str(policy_id).rsplit(".", 1)[-1]
    words = (table.get("by_suffix") or {}).get(suffix) or []
    return [str(x) for x in words]


def render_policy(data, aliases=None):
    """政策正文：一句要点 + 适用范围 + 用户常问 + 逐条规则。

    不再 `yaml.safe_dump` 整份文件：YAML 里的 `terms.duration.整机` 既不含
    「保修」也不含「几年」，同义 FAQ 一排上来它就没机会。
    """
    lines = []
    title = data.get("summary") or data.get("category") or data.get("title") or data.get("policy_id")
    if title:
        lines.append(str(title))
    appliances = as_list((data.get("applies_to") or {}).get("appliance"))
    if appliances:
        lines.append("适用品类：" + "、".join(appliances))
    if data.get("brand"):
        lines.append(f"品牌：{data['brand']}")
    words = as_list(aliases if aliases is not None else data.get("aliases"))
    if words:
        lines.append("用户常问：" + "、".join(words))
    rest = {k: v for k, v in data.items() if k not in POLICY_SKIP_KEYS}
    lines.extend(render_policy_value(rest))
    return "\n".join(lines).strip()


def load_dict(appliance):
    """参数键 -> 中文名，检索时用中文名切词，别让用户搜'制冷量'却只匹配到英文键"""
    p = ROOT / "00_schema/spec_dictionary" / f"{appliance}.yaml"
    if not p.exists():
        return {}
    d = load_yaml(p) or {}
    return {k: (v or {}).get("cn", k) for k, v in (d.get("keys") or {}).items()}


def load_feature_names(appliance):
    """功能键 -> 中文名。功能键同样是英文，不映射的话用户搜'烘干'匹配不到 feature key dry"""
    p = ROOT / "00_schema/spec_dictionary" / f"{appliance}.yaml"
    if not p.exists():
        return {}
    return (load_yaml(p) or {}).get("feature_names") or {}


# install/usage 里这些键是给人和校验器看的元信息，不是可回答的内容
META_KEYS = {"missing", "notes", "updated", "product_id", "source", "not_applicable", "sold_with"}


def iter_blocks(data):
    """遍历 install/usage 的一级小节。不写死小节名——
    空调是 electrical/space/bracket/piping，洗衣机多了 water/drain/transport，
    写死名单的下场是洗衣机最关键的"排水"整段被静默丢掉。"""
    for k, v in (data or {}).items():
        if k in META_KEYS or not v:
            continue
        yield k, v


def render_block(block):
    lines = []
    if isinstance(block, dict):
        for k, v in block.items():
            if isinstance(v, dict):
                val = v.get("text") or v.get("note")
                if val is None and v.get("value") is not None:
                    val = v.get("value")
                if val is None:
                    val = fmt(v)
                src = f"（{v.get('source')}）" if v.get("source") else ""
                lines.append(f"- {k}: {val}{src}")
            elif isinstance(v, list):
                lines.append(f"- {k}: " + "；".join(str(x) for x in v))
            else:
                lines.append(f"- {k}: {v}")
    elif isinstance(block, list):
        for it in block:
            lines.append(f"- {fmt(it) if isinstance(it, (dict, list)) else it}")
    else:
        lines.append(str(block))
    return lines


def page_of(src):
    if not src:
        return None
    m = re.search(r"p(\d+)", str(src))
    return int(m.group(1)) if m else None


class Builder:
    def __init__(self):
        self.rows = []

    def add(self, *, chunk_id, doc_type, scenario, text, product_id=None,
            appliance=None, brand=None, model=None, intent=None,
            source=None, verified=True, extra=None):
        meta = {
            "chunk_id": chunk_id,
            "doc_type": doc_type,
            "scenario": scenario,
            "product_id": product_id,
            "appliance": appliance,
            "brand": brand,
            "model": model,
            "intent": intent,
            "source": (str(source).replace("\\", "/") if source else None),
            "source_page": page_of(source),
            "verified": bool(verified),
        }
        meta.update(extra or {})
        body = f"---\n{yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()}\n---\n\n{text.strip()}\n"
        out = CHUNKS / doc_type_dir.get(doc_type, "misc") / f"{chunk_id}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
        self.rows.append({**meta, "path": str(out.relative_to(ROOT)).replace("\\", "/"),
                          "chars": len(text)})


# ── 03_product ────────────────────────────────────────────
def build_products(b):
    faq_asks, _faq_policy, faq_by_slice = load_faq_questions()
    alias_table = load_alias_table()
    spec_aliases = alias_table.get("spec_keys") or {}
    section_aliases = alias_table.get("sections") or {}
    for prod_path in sorted((ROOT / "03_product").rglob("product.yaml")):
        p = load_yaml(prod_path)
        if not p:
            continue
        d = prod_path.parent
        pid = p["product_id"]
        ready = p.get("status") == "ready"

        b.add(
            chunk_id=f"{pid}@id#001", doc_type="identity", scenario=None,
            product_id=pid, appliance=p.get("appliance"), brand=p.get("brand"),
            model=p.get("model"),
            text=(f"{p.get('brand_cn','')} {p.get('model','')}（{p.get('product_type','')}），"
                  f"口语叫法 {p.get('colloquial') or '未标注'}。"
                  f"适用于 {p.get('appliance_cn','')} 品类的参数/安装/使用/故障类问题。"),
            source=str(prod_path.relative_to(ROOT)), verified=ready,
            extra={"status": p.get("status")},
        )
        if not ready:
            continue

        # 参数 + 功能：一型号一张卡（单一事实来源，见 00_schema/single_source.md）
        # 一字段一切片时，一个型号有 26~72 篇同构短句；卡片只需命中一次。
        sp = load_yaml(d / "specs.yaml") or {}
        ft = load_yaml(d / "features.yaml") or {}
        keys_cn, features_cn = load_names(p.get("appliance"))
        card = render_card(
            p, sp, ft, keys_cn, features_cn, faq_asks.get(str(pid), []),
            spec_aliases=spec_aliases,
        )
        spec_keys = [k for k, v in (sp.get("specs") or {}).items() if v is not None]
        b.add(
            chunk_id=f"{pid}@s1#params", doc_type="fact", scenario="s1_presale",
            product_id=pid, appliance=p.get("appliance"), brand=p.get("brand"),
            model=p.get("model"), intent="presale.spec", text=card,
            source=f"{d.relative_to(ROOT)}/specs.yaml",
            verified=True,
            extra={"spec_keys": spec_keys, "card": True},
        )

        # 安装：按一级小节切（小节名不写死，见 iter_blocks）
        ins = load_yaml(d / "install.yaml") or {}
        for sec, block in iter_blocks(ins):
            b.add(chunk_id=f"{pid}@s2#{sec}", doc_type="fact", scenario="s2_install",
                  product_id=pid, appliance=p.get("appliance"), brand=p.get("brand"),
                  model=p.get("model"), intent="install.feasibility",
                  text="\n".join([f"{p.get('model')} 的安装条件 - {sec}"]
                                 + _asks_line(faq_by_slice.get((str(pid), "install", sec)), section_aliases.get(sec))
                                 + render_block(block)),
                  source=f"{d.relative_to(ROOT)}/install.yaml#{sec}")

        # 使用
        us = load_yaml(d / "usage.yaml") or {}
        for sec, block in iter_blocks(us):
            b.add(chunk_id=f"{pid}@s3#{sec}", doc_type="fact", scenario="s3_usage",
                  product_id=pid, appliance=p.get("appliance"), brand=p.get("brand"),
                  model=p.get("model"), intent="usage.operation",
                  text="\n".join([f"{p.get('model')} 的使用说明 - {sec}"]
                                 + _asks_line(faq_by_slice.get((str(pid), "usage", sec)), section_aliases.get(sec))
                                 + render_block(block)),
                  source=f"{d.relative_to(ROOT)}/usage.yaml#{sec}")

        # 故障：正常现象 + 代码规则
        fa = load_yaml(d / "faults.yaml") or {}
        for i, item in enumerate((fa.get("normal_phenomena") or []), start=1):
            if not isinstance(item, dict):
                item = {"id": f"np{i:02d}", "symptom": str(item), "explain": ""}
            b.add(chunk_id=f"{pid}@s4#{item['id']}", doc_type="fact", scenario="s4_troubleshoot",
                  product_id=pid, appliance=p.get("appliance"), brand=p.get("brand"),
                  model=p.get("model"), intent="troubleshoot.symptom",
                  text=f"{item.get('symptom')}：{item.get('explain')}（属正常现象，非故障）",
                  source=f"{d.relative_to(ROOT)}/faults.yaml#{item['id']}")
        codes = fa.get("codes") or {}
        if codes.get("rule") or codes.get("note"):
            b.add(chunk_id=f"{pid}@s4#codes", doc_type="fact", scenario="s4_troubleshoot",
                  product_id=pid, appliance=p.get("appliance"), brand=p.get("brand"),
                  model=p.get("model"), intent="troubleshoot.code",
                  text="故障代码处置：" + ("；".join(codes["rule"]) if isinstance(codes.get("rule"), list) else str(codes.get("rule") or codes.get("note"))),
                  source=f"{d.relative_to(ROOT)}/faults.yaml#codes",
                  verified=codes.get("has_code_display") is not None)


def _asks_line(questions, extra=()) -> list[str]:
    """把 FAQ 的问法（用户口语）拼进 fact 切片。

    指针型 FAQ 已从索引里移出（答案在权威文档），问法不能跟着丢：否则用户说
    「滤网怎么拆下来洗」就再也匹配不到 `usage.yaml#maintenance`。
    """
    words = [str(item).strip() for item in (questions or []) if str(item).strip()]
    words += [str(item).strip() for item in (extra or []) if str(item).strip()]
    return [f"用户常问：{'、'.join(dict.fromkeys(words))}"] if words else []


def as_list(v):
    """appliance 字段可能是字符串也可能是列表；统一成列表，None -> []"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [x for x in v if x]


# ── 04_scenario ──────────────────────────────────────────
def _render_slots(scenario: str, appliance: str, block: dict, common: dict) -> str:
    """把 slots.yaml 的一个品类块渲染成可检索文本。

    槽位表原本整份没进索引，12 条题因此「找不到出处」被跳过；它同时承载「判定表 +
    追问话术 + 收费项」，是安装/报修场景真正的答案依据，必须能被检索到。
    """
    label = {"s2_install": "安装条件与追问", "s6_service": "报修建单与追问"}.get(
        scenario, "槽位与追问"
    )
    lines = [f"{appliance} 的{label}"]

    def emit(key, value):
        if isinstance(value, dict):
            pairs = "；".join(f"{k}={_flat(v)}" for k, v in value.items())
            lines.append(f"{key}：{pairs}")
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            lines.append(f"{key}：")
            for item in value:
                lines.append(f"- {_flat(item.get('rule') or item)}")
        elif isinstance(value, list):
            lines.append(f"{key}：{'、'.join(_flat(x) for x in value)}")
        else:
            lines.append(f"{key}：{_flat(value)}")

    if common:
        emit("通用要求", common)
    for key, value in block.items():
        if key == "prompts":
            lines.append("追问话术：")
            for sub, text in value.items():
                lines.append(f"- {sub}：{_flat(text)}")
        elif key == "key_rules":
            lines.append("关键规则：")
            for rule in value:
                lines.append(f"- {_flat(rule.get('rule') or rule)}")
        else:
            emit(key, value)
    return "\n".join(lines)


def _flat(value) -> str:
    if isinstance(value, (list, tuple)):
        return "、".join(_flat(x) for x in value)
    if isinstance(value, dict):
        return "；".join(f"{k}={_flat(v)}" for k, v in value.items())
    return str(value).strip()


def build_scenarios(b):
    for faq_path in sorted((ROOT / "04_scenario").glob("s*/faq.yaml")):
        sc = faq_path.parent.name
        # 售前 / 售后 FAQ 只是「问法」：已拼进参数卡与政策的「用户常问」，
        # 不再作为独立答案候选——否则 `Q:` 行与用户问句逐字相同，永远压住权威文档
        # （实测参数卡排第 3、政策排第 3，MRR 上不去）。
        if sc in {"s1_presale", "s5_aftersale"}:
            continue
        data = load_yaml(faq_path) or {}
        for item in (data.get("faq") or []):
            # 指针型 FAQ（「详见 X 的 usage.yaml（maintenance）」）只是入口：
            # 答案在权威文档里，问法已由 `_asks_line` 拼进那条 fact 切片。
            # 留在这里会永远压住权威文档（`Q:` 行与用户问句逐字相同）。
            # 只有「详见 X 的 usage.yaml（section）」这种**带小节**的指针才移出：
            # 没有小节就无法定位到具体的 fact 切片，移出会让这条题彻底失去 gold。
            if re.search(
                r"详见 \S+ 的 (specs|usage|install|features|faults)\.yaml（",
                str(item.get("answer") or ""),
            ):
                continue
            scope = item.get("scope") or {}
            models = as_list(scope.get("model"))
            brands = as_list(scope.get("brand"))
            aps = as_list(scope.get("appliance"))
            # 一条 FAQ 一个切片：多品类 / 多品牌用列表表达，**不再按品类展开副本**。
            # 展开会让「同一条退换规则」变成 3 份切片互相竞争（single_source.md）。
            # 单值仍写进 appliance/brand 便于收窄；多值置 None（不误删通用文档）。
            b.add(chunk_id=f"global@{sc.split('_')[0]}#{item['id']}", doc_type="faq",
                  scenario=sc, intent=item.get("intent"),
                  appliance=aps[0] if len(aps) == 1 else None,
                  brand=brands[0] if len(brands) == 1 else None,
                  model=models[0] if len(models) == 1 else None,
                  text=f"Q: {item['question']}\nA: {item['answer']}",
                  source=f"{faq_path.relative_to(ROOT)}#{item['id']}",
                  verified=item.get("verified", True),
                  extra={"faq_id": item["id"], "scope_models": models, "brands": brands,
                         "pointer": "详见" in str(item.get("answer") or ""),
                         "handoff": (item.get("handoff") or {}).get("needed")})

    # 剧本：playbook.md 是通用剧本，by_appliance/<品类>.md 是该品类的差异剧本
    # （空调的"三查"是电/位/管，洗衣机是电/水/排水/门宽，通用剧本里写不下）
    md_files = [(p, None) for p in sorted((ROOT / "04_scenario").glob("s*/playbook.md"))]
    md_files += [(p, p.stem) for p in sorted((ROOT / "04_scenario").glob("s*/by_appliance/*.md"))]

    for pb, ap in md_files:
        sc = pb.parent.name if ap is None else pb.parent.parent.name
        key = "pb" if ap is None else f"pb-{ap}"
        text = pb.read_text(encoding="utf-8")
        for i, sec in enumerate(re.split(r"\n## ", text)[1:], start=1):
            title, _, body = sec.partition("\n")
            b.add(chunk_id=f"global@{sc.split('_')[0]}#{key}{i:02d}", doc_type="playbook",
                  scenario=sc, intent=None, appliance=ap,
                  text=f"{title}\n{body}".strip(),
                  source=f"{pb.relative_to(ROOT)}##{title}",
                  extra={"section": title.strip()})

    # 现象主库：按条切，按 appliance 展开
    # 同一现象可能适用多个品类（触电、焦味、报代码），每个品类一条切片，
    # 否则按 appliance 过滤时会漏掉这些跨品类条目。
    sym = load_yaml(ROOT / "04_scenario/s4_troubleshoot/symptoms.yaml") or {}
    for i, s in enumerate(sym.get("symptoms") or [], start=1):
        parts = [f"现象：{s.get('cn')}", f"用户说法：{'、'.join(s.get('user_phrases') or [])}"]
        if s.get("explain"):
            parts.append(f"解释：{s['explain']}")
        if s.get("triage"):
            parts.append("判定链：" + "；".join(
                f"{t.get('check')}→{t.get('if_hit')}" for t in s["triage"]))
        if s.get("self_help"):
            parts.append("可自查：" + "、".join(s["self_help"]))
        if s.get("escalate_when"):
            parts.append("升级条件：" + "、".join(s["escalate_when"]))
        text = "\n".join(parts)
        aps = as_list(s.get("appliance"))
        # 同一条现象适用多个品类时也**不再展开副本**：文本完全相同，展开只是让
        # 同一事实在候选池里多几份（single_source.md）。单品类保留 appliance。
        b.add(chunk_id=f"global@s4#sym{i:02d}", doc_type="fact",
              scenario="s4_troubleshoot",
              appliance=aps[0] if len(aps) == 1 else None,
              intent="troubleshoot.symptom",
              text=text,
              source=f"04_scenario/s4_troubleshoot/symptoms.yaml#{s['id']}",
              extra={"symptom_id": s.get("id"), "severity": s.get("severity"),
                     "applies_to_appliance": aps})


# ── 05/06 政策 ────────────────────────────────────────────
def build_policies(b):
    """政策切片：**一个 policy_id 只产出一个切片**（`00_schema/single_source.md`）。

    以前按 `applies_to.appliance` 展开成 `@air_conditioner` / `@washer` …，同一份规则
    在候选池里自我竞争，gold 只认其中一个；现在统一 `appliance=None`——按品类过滤时
    它属于「无归属的通用文档」，仍然始终保留（与 `metadata.filter_visible` 语义一致）。
    """
    alias_table = load_policy_aliases()
    _faq_product, faq_asks, _faq_slice = load_faq_questions()
    for group, pat in (("05_brand_ref", "*/warranty.yaml"), ("05_brand_ref", "*/fee_schedule.yaml")):
        for path in sorted((ROOT / group).glob(pat)):
            data = load_yaml(path)
            if not data:
                continue
            pid = data.get("policy_id") or path.stem
            b.add(chunk_id=f"global@policy#{pid.replace('.', '_')}",
                  doc_type="policy", scenario="s5_aftersale",
                  brand=data.get("brand"), appliance=None,
                  intent="aftersale.warranty" if "warranty" in path.name else "install.fee",
                  text=render_policy(data, policy_aliases(pid, alias_table) + faq_asks.get(pid, [])),
                  source=str(path.relative_to(ROOT)),
                  verified=data.get("verified", False),
                  extra={"policy_id": pid,
                         "applies_to_appliance": as_list(
                             (data.get("applies_to") or {}).get("appliance"))})

    for path in sorted(list((ROOT / "05_brand_ref").glob("*/fault_codes.yaml"))
                       + list((ROOT / "05_brand_ref").glob("*/brand.yaml"))
                       + list((ROOT / "05_brand_ref").glob("*/parts_catalog.yaml"))
                       + list((ROOT / "06_platform_policy").glob("*.yaml"))):
        data = load_yaml(path)
        if not data:
            continue
        pid = data.get("policy_id") or path.stem
        scen = "s5_aftersale" if "platform" in str(path) else "s4_troubleshoot"
        # fault_codes 可能分品类建表（midea 是 tables.<appliance>），brand 用 applies_to，
        # brand.yaml 用 appliance_scope。三种写法都要认——但只用于**标注适用范围**，
        # 不再据此拆切片（拆分就是同一事实的多份表达）。
        aps = (as_list((data.get("applies_to") or {}).get("appliance"))
               or as_list(data.get("appliance_scope"))
               or as_list(list((data.get("tables") or {}).keys())))
        b.add(chunk_id=f"global@policy#{pid.replace('.', '_')}", doc_type="policy",
              scenario=scen, brand=data.get("brand"), appliance=None,
              text=render_policy(data, policy_aliases(pid, alias_table) + faq_asks.get(pid, [])),
              source=str(path.relative_to(ROOT)), verified=data.get("verified", False),
              extra={"policy_id": pid, "applies_to_appliance": aps})



def main():
    # `--prune`：不限数量清理孤儿切片。默认阈值 40 是为了兼容 Windows（一次删几百个
    # 文件会被安全策略拦），但重构过切法之后孤儿会有近千个，光靠默认阈值永远清不掉——
    # 那时显式传 `--prune` 跑一次即可。
    prune = "--prune" in sys.argv[1:]

    # 不做"先清空再重建"。
    # 原因：chunks 有 500+ 个文件，一次删 500 个在 Windows 上会被安全策略拦，
    # 而且中途失败会留下半残目录，比留几个孤儿文件糟得多。
    # 现在改成：原地覆盖写入 -> 只清理本次没有生成的孤儿文件（数量超阈值就只报告不删）。
    stale_before = {}
    if CHUNKS.exists():
        for f in CHUNKS.rglob("*.md"):
            stale_before[f] = True

    b = Builder()
    build_products(b)
    build_scenarios(b)
    build_policies(b)

    written = set()
    for r in b.rows:
        written.add((ROOT / r["path"]).resolve())
    orphans = [f for f in stale_before if f.resolve() not in written]
    removed = 0
    if len(orphans) <= 40 or prune:
        for f in orphans:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    elif orphans:
        print(f"[warn] 有 {len(orphans)} 个孤儿切片未删除（超批量删除阈值），"
              f"不影响检索（检索只认 manifest.jsonl），需要时用 `--prune` 清理 {CHUNKS}")

    with open(MANIFEST, "w", encoding="utf-8") as f:
        for r in b.rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 报告
    from collections import Counter
    by_type = Counter(r["doc_type"] for r in b.rows)
    by_scen = Counter(str(r["scenario"]) for r in b.rows)
    unverified = [r for r in b.rows if not r["verified"]]
    rep = ["# 索引构建报告", "",
           f"- 切片总数：{len(b.rows)}",
           f"- 按类型：{dict(by_type)}",
           f"- 按场景：{dict(by_scen)}",
           f"- 未验证（verified=false）切片：{len(unverified)}",
           f"- 清理孤儿切片：{removed}", "",
           "## 未验证切片清单", ""]
    for r in unverified:
        rep.append(f"- `{r['chunk_id']}` ← {r['source']}")
    out = INDEX / "_build"
    out.mkdir(exist_ok=True)
    (out / "report.md").write_text("\n".join(rep) + "\n", encoding="utf-8")

    print(f"切片 {len(b.rows)} 条 -> {CHUNKS}")
    print(f"清单 -> {MANIFEST}")
    print(f"报告 -> {out/'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
