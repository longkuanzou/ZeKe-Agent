#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""型号卡脚手架：01_source 里有的型号，03_product 里必须有卡

解决的问题：新品类加进来后，容易漏建卡，或者建得字段不一。
本脚本只做"身份识别"这一层（品类/品牌/型号/形态/资料状态），参数一律不猜。

判定依据：
  - 形态（滚筒/波轮/干衣机/电饭煲）来自型号编码惯例，标注 product_type_source
  - 容量/容积仅在同品牌型号编码明确编码时才填，并标 inferred
  - ocr_needed 取自 02_ingest/{...}/report.json（无文本层 / 有扫描页）

用法:
  python tools/scaffold_products.py            # 只补缺失的卡
  python tools/scaffold_products.py --force    # 覆盖（会丢手改内容，慎用）
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "01_source"
PROD = ROOT / "03_product"
ING = ROOT / "02_ingest"

BRAND_CN = {"haier": "海尔", "midea": "美的", "supor": "苏泊尔",
            "littleswan": "小天鹅", "siemens": "西门子"}
AP_CN = {"air_conditioner": "空调", "washer": "洗衣机", "rice_cooker": "电饭煲",
         "refrigerator": "冰箱", "microwave": "微波炉"}

# 型号编码 → (形态, 容量/容积)，只收编码里有明确含义的
WASHER_RULES = [
    (r"^WN", "洗衣干衣机（滚筒）", None),
    (r"^WQ", "干衣机（滚筒）", None),
    (r"^W[GM]", "滚筒洗衣机", None),
    (r"^TG(100|10)", "滚筒洗衣机", 10),
    (r"^TB(100|10)", "波轮洗衣机", 10),
    (r"^TB(\d{2})", "波轮洗衣机", None),
]
COOKER_RULES = [
    (r"^MB-.*?(\d{2})", "电饭煲", None),
    (r"^S?F(\d{2})", "电饭煲", None),
]

# 型号编码推不出来的特例，人工核对后写在这里，避免每次 --force 都丢掉
OVERRIDES = {
    "siemens/WQ53E2D01W+WQ53E2D11W": {
        "product_type": "干衣机套装（两台）",
        "notes": [
            "型号含 '+'：是 WQ53E2D01W 与 WQ53E2D11W 两台干衣机的套装，共用一份说明书",
            "product_id 用 '-' 拼接（URL/文件名安全），目录名保留原始型号中的 '+'",
            "回答参数类问题时若两台参数不同，必须分别说明，不得合并成一个数",
        ],
    },
}


def guess(appliance, model):
    """返回 (形态, 容量, 容量单位, 依据说明)"""
    if appliance == "washer":
        for pat, t, cap in WASHER_RULES:
            if re.match(pat, model):
                note = "型号编码惯例"
                c = cap if cap else None
                return t, c, "kg", note
        return None, None, None, None
    if appliance == "rice_cooker":
        m = re.match(r"^MB-?[A-Z]{0,3}(\d{2})", model)
        if m:
            d = int(m.group(1))
            # 电饭煲型号里的 40/50 通常表示 4L/5L
            if 30 <= d <= 60 and d % 10 == 0:
                return "电饭煲", d / 10, "L", "型号编码推断（40→4L）"
        m = re.match(r"^S?F(\d{2})", model)
        if m:
            d = int(m.group(1))
            if 30 <= d <= 60 and d % 10 == 0:
                return "电饭煲", d / 10, "L", "型号编码推断（40→4L）"
        return "电饭煲", None, None, "型号编码惯例"
    return None, None, None, None


