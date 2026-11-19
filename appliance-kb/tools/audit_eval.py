#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测集答案审计：把每条标准答案拿回源文件核对，找出"不对的地方"。

和 `check_eval.py` 的分工：
  check_eval.py —— 评测集**自身**是否自洽（必须项在答案里、禁止项不在答案里、id 唯一…）
  audit_eval.py  —— 标准答案**对不对**（数字在源文件里查得到吗、依据声明和库内 confidence 一致吗、
                    取数是不是拿错了品牌的文件）

检查项：
  A1 数值溯源   must_include 里的数字，必须能在 source_contains 指向的文件里找到
  A2 依据一致性 answer_basis 声明的凭据等级，必须和库内该参数的 confidence 对得上
  A3 跨品牌取数 source_contains 指向 05_brand_ref/{X}/… 时，行里的 brand 必须是 X
  A4 答案数字   answer 里出现的数字，必须能在出处文件里找到（政策类口径的抽查）
  A5 越界型号   answer 里提到了不属于本行 product_id 的其他型号（可能是串台）

输出：
  stdout  审计报告
  --json  写 09_eval/_audit.json（供 build_review_html.py 渲染确认页）

用法:
  python tools/audit_eval.py
  python tools/audit_eval.py --json
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "09_eval/questions.jsonl"
OUT = ROOT / "09_eval/_audit.json"

BRANDS = {"haier", "midea", "littleswan", "siemens", "supor"}
CN_BRAND = {"haier": "海尔", "midea": "美的", "littleswan": "小天鹅",
            "siemens": "西门子", "supor": "苏泊尔"}


def load_yaml(p):
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def digits(text):
    """抽取文本里的数字组（去掉小数点后的单位干扰）"""
    return re.findall(r"\d+(?:\.\d+)?", str(text))


# 型号代号（如 A11 / F40H9083S / N8KS1-1P / KFR-35GW/Q2）里的数字不是参数值，
# 校验数值溯源时必须先剥掉，否则每条提到别的机型的答案都会被判成"出处标错"。
_MODEL_TOKEN = re.compile(r"\b[A-Z]{1,4}-?\d{1,5}[A-Z0-9]*(?:/[A-Z0-9]+)?\b")


def strip_models(text):
    """剥掉型号代号，保留其余文字"""
    return _MODEL_TOKEN.sub(" ", str(text))


def digits_for_check(text):
    """用于数值溯源校验的数字：去掉型号代号里的数字、去掉匹数（由冷量推算而非库内字段）"""
    s = strip_models(text)
    # "1.5 匹" 这类匹数是由制冷量推出来的展示值，库里不单独存，按非溯源项处理
    s = re.sub(r"\d+(?:\.\d+)?\s*匹", " ", s)
    return digits(s)


def resolve_src(src, pid):
    """source_contains -> (绝对路径, 锚点key)"""
    if not src:
        return None, None
    head, _, anchor = src.partition("#")
    if head in ("specs.yaml", "features.yaml", "usage.yaml", "faults.yaml", "install.yaml"):
        base = None
        for sp in (ROOT / "03_product").glob("*/*/*/specs.yaml"):
            if (load_yaml(sp) or {}).get("product_id") == pid:
                base = sp.parent
                break
        return (base / head if base else None), anchor
    p = ROOT / head
    return (p if p.exists() else None), anchor


def spec_confidence(src, pid, anchor):
    """若出处是某个型号的 specs.yaml#key，返回该参数的 confidence"""
    if not src or not anchor:
        return None
    head = src.split("#")[0]
    if head not in ("specs.yaml", "features.yaml"):
        return None
    for sp in (ROOT / "03_product").glob("*/*/*/specs.yaml"):
        if (load_yaml(sp) or {}).get("product_id") != pid:
            continue
        d = load_yaml(sp)
        node = (d.get("specs") or {}) if head == "specs.yaml" else (d.get("features") or {})
        v = node.get(anchor)
        if isinstance(v, dict):
            return v.get("confidence") or "confirmed"
        return None
    return None


