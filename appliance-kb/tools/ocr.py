#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OCR 补文本层：把"纯图片资料"、"PDF 扫描页"和"乱码 PDF"变成可检索的文本。

为什么不直接改 ingest.py：
  ingest.py 的职责是"能抽就抽，抽不到就记账"。OCR 慢、依赖重、失败率高，
  混进 ingest 会让"资料入库"这件事变脆。所以拆成两步：
  ingest 出 report.json 记 ocr_needed，ocr.py 按需补账。

两个必须处理的坑：
  1. 长图/超长页：苏泊尔 F50X96 的 说明书3.pdf 单页高 16356px，
     整张丢给检测模型要几分钟。必须切条（带重叠）再识别。
  2. 顺序执行太慢：141 页 × 5 秒 = 12 分钟。按"页"粒度分给多进程，
     16 核能压到 2~3 分钟。

产出：
  02_ingest/{appliance}/{brand}/{model}/ocr.md      带 <a id="img:xxx"> / <a id="pN"> 锚点
  02_ingest/{appliance}/{brand}/{model}/ocr/*.txt  单文件缓存，重跑不重复识别
  report.json 增加 ocr_done / ocr_chars 字段

用法:
  python tools/ocr.py                      # 补所有 ocr_needed 的型号
  python tools/ocr.py --appliance washer   # 只补某个品类
  python tools/ocr.py --model TG10K60      # 只补某个型号
  python tools/ocr.py --jobs 8             # 并发进程数（默认 4）
  python tools/ocr.py --force              # 忽略缓存，重新识别
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ING = ROOT / "02_ingest"
SRC = ROOT / "01_source"
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

TILE_H = 1600      # 单块高度上限（px）
TILE_OVERLAP = 120  # 块间重叠，避免半行被切断
RENDER_DPI = 180

_engine = None


def engine():
    """RapidOCR 初始化要加载 3 个模型，很贵；每个进程只建一次"""
    global _engine
    if _engine is None:
        os.environ.setdefault("OMP_NUM_THREADS", "2")
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def _run(img_path):
    res, _ = engine()(str(img_path))
    return [r[1] for r in (res or [])]


def ocr_image_file(path, work_dir):
    """识别单张图片；超长图自动切条。返回行列表。"""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None

    path = Path(path)
    with Image.open(path) as im:
        w, h = im.size
        if h <= TILE_H or w == 0:
            return _run(path)

        lines = []
        y = 0
        i = 0
        while y < h:
            y2 = min(y + TILE_H, h)
            # 不删临时块：一是删了下次还要重写，二是 Windows 上删文件偶尔会撞上
            # 杀软/索引/句柄延迟。同名覆盖即可，_pages 随时可整目录丢弃。
            tile = work_dir / f"_tile_{path.stem}_{i}.png"
            im.crop((0, y, w, y2)).save(tile)
            lines += _run(tile)
            if y2 >= h:
                break
            y = y2 - TILE_OVERLAP
            i += 1
        return lines


def render_page(pdf, pageno, out_png, dpi=RENDER_DPI):
    import fitz
    doc = fitz.open(pdf)
    try:
        pix = doc[pageno - 1].get_pixmap(dpi=dpi)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        pix.save(out_png)
    finally:
        doc.close()


def do_task(task):
    """一个任务 = 一份图片 或 PDF 的一页。返回 (任务键, 行列表, 是否命中缓存)

    任务键必须带 ap/brand/model：小天鹅 5 个型号的图片都叫「产品介绍.jpg」，
    键里不带型号的话，5 个型号的结果会互相覆盖，
    最后所有型号的 ocr.md 都变成同一个型号的内容（这个坑真的踩过）。
    """
    ap, brand, model, kind, fname, page, force = task
    tkey = (ap, brand, model, kind, fname, page)
    base = ING / ap / brand / model
    srcbase = SRC / ap / brand / model
    cache = base / "ocr"
    cache.mkdir(parents=True, exist_ok=True)

    stem = Path(fname).stem if kind == "img" else f"{Path(fname).stem}_p{page}"
    cf = cache / f"{stem}.txt"
    if cf.exists() and not force:
        return tkey, cf.read_text(encoding="utf-8").splitlines(), True

    work = base / "_pages"
    work.mkdir(parents=True, exist_ok=True)
    if kind == "img":
        src = srcbase / fname
        if not src.exists():
            cand = list(srcbase.rglob(fname))
            if not cand:
                return tkey, [], False
            src = cand[0]
    else:
        src = work / f"{Path(fname).stem}_p{page}.png"
        if not src.exists():
            render_page(srcbase / fname, page, src)

    try:
        lines = ocr_image_file(src, work)
    except Exception as e:  # noqa: BLE001
        # 单个单元失败不该让整批挂掉；失败结果不写缓存，下次重跑会重试
        print(f"  [fail] {ap}/{brand}/{model} {fname} p{page}: {e}", file=sys.stderr)
        return tkey, [f"（OCR 失败：{type(e).__name__}）"], False
    cf.write_text("\n".join(lines), encoding="utf-8")
    return tkey, lines, False


def collect(appliance=None, model_filter=None):
    tasks = []
    for rp in sorted(ING.rglob("report.json")):
        rel = rp.relative_to(ING).parts
        if len(rel) != 4:
            continue
        ap, brand, model = rel[0], rel[1], rel[2]
        if appliance and ap != appliance:
            continue
        if model_filter and model not in model_filter:
            continue
        rep = json.loads(rp.read_text(encoding="utf-8"))
        if not rep.get("ocr_needed"):
            continue
        for img in rep.get("images_pending_ocr") or []:
            tasks.append((ap, brand, model, "img", img, None))
        for sp in rep.get("scanned_pages") or []:
            tasks.append((ap, brand, model, "pdf", sp["file"], sp["page"]))
    return tasks


def assemble(tasks, results, force):
    """按型号写 ocr.md"""
    by_model = {}
    for t in tasks:
        by_model.setdefault(t[:3], []).append(t)

    done = 0
    for (ap, brand, model), ts in sorted(by_model.items()):
        base = ING / ap / brand / model
        rep = json.loads((base / "report.json").read_text(encoding="utf-8"))
        out = ["# OCR 文本", "",
               f"来源：{ap}/{brand}/{model}",
               "说明：OCR 结果可能有错字（尤其 0/O、1/l、8/B 和单位符号），"
               "用于回填参数前必须对照原图确认，不得直接采信。", ""]
        chars = 0
        for t in ts:
            key = (ap, brand, model, t[3], t[4], t[5])
            lines = results.get(key, [])
            txt = "\n".join(lines)
            chars += len(txt)
            if t[3] == "img":
                out += [f'<a id="img:{t[4]}"></a>', f"## {t[4]}", ""]
            else:
                out += [f'<a id="{Path(t[4]).stem}:p{t[5]}"></a>',
                        f"## {t[4]} 第 {t[5]} 页", ""]
            out += [txt or "（未识别到文字）", ""]

        (base / "ocr.md").write_text("\n".join(out), encoding="utf-8")
        rep["ocr_done"] = True
        rep["ocr_chars"] = chars
        rep["ocr_pages"] = len(ts)
        (base / "report.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ocr ] {ap}/{brand}/{model}  {len(ts)} 单元  {chars} 字")
        done += 1
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--appliance")
    ap.add_argument("--model", nargs="*", default=None)
    ap.add_argument("--jobs", type=int, default=2,
                    help="并发进程数。别贪大：onnxruntime 每个会话默认吃满物理核，"
                         "8 并发实测比 2 并发更慢（线程打架）。默认 2")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    tasks = collect(args.appliance, args.model)
    if not tasks:
        print("没有需要 OCR 的资料")
        return 0
    n_img = sum(1 for t in tasks if t[3] == "img")
    print(f"待识别 {len(tasks)} 个单元（图片 {n_img}，PDF 页 {len(tasks) - n_img}），并发 {args.jobs}")

    payload = [t + (args.force,) for t in tasks]
    results = {}
    hits = 0
    t0 = time.time()
    if args.jobs <= 1:
        for t in payload:
            key, lines, cached = do_task(t)
            results[key] = lines
            hits += int(cached)
            print(f"  {key} {'cache' if cached else f'{len(lines)}行'} {time.time() - t0:.0f}s")
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            for i, (key, lines, cached) in enumerate(ex.map(do_task, payload), start=1):
                results[key] = lines
                hits += int(cached)
                if i % 10 == 0 or i == len(payload):
                    print(f"  {i}/{len(payload)}  已用 {time.time() - t0:.0f}s  缓存命中 {hits}")

    done = assemble(tasks, results, args.force)
    print(f"\n完成 {done} 个型号，用时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