def slug(model):
    """型号 → 可用作 ID/路径的 slug。西门子套装型号含 '+'，会污染 chunk_id 与 URL。"""
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def report(appliance, brand, model):
    p = ING / appliance / brand / model / "report.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def ocr_scope(rep):
    """ocr_needed 是三态的，todo 文案也得跟着变，否则会误导读资料的人"""
    imgs = rep.get("images_pending_ocr") or []
    scanned = rep.get("scanned_pages") or []
    chars = rep.get("total_chars", 0)
    if rep.get("text_garbled"):
        return "garbled", f"文本层损坏（字体映射错误），{len(scanned)} 页需整本 OCR"
    if imgs:
        return "images", f"{len(imgs)} 张图片资料无文本层"
    if scanned and not chars:
        return "full", f"{len(scanned)} 页全部为扫描页"
    if scanned:
        return "partial", f"{len(scanned)}/{sum(s.get('pages', 0) for s in rep.get('sources', []))} 页为扫描页"
    return None, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="覆盖已存在的卡；status: ready 的卡即使加 --force 也不覆盖")
    ap.add_argument("--appliance", nargs="*", default=None,
                    help="只处理指定品类，如 --appliance washer rice_cooker")
    args = ap.parse_args()

    made = skipped = blocked = 0
    for ap_dir in sorted([d for d in SRC.iterdir() if d.is_dir() and not d.name.startswith("_")]):
        appliance = ap_dir.name
        if args.appliance and appliance not in args.appliance:
            continue
        for brand_dir in sorted([d for d in ap_dir.iterdir() if d.is_dir()]):
            brand = brand_dir.name
            for model_dir in sorted([d for d in brand_dir.iterdir() if d.is_dir()]):
                model = model_dir.name
                out = PROD / appliance / brand / model / "product.yaml"
                if out.exists():
                    if not args.force:
                        skipped += 1
                        continue
                    # 已完工的卡是人工回填的，脚本无权覆盖
                    if "status: ready" in out.read_text(encoding="utf-8"):
                        blocked += 1
                        print(f"[skip ] {appliance}/{brand}/{model}  status=ready，拒绝覆盖")
                        continue
                out.parent.mkdir(parents=True, exist_ok=True)

                ptype, cap, cap_unit, tsrc = guess(appliance, model)
                ov = OVERRIDES.get(f"{brand}/{model}", {})
                if ov.get("product_type"):
                    ptype, tsrc = ov["product_type"], "人工核对"
                rep = report(appliance, brand, model)
                files = [s.get("file") for s in rep.get("sources", [])] + (rep.get("images_pending_ocr") or [])
                ocr = rep.get("ocr_needed", True)
                characters = rep.get("total_chars", 0)
                pages = sum(s.get("pages", 0) for s in rep.get("sources", []))
                scope, scope_note = ocr_scope(rep)

                lines = [
                    f"product_id: {brand}-{slug(model)}",
                    "status: skeleton",
                    f"appliance: {appliance}",
                    f"appliance_cn: {AP_CN.get(appliance, appliance)}",
                    f"brand: {brand}",
                    f"brand_cn: {BRAND_CN.get(brand, brand)}",
                    f"model: {model}",
                    f"model_slug: {model}",
                    "indoor_model: null",
                    "outdoor_model: null",
                    f"product_type: {ptype or 'null'}",
                    f"product_type_source: {tsrc or 'null'}",
                    "colloquial: null",
                    "energy_grade: null",
                    "launch_year: null",
                    "updated: 2026-10-22",
                    "",
                    "source_docs:",
                ]
                for f in files:
                    lines.append(f"  - file: {f}")
                if pages:
                    note = (f"共 {pages} 页，抽出 {characters} 字符但为乱码（字体映射坏，不可用）"
                            if rep.get("text_garbled")
                            else f"共 {pages} 页，可抽文本 {characters} 字符")
                    lines.append(f"  - note: {note}")
                if rep.get("images_pending_ocr") and rep.get("sources"):
                    lines.append(f"  - note: 另有 {len(rep['images_pending_ocr'])} 张图片资料（JPG/PNG），无文本层")
                ocr_done = bool(rep.get("ocr_done"))
                lines += [
                    "",
                    "material_status:",
                    f"  text_extracted: {str(bool(rep.get('text_extracted', characters))).lower()}",
                    f"  ocr_needed: {str(bool(ocr)).lower()}",
                ]
                if scope:
                    note_txt = f"已 OCR 完成（原始状态：{scope_note}）" if ocr_done else scope_note
                    lines += [f"  ocr_scope: {scope}",
                              f"  ocr_note: {note_txt}"]
                    if ocr_done:
                        lines.append("  ocr_done: true")
                        lines.append(f"  text_source: ocr（{rep.get('ocr_pages', 0)} 个单元，"
                                     f"{rep.get('ocr_chars', 0)} 字）")
                if cap:
                    lines += ["", "inferred:",
                              f"  - 容量 {cap}{cap_unit}（{tsrc}，待按说明书核定）"]
                lines += [
                    "",
                    "todo:",
                    "  - specs.yaml     参数（按 00_schema/spec_dictionary 逐键回填）",
                    "  - features.yaml  功能有无",
                    "  - usage.yaml     操作与保养",
                    "  - faults.yaml    型号差异（代码规则/保护项）",
                ]
                if appliance in ("air_conditioner", "washer"):
                    lines.append("  - install.yaml   安装条件")
                if ocr_done:
                    lines.append("  - 资料已 OCR 完成，数值须对照原图/ocr.md 复核后再落库")
                elif scope == "partial":
                    lines.append(f"  - {scope_note}，仅影响该部分图表/参数页，其余文本已可读")
                elif scope == "garbled":
                    lines.append(f"  - {scope_note}；乱码不能当资料用（见 10_ops/backlog.md P0）")
                elif scope:
                    lines.append(f"  - 资料{scope_note}，需 OCR 后才能回填（见 10_ops/backlog.md P0）")
                lines += [
                    "",
                    "notes:",
                    "  - 自动生成的型号身份卡：不得用于回答参数/安装类问题",
                ]
                for n in ov.get("notes", []):
                    lines.append(f"  - {n}")
                out.write_text("\n".join(lines) + "\n", encoding="utf-8")
                made += 1
                print(f"[card] {appliance}/{brand}/{model}  ocr={ocr} text={bool(characters)}")

    print(f"\n新建 {made}，跳过已存在 {skipped}，保护性跳过 {blocked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
