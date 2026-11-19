#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""说明书写入脚本：01_source -> 02_ingest

职责（只做机械转换，不做任何知识判断）：
  1. 扫描 01_source/{品类}/{品牌}/{型号}/ 下的 pdf / jpg
  2. PDF 逐页抽文本，输出 02_ingest/{品类}/{品牌}/{型号}/text.md（带页码锚点）
  3. 输出 report.json：页数、字符数、疑似扫描页（几乎无文本）、图片资料清单
  4. 汇总生成 01_source/_manifest.yaml（型号清单，供 03_product 对照建卡）

用法:
  python tools/ingest.py                # 全量
  python tools/ingest.py --force        # 覆盖已存在的抽取结果
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "01_source"
OUT = ROOT / "02_ingest"

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
PDF_EXT = {".pdf"}
SCAN_THRESHOLD = 40  # 单页可抽字符少于此值 -> 判定为疑似扫描页/纯图页

# 文本层"假成功"检测：PDF 里字体没有正确嵌入 ToUnicode 表时，PyMuPDF 能抽出大量字符，
# 但全是乱码（如"䊺墖䟂伩橵䥌"）。字符数看起来正常，OCR 标记却是 false —— 这是最危险的一类脏数据。
# 判据：真实中文说明书每千字里"的了是在不和有请使用安装"至少出现 20 次；乱码文本接近 0。
COMMON_CJK = "的了是在不和有请使用安装"
GARBLED_RATIO = 5.0


def looks_garbled(text):
    n = len(text.strip())
    if n < 500:
        return False
    hit = sum(text.count(c) for c in COMMON_CJK)
    return hit * 1000 / n < GARBLED_RATIO


def dump_yaml_lite(obj, indent=0):
    """极简 YAML 输出（避免额外依赖 PyYAML）。只支持 dict/list/str/int/bool。"""
    pad = "  " * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(dump_yaml_lite(v, indent + 1))
            elif isinstance(v, list) and not v:
                lines.append(f"{pad}{k}: []")
            elif isinstance(v, dict) and not v:
                lines.append(f"{pad}{k}: {{}}")
            else:
                lines.append(f"{pad}{k}: {fmt_scalar(v)}")
    elif isinstance(obj, list):
        for it in obj:
            if isinstance(it, dict):
                sub = dump_yaml_lite(it, indent + 1).lstrip()
                lines.append(f"{pad}- {sub}")
            else:
                lines.append(f"{pad}- {fmt_scalar(it)}")
    return "\n".join(lines)


def fmt_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or re.search(r"[:#\[\]{}&*!|>'\"%@`]", s) or s.strip() != s:
        return '"' + s.replace('"', '\\"') + '"'
    return s


def pdf_pages(path):
    import fitz  # PyMuPDF

    doc = fitz.open(path)
    pages = []
    for i in range(len(doc)):
        pages.append(doc[i].get_text())
    return pages


def ingest_one(ap, brand, model, force=False):
    src_dir = SRC / ap / brand / model
    dst_dir = OUT / ap / brand / model
    text_md = dst_dir / "text.md"
    report = dst_dir / "report.json"
    if text_md.exists() and not force:
        return "skip"

    dst_dir.mkdir(parents=True, exist_ok=True)
    pdfs = sorted([p for p in src_dir.iterdir() if p.suffix.lower() in PDF_EXT])
    imgs = sorted([p for p in src_dir.iterdir() if p.suffix.lower() in IMG_EXT])

    chunks = []
    rep = {
        "appliance": ap,
        "brand": brand,
        "model": model,
        "sources": [],
        "images_pending_ocr": [],
        "scanned_pages": [],
        "total_chars": 0,
    }

    for pdf in pdfs:
        try:
            pages = pdf_pages(pdf)
        except Exception as e:  # noqa: BLE001
            rep["sources"].append({"file": pdf.name, "error": str(e)})
            continue
        chars = sum(len(p.strip()) for p in pages)
        garbled = looks_garbled("\n".join(pages))
        rep["sources"].append(
            {"file": pdf.name, "pages": len(pages), "chars": chars, "type": "pdf",
             "text_garbled": garbled}
        )
        rep["total_chars"] += chars
        if garbled:
            # 文本层损坏 -> 等同于整本无文本层，全部页面走 OCR
            rep["text_garbled"] = True
            for idx in range(1, len(pages) + 1):
                rep["scanned_pages"].append({"file": pdf.name, "page": idx})
            chunks.append(
                f'<!-- src: {pdf.name} | WARNING: 文本层损坏 -->\n'
                f"# {pdf.stem}（文本层损坏，下方为乱码原文，仅作对照，不得据此回填参数）\n\n"
                + "\n".join(p.strip() for p in pages) + "\n"
            )
            continue
        for idx, p in enumerate(pages, start=1):
            if len(p.strip()) < SCAN_THRESHOLD:
                rep["scanned_pages"].append({"file": pdf.name, "page": idx})
            chunks.append(
                f'<!-- src: {pdf.name} | page: {idx} -->\n'
                f'<a id="p{idx}"></a>\n'
                f'## {pdf.stem} 第 {idx} 页\n\n{p.strip()}\n'
            )

    for img in imgs:
        rep["images_pending_ocr"].append(img.name)

    rep["ocr_needed"] = (not chunks) or bool(rep["scanned_pages"])
    rep["text_extracted"] = bool(chunks) and not rep.get("text_garbled", False)

    if chunks:
        text_md.write_text("\n".join(chunks), encoding="utf-8")
    report.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    return "ok"


