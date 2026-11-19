#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""知识库校验：把"契约"变成可执行的检查，而不是靠人记。

检查项：
  1  YAML 语法
  2  product.yaml 必填字段 / product_id 唯一 / 与目录一致
  3  specs 键必须在 spec_dictionary 中注册（防自创字段）
  4  每条参数必须带 source（防无源数据）
  5  政策类必须有 verified 字段（防把待确认写成确定）
  6  faq 的 id 唯一、必填字段齐全
  7  index 切片 frontmatter 的 source 必须指向真实存在的文件
  8  覆盖度报告：ready 型号缺哪些文件、缺哪些关键参数

用法:
  python tools/validate.py            # 只报告
  python tools/validate.py --strict   # 有 error 时退出码 1（CI 用）
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ERRORS, WARNS = [], []


def err(m):
    ERRORS.append(m)


def warn(m):
    WARNS.append(m)


def y(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def id_slug(s):
    """product_id 用：型号里可能有 '+'（西门子套装）等非 URL/文件名安全字符"""
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def check_yaml():
    for p in ROOT.rglob("*.yaml"):
        if "_build" in p.parts or "chunks" in p.parts:
            continue
        try:
            y(p)
        except Exception as e:  # noqa: BLE001
            err(f"[YAML] {p.relative_to(ROOT)}: {str(e).splitlines()[0]}")


PRODUCT_REQUIRED = ["product_id", "status", "appliance", "brand", "model", "model_slug", "source_docs"]
SPEC_ITEM_KEYS = {"value", "min", "max", "unit", "source", "note", "w", "h", "d", "standard", "confidence",
                  # 2026-10-23 新增：
                  #   cross_check_alt —— 业务方口径与本库一手资料冲突时的"另一套值"留档，
                  #                      必须同时带 status: 未采用，绝不允许拿去覆盖 value
                  #   type            —— 材质/工艺的细分类（如内胆的"0 涂层"），value 放可读名
                  "cross_check_alt", "type"}


def check_products():
    seen = {}
    dicts = {}
    for p in sorted((ROOT / "03_product").rglob("product.yaml")):
        rel = p.relative_to(ROOT)
        try:
            d = y(p)
        except Exception:  # noqa: BLE001
            continue
        if not d:
            err(f"[PRODUCT] {rel}: 文件为空")
            continue
        for k in PRODUCT_REQUIRED:
            if k not in d:
                err(f"[PRODUCT] {rel}: 缺少必填字段 {k}")
        pid = d.get("product_id")
        if pid in seen:
            err(f"[PRODUCT] product_id 重复 {pid}: {seen[pid]} vs {rel}")
        seen[pid] = str(rel)

        # id 与目录一致性
        expect = f"{d.get('brand')}-{id_slug(d.get('model_slug',''))}"
        if pid != expect:
            err(f"[PRODUCT] {rel}: product_id 应为 {expect}，实际 {pid}")
        if p.parent.name != d.get("model_slug"):
            warn(f"[PRODUCT] {rel}: 目录名 {p.parent.name} 与 model_slug {d.get('model_slug')} 不一致")

        if d.get("status") != "ready":
            continue

        ap = d.get("appliance")
        if ap not in dicts:
            dp = ROOT / "00_schema/spec_dictionary" / f"{ap}.yaml"
            dicts[ap] = set(((y(dp) or {}).get("keys") or {}).keys()) if dp.exists() else set()

        for fname in ("specs.yaml", "features.yaml", "install.yaml", "usage.yaml", "faults.yaml"):
            if not (p.parent / fname).exists():
                warn(f"[PRODUCT] {rel.parent}: status=ready 但缺少 {fname}")

        sp = p.parent / "specs.yaml"
        if sp.exists():
            s = y(sp) or {}
            for k, v in (s.get("specs") or {}).items():
                if k not in dicts[ap]:
                    err(f"[SPECS] {rel.parent}/specs.yaml: 键 {k} 未在 spec_dictionary/{ap}.yaml 注册")
                if v is None:
                    continue          # null 合法，表示说明书未给
                if not isinstance(v, dict):
                    err(f"[SPECS] {rel.parent}: {k} 应为对象（含 source）")
                    continue
                if "source" not in v:
                    err(f"[SPECS] {rel.parent}: {k} 缺少 source（不允许无源参数）")
                extra = set(v) - SPEC_ITEM_KEYS
                if extra:
                    warn(f"[SPECS] {rel.parent}: {k} 含非标准字段 {extra}")
    return seen


def check_policies():
    for p in sorted(list((ROOT / "05_brand_ref").rglob("*.yaml")) + list((ROOT / "06_platform_policy").rglob("*.yaml"))):
        try:
            d = y(p)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(d, dict):
            continue
        rel = p.relative_to(ROOT)
        if "policy_id" not in d:
            err(f"[POLICY] {rel}: 缺少 policy_id")
        if "verified" not in d:
            err(f"[POLICY] {rel}: 缺少 verified（政策类必须显式声明是否已核实）")
        # 金额口径（2026-10-23 调整，原因见 10_ops/changelog.md）：
        #   ① 品牌官方核实的单价 → 写 `price: 数字` + 文件级 `price_verified: true`
        #   ② 行业参考区间（本库目前全部是这种）→ 只能写 `price_min` / `price_max`，
        #      且文件必须有 `price_basis` 说明金额从哪来。
        #   分开的理由：客服可以答"大概 80~120 一米"，但不能把它说成"海尔官方标准价"，
        #   更不能被用户拿着一个具体数字主张"你们说过 90 元"。
        txt = p.read_text(encoding="utf-8")
        if re.search(r"^\s*price:\s*\d", txt, re.M) and d.get("price_verified") is not True:
            err(f"[POLICY] {rel}: 出现具体单价 price 但 price_verified 不为 true"
                f"（未核实的金额只能写 price_min/price_max 区间）")
        if re.search(r"^\s*price_m(?:in|ax):\s*\d", txt, re.M) and not d.get("price_basis"):
            err(f"[POLICY] {rel}: 有参考价格区间但缺 price_basis（必须说明金额来源，"
                f"否则客服会当成官方报价）")


def check_faq():
    ids = {}
    missing_pointer = []
    for p in sorted((ROOT / "04_scenario").glob("s*/faq.yaml")):
        d = y(p) or {}
        for item in (d.get("faq") or []):
            i = item.get("id")
            if not i:
                err(f"[FAQ] {p.relative_to(ROOT)}: 存在无 id 条目")
                continue
            if i in ids:
                err(f"[FAQ] id 重复 {i}: {ids[i]} vs {p.relative_to(ROOT)}")
            ids[i] = str(p.relative_to(ROOT))
            for k in ("question", "intent", "answer"):
                if not item.get(k):
                    err(f"[FAQ] {i}: 缺少 {k}")
            if item.get("verified") is None and "verified" not in item:
                warn(f"[FAQ] {i}: 未声明 verified，按未核实处理")
            if len(str(item.get("answer", ""))) > 400:
                warn(f"[FAQ] {i}: 答案 {len(item['answer'])} 字，建议拆分（在线场景超长）")
            # 单一事实来源：能唯一指向权威文件的 FAQ 必须只给「结论 + 指针」；
            # source 用通配符（`*` / `{brand}`）的条目是**跨型号通用知识**（如「首次使用
            # 先空烧一次」），它自己就是权威，不要求指针。
            source = str(item.get("source") or "")
            authoritative = source.startswith(
                ("03_product/", "05_brand_ref/", "06_platform_policy/"))
            wildcard = ("*" in source) or ("{" in source)
            if authoritative and not wildcard and "详见" not in str(item.get("answer", "")):
                missing_pointer.append(i)
    if missing_pointer:
        warn(f"[FAQ] {len(missing_pointer)} 条缺少权威指针（通配 source，迁移中）："
             f"{missing_pointer[:5]}")


def check_cards():
    """参数卡必须覆盖该型号 `specs.yaml` 里所有非空键。

    卡是参数的唯一权威（`00_schema/single_source.md`）；少了键就意味着
    「检索命中了卡，但卡里没有这个字段」——这种缺失只能靠答案级指标发现，
    所以在契约校验里挡掉。
    """
    mf = ROOT / "08_index/manifest.jsonl"
    if not mf.exists():
        warn("[CARD] manifest 不存在，跳过参数卡覆盖校验（先运行 tools/build_index.py）")
        return
    cards = {}
    for line in mf.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("card"):
            cards[str(row.get("product_id"))] = set(row.get("spec_keys") or [])
    for p in sorted((ROOT / "03_product").rglob("product.yaml")):
        d = y(p) or {}
        if d.get("status") != "ready":
            continue
        pid = str(d.get("product_id"))
        if pid not in cards:
            err(f"[CARD] {pid}: status=ready 但没有参数卡切片")
            continue
        specs = y(p.parent / "specs.yaml") or {}
        keys = {k for k, v in (specs.get("specs") or {}).items() if v is not None}
        missing = sorted(keys - cards[pid])
        if missing:
            err(f"[CARD] {pid}: 参数卡缺少 {missing}")


def check_index():
    mf = ROOT / "08_index/manifest.jsonl"
    if not mf.exists():
        warn("[INDEX] manifest 不存在，先运行 tools/build_index.py")
        return
    seen = set()
    for line in mf.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r["chunk_id"] in seen:
            err(f"[INDEX] chunk_id 重复 {r['chunk_id']}")
        seen.add(r["chunk_id"])
        src = (r.get("source") or "").split("#")[0].split("（")[0].strip()
        src = src.split("##")[0]
        if src and not (ROOT / src).exists():
            err(f"[INDEX] {r['chunk_id']} 的 source 不存在: {src}")


def check_single_source():
    """单一事实来源：同一个 `policy_id` 只允许一个切片。

    以前 `build_index.py` 按 `applies_to.appliance` 把一份政策展开成 5 个切片
    （`platform.return@air_conditioner` / `@washer` …），规则文本完全相同：
    同一事实多份表达，检索里互相竞争，gold 只认其中一个。
    契约见 `00_schema/single_source.md`。
    """
    mf = ROOT / "08_index/manifest.jsonl"
    if not mf.exists():
        warn("[SINGLE] manifest 不存在，跳过政策唯一性校验（先运行 tools/build_index.py）")
        return
    seen = {}
    for line in mf.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        pid = row.get("policy_id")
        if pid:
            seen.setdefault(str(pid), []).append(row["chunk_id"])
    for pid, chunk_ids in sorted(seen.items()):
        if len(chunk_ids) > 1:
            err(f"[SINGLE] policy_id {pid} 出现 {len(chunk_ids)} 个切片：{chunk_ids[:5]}")


def coverage():
    rows = []
    for p in sorted((ROOT / "03_product").rglob("product.yaml")):
        d = y(p) or {}
        have = [f for f in ("specs.yaml", "features.yaml", "install.yaml", "usage.yaml", "faults.yaml")
                if (p.parent / f).exists()]
        miss = []
        if (p.parent / "specs.yaml").exists():
            s = (y(p.parent / "specs.yaml") or {}).get("specs") or {}
            nulls = [k for k, v in s.items() if v is None]
            if nulls:
                miss.append("参数缺失: " + ", ".join(nulls))
        rows.append((d, have, miss))

    # 汇总：先看全局缺口分布，再翻明细。否则 28 个型号的明细里看不出"卡在哪"
    total = len(rows)
    ready = sum(1 for d, _, _ in rows if d.get("status") == "ready")
    ap_stat = {}
    ocr_stat = {"images": [], "full": [], "partial": [], "garbled": []}
    form_all = {"images": 0, "full": 0, "partial": 0, "garbled": 0, "none": 0}
    ocr_done = []
    for d, _, _ in rows:
        ap = d.get("appliance") or "?"
        a = ap_stat.setdefault(ap, {"n": 0, "ready": 0})
        a["n"] += 1
        if d.get("status") == "ready":
            a["ready"] += 1
        ms = d.get("material_status") or {}
        scope = ms.get("ocr_scope")
        if scope in form_all:
            form_all[scope] += 1        # 全部型号的形态分布，含已 OCR 的
        if scope not in ocr_stat:
            continue
        if ms.get("ocr_done"):
            ocr_done.append(d.get("model"))
        else:
            ocr_stat[scope].append(d.get("model"))

    lines = ["# 覆盖度报告", "",
             f"- 型号总数：{total}，ready {ready}，skeleton {total - ready}",
             "- 按品类：", ""]
    for ap in sorted(ap_stat):
        a = ap_stat[ap]
        lines.append(f"  - {ap}：{a['n']} 个，ready {a['ready']}")
    lines += ["", "- 资料形态分布（全部型号）：", ""]
    label = {"images": "纯图片（JPG/PNG，无 PDF 文本层）",
             "full": "PDF 全部为扫描页",
             "partial": "PDF 部分页为扫描页",
             "garbled": "PDF 文本层乱码（字体映射坏）",
             "none": "PDF 文本层可直接抽取（未走 OCR）"}
    for scope in ("images", "full", "partial", "garbled", "none"):
        lines.append(f"  - {label[scope]}：{form_all[scope]} 个")
    unknown = sum(form_all.values())
    if total - unknown:
        # 老型号身份卡里没写 ocr_scope，不能静默丢掉，否则报告会显得"资料形态很少"
        lines.append(f"  - 未标注形态：{total - unknown} 个（老身份卡没写 material_status.ocr_scope，属待补项）")
    lines += ["", "- 待 OCR（按形态分桶，0 就是都处理完了）：", ""]
    for scope in ("images", "full", "partial", "garbled"):
        ms = ocr_stat[scope]
        lines.append(f"  - {label[scope]}：{len(ms)} 个" + ("　" + "、".join(ms) if ms else ""))
    if ocr_done:
        lines.append(f"- 已完成 OCR：{len(ocr_done)} 个　" + "、".join(ocr_done))
    lines += ["", "---", "", "## 型号明细", ""]

    for d, have, miss in rows:
        lines.append(f"## {d.get('product_id')}  `{d.get('status')}`")
        lines.append(f"- 已建文件：{len(have)}/5（{', '.join(have) or '无'}）")
        ms = d.get("material_status") or {}
        if ms.get("ocr_scope"):
            # ocr_note 是人工备注，脚本回填的身份卡只有 ocr_scope；两者都缺才不显示，
            # 否则报告里会出现一堆「资料状态：None」，看不出这批资料到底处理到哪一步
            lines.append(f"- 资料状态：{ms.get('ocr_note') or ms.get('ocr_scope')}")
        for m in miss:
            lines.append(f"- {m}")
        lines.append("")
    out = ROOT / "10_ops/coverage.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    check_yaml()
    check_products()
    check_policies()
    check_faq()
    check_index()
    check_single_source()
    check_cards()
    cov = coverage()

    print(f"ERROR {len(ERRORS)} / WARN {len(WARNS)}")
    for e in ERRORS:
        print("  E " + e)
    for w in WARNS:
        print("  W " + w)
    print(f"覆盖度报告 -> {cov.relative_to(ROOT)}")
    if args.strict and ERRORS:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