def all_models():
    out = {}
    for sp in (ROOT / "03_product").glob("*/*/*/product.yaml"):
        d = load_yaml(sp)
        if d.get("product_id"):
            out[d["product_id"]] = sp.parent.name
    return out



def build_corpus():
    """全库语料：03~06 层的 yaml/md 文本拼一起，用于"这个数字全库有没有出处"兜底查"""
    buf = []
    for d in ("03_product", "04_scenario", "05_brand_ref", "06_platform_policy"):
        for p in (ROOT / d).rglob("*"):
            if p.suffix in (".yaml", ".md") and p.is_file():
                try:
                    buf.append(p.read_text(encoding="utf-8", errors="ignore"))
                except Exception:  # noqa: BLE001
                    pass
    return "\n".join(buf)


CORPUS = build_corpus()

# (相对路径, 文本) 列表，用于"这个数在库里哪儿"
FILES = []
for _d in ("03_product", "04_scenario", "05_brand_ref", "06_platform_policy"):
    for _p in (ROOT / _d).rglob("*"):
        if _p.suffix in (".yaml", ".md") and _p.is_file():
            FILES.append((str(_p.relative_to(ROOT)).replace("\\", "/"),
                          _p.read_text(encoding="utf-8", errors="ignore")))

_CORPUS_NUMS = set(re.findall(r"\d{3,}", CORPUS))