def write_manifest():
    rows = []
    if not SRC.exists():
        return
    for ap_dir in sorted([d for d in SRC.iterdir() if d.is_dir() and not d.name.startswith("_")]):
        for brand_dir in sorted([d for d in ap_dir.iterdir() if d.is_dir()]):
            for model_dir in sorted([d for d in brand_dir.iterdir() if d.is_dir()]):
                files = sorted(f.name for f in model_dir.iterdir() if f.is_file())
                rep_path = OUT / ap_dir.name / brand_dir.name / model_dir.name / "report.json"
                pages = 0
                if rep_path.exists():
                    try:
                        pages = int(
                            json.loads(rep_path.read_text(encoding="utf-8")).get("total_pages", 0)
                        )
                    except Exception:  # noqa: BLE001
                        pages = 0
                rows.append(
                    {
                        "appliance": ap_dir.name,
                        "brand": brand_dir.name,
                        "model": model_dir.name,
                        "files": files,
                        "pdf_count": sum(1 for f in files if f.lower().endswith(".pdf")),
                        "image_count": sum(
                            1 for f in files if os.path.splitext(f)[1].lower() in IMG_EXT
                        ),
                    }
                )
    data = {
        "generated_by": "tools/ingest.py",
        "note": "型号清单；新增说明书后重跑 ingest.py 自动更新。model 字段为目录名，原始型号以 product.yaml 为准",
        "count": len(rows),
        "items": rows,
    }
    (SRC / "_manifest.yaml").write_text(
        "# 资料清单（自动生成，勿手改）\n" + dump_yaml_lite(data) + "\n", encoding="utf-8"
    )


def ingest_brand_docs(ap, brand):
    """品牌级零散资料（不在型号目录下），如 supoor/故障自查.md
    这类文件往往是运营自己整理的高价值资料，不能因为不在型号目录里就被漏掉。"""
    src = SRC / ap / brand
    loose = [f for f in src.iterdir() if f.is_file() and f.suffix.lower() in {".md", ".txt"}]
    if not loose:
        return 0
    dst = OUT / ap / brand / "_brand_docs.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    body = []
    for f in sorted(loose):
        body.append(f"<!-- brand-doc: {f.name} -->\n# {f.stem}\n\n{f.read_text(encoding='utf-8')}\n")
    dst.write_text("\n".join(body), encoding="utf-8")
    return len(loose)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--appliance", nargs="*", default=None,
                    help="只处理指定品类，如 --appliance washer")
    ap.add_argument("--model", nargs="*", default=None,
                    help="只处理指定型号，如 --model WM12P2602W")
    args = ap.parse_args()

    ok = skip = 0
    for ap_dir in sorted([d for d in SRC.iterdir() if d.is_dir() and not d.name.startswith("_")]):
        if args.appliance and ap_dir.name not in args.appliance:
            continue
        for brand_dir in sorted([d for d in ap_dir.iterdir() if d.is_dir()]):
            n = ingest_brand_docs(ap_dir.name, brand_dir.name)
            if n:
                print(f"[brand-docs] {ap_dir.name}/{brand_dir.name}: {n} 个文件")
            for model_dir in sorted([d for d in brand_dir.iterdir() if d.is_dir()]):
                if args.model and model_dir.name not in args.model:
                    continue
                r = ingest_one(ap_dir.name, brand_dir.name, model_dir.name, args.force)
                ok += r == "ok"
                skip += r == "skip"
                print(f"[{r}] {ap_dir.name}/{brand_dir.name}/{model_dir.name}")
    write_manifest()
    print(f"\n完成：新增 {ok}，跳过 {skip}（已存在，用 --force 覆盖）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