def is_derivable(num):
    """这个数能不能由库内两个数相减/相加得到（是的话就不是凭空来的，属推导值）"""
    try:
        v = int(num)
    except ValueError:
        return False
    if v < 100:
        return False
    ns = {int(x) for x in _CORPUS_NUMS}
    for a in ns:
        if a - v in ns or a + v in ns:   # a-v=v 或 a+v=v → 由两个库内数相减/相加得来
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rows = [json.loads(l) for l in EVAL.read_text(encoding="utf-8").splitlines() if l.strip()]
    models = all_models()
    issues = []
    src_cache = {}

    for r in rows:
        rid = r["id"]
        exp = r["expect"]
        src = exp.get("source_contains")
        ans = r.get("answer") or ""
        basis = r.get("answer_basis")
        pid = r.get("product_id")
        flags = []

        path, anchor = resolve_src(src, pid)
        text = ""
        if path is not None:
            key = str(path)
            if key not in src_cache:
                src_cache[key] = path.read_text(encoding="utf-8", errors="ignore")
            text = src_cache[key]

        # ── A1 数值溯源 ───────────────────────────────
        for tok in exp.get("must_include") or []:
            ds = digits(tok)
            if not ds:
                continue
            if text and not any(d in text for d in ds):
                flags.append({
                    "code": "A1", "level": "error",
                    "msg": f"必须项「{tok}」里的数字 {ds} 在出处文件里查不到",
                    "src": src,
                })

        # ── A2 依据一致性 ─────────────────────────────
        conf = spec_confidence(src, pid, anchor)
        if conf and basis:
            if basis == "说明书原文" and conf != "confirmed":
                flags.append({
                    "code": "A2", "level": "warn",
                    "msg": f"标为「说明书原文」，但库内该参数 confidence = {conf}",
                    "src": src,
                })
            if basis == "推断" and conf == "confirmed":
                flags.append({
                    "code": "A2", "level": "warn",
                    "msg": f"标为「推断」，但库内该参数 confidence = confirmed",
                    "src": src,
                })
            if basis == "业务方口径" and conf == "confirmed":
                flags.append({
                    "code": "A2", "level": "info",
                    "msg": f"标为「业务方口径」，库内已转 confirmed（核定后）",
                    "src": src,
                })

        # ── A3 跨品牌取数 ─────────────────────────────
        if src and src.startswith("05_brand_ref/"):
            seg = src.split("/")
            if len(seg) > 2 and seg[2] in BRANDS and r.get("brand") and r["brand"] != seg[2]:
                flags.append({
                    "code": "A3", "level": "error",
                    "msg": f"出处是 {CN_BRAND[seg[2]]} 的文件，但这一行的 brand 是 {CN_BRAND.get(r['brand'], r['brand'])}",
                    "src": src,
                })

        # ── A4 答案数字抽查（声明出处 → 全库兜底）────────
        if text:
            ans_nums = [d for d in digits_for_check(ans)
                        if len(d) >= 2 and d not in
                        ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10")]
            miss = [d for d in ans_nums if d not in text]
            if miss:
                # 到全库兜底找：找不到就是"答案里的数在整个知识库里都没有出处"
                nowhere = [d for d in miss
                           if d not in CORPUS and not is_derivable(d)]
                if nowhere:
                    flags.append({
                        "code": "A4", "level": "error",
                        "msg": f"答案里的数字 {'、'.join(nowhere)} 在声明出处和整个知识库里都查不到",
                        "src": src,
                    })
                elif len(miss) == len(ans_nums) and len(ans_nums) >= 2:
                    where = []
                    for d in miss[:3]:
                        hits = [f for f in FILES if d in f[1]]
                        where.append(f"{d}→{hits[0][0] if hits else '?'}")
                    flags.append({
                        "code": "A4", "level": "warn",
                        "msg": f"答案里 {len(ans_nums)} 个数字不在声明出处里，但库内别处能找到"
                              f"（{'、'.join(where)}）——出处可能标错",
                        "src": src,
                    })

        # ── A6 出处内容与本行品类不符 ─────────────────
        if path is not None and path.suffix == ".yaml" and r.get("appliance"):
            d = load_yaml(path)
            applies = (d.get("applies_to") or {}).get("appliance")
            if applies and r["appliance"] not in applies:
                # 有些题目的用意恰恰是"这个文件不覆盖本品类"（用来证明库内缺失），
                # 这时指向不适用的出处是对的，只降级不报错
                admits_missing = any(k in ans for k in
                                     ("不适用", "没有资料", "不覆盖", "不敢", "本库没有", "没有这个政策"))
                flags.append({
                    "code": "A6", "level": "info" if admits_missing else "error",
                    "msg": f"出处文件适用品类是 {applies}，但这一行是 {r['appliance']}"
                           + ("——但答案本身在说明「本库不覆盖」，属故意指向，降级为提示"
                              if admits_missing else "——出处张冠李戴"),
                    "src": src,
                })

        # ── A5 越界型号 ───────────────────────────────
        for other_pid, name in models.items():
            if other_pid == pid:
                continue
            if name and name in ans:
                flags.append({
                    "code": "A5", "level": "info",
                    "msg": f"答案里提到其他型号 {name}（对比场景属正常，单型号问答需确认是否串台）",
                    "src": src,
                })
                break

        if flags:
            issues.append({"id": rid, "flags": flags})

    # ── 汇总 ────────────────────────────────────────
    by_code = {}
    for it in issues:
        for f in it["flags"]:
            by_code.setdefault(f["code"], {"error": 0, "warn": 0, "info": 0})
            by_code[f["code"]][f["level"]] += 1

    print(f"审计 {len(rows)} 条，发现 {len(issues)} 条有标记")
    print(f"{'代码':<6}{'error':>7}{'warn':>7}{'info':>7}  说明")
    desc = {"A1": "数值溯源", "A2": "依据与库内 confidence 一致性",
            "A3": "跨品牌取数", "A4": "答案数字抽查", "A5": "越界型号",
            "A6": "出处适用品类与本行不符"}
    for c in ("A1", "A2", "A3", "A4", "A5", "A6"):
        d = by_code.get(c, {"error": 0, "warn": 0, "info": 0})
        print(f"{c:<6}{d['error']:>7}{d['warn']:>7}{d['info']:>7}  {desc[c]}")
    print()
    for it in issues:
        errs = [f for f in it["flags"] if f["level"] == "error"]
        warns = [f for f in it["flags"] if f["level"] == "warn"]
        mark = "E" if errs else ("W" if warns else "i")
        print(f"[{mark}] {it['id']}")
        for f in it["flags"]:
            print(f"      {f['code']} {f['level']:<5} {f['msg']}")

    if a.json:
        OUT.write_text(json.dumps(
            {"total": len(rows), "issues": issues, "by_code": by_code},
            ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        print(f"\n→ {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
