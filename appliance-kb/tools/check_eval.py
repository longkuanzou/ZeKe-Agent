#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测集自检：把评测集的"契约"也变成可执行检查。

为什么需要：questions.jsonl 是"标准答案待确认清单"，一旦 `must_include` 里的数
和 `answer` 对不上，或者答案里悄悄出现了本该禁止的说法，这份清单就不能拿来当
验收依据了。人眼扫 180 行一定会漏。

检查项：
  E1  id 唯一、格式 e-{scenario}-{nnn}
  E2  scenario / intent 在允许集合内
  E3  product_id 能在 03_product 里找到对应 specs.yaml；model 与 product_id 一一对应
  E4  must_include 非空（评测要有可判定的钩子）
  E5  must_include 的每一项都必须出现在 answer 里（否则标准答案自己就不含这个数）
  E6  must_not_include 的每一项都不允许出现在 answer 里（答案里只能"绕开"，不能"顺口说出"）
  E7  source_contains 指向的文件必须真实存在
  E8  answer_basis 在允许集合内；非"库内缺失"的条目必须有 source_contains
  E9  覆盖度：每个型号至少被 1 条问到；三品类各场景至少 1 条

用法:
  python tools/check_eval.py           # 报告
  python tools/check_eval.py --strict  # 有 error 时退出码 1
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "09_eval/questions.jsonl"
PROD = ROOT / "03_product"

SCENARIOS = {"s1_presale", "s2_install", "s3_usage",
             "s4_troubleshoot", "s5_aftersale", "s6_service", "s_adversarial"}
BASES = {"说明书原文", "业务方口径", "推断", "网络值", "政策文件", "库内缺失"}

ERRORS, WARNS = [], []


def err(m):
    ERRORS.append(m)


def warn(m):
    WARNS.append(m)


def load_products():
    """product_id -> specs.yaml 路径"""
    out = {}
    for sp in PROD.glob("*/*/*/specs.yaml"):
        try:
            d = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        pid = d.get("product_id")
        if pid:
            out[pid] = sp
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()

    if not EVAL.exists():
        print("缺少 09_eval/questions.jsonl，先跑 tools/gen_eval.py")
        return 1
    rows = [json.loads(l) for l in EVAL.read_text(encoding="utf-8").splitlines() if l.strip()]
    products = load_products()

    seen = set()
    touched = set()
    per_sc = Counter()

    for r in rows:
        rid = r.get("id", "<no-id>")
        sc = r.get("scenario")
        per_sc[sc] += 1

        # E1
        if rid in seen:
            err(f"[E1] id 重复：{rid}")
        seen.add(rid)
        if not re.fullmatch(r"e-[a-z0-9_]+-\d{3}", rid):
            warn(f"[E1] id 格式不规范：{rid}")

        # E2
        if sc not in SCENARIOS:
            err(f"[E2] {rid} 未知 scenario：{sc}")
        if not r.get("intent"):
            err(f"[E2] {rid} 缺 intent")

        # E3
        pid = r.get("product_id")
        if pid:
            touched.add(pid)
            if pid not in products:
                err(f"[E3] {rid} product_id 在 03_product 里找不到：{pid}")
            if not r.get("model"):
                err(f"[E3] {rid} 有 product_id 但没写 model")
        elif r.get("model"):
            err(f"[E3] {rid} 写了 model 但没写 product_id（无法机器对齐）")

        exp = r.get("expect") or {}
        inc = exp.get("must_include") or []
        exc = exp.get("must_not_include") or []
        ans = r.get("answer") or ""

        # E4
        if not inc:
            err(f"[E4] {rid} must_include 为空（没有可判定的钩子）")

        # E5
        for t in inc:
            if t not in ans:
                err(f"[E5] {rid} must_include「{t}」不在 answer 里")

        # E6
        for t in exc:
            if t in ans:
                err(f"[E6] {rid} must_not_include「{t}」却出现在 answer 里"
                    f"（禁止项要写成「错误答案才会出现的说法」，例如「是 {t}」而不是裸的「{t}」）")

        # E7
        src = exp.get("source_contains")
        if src:
            head = src.split("#")[0]
            if head in ("specs.yaml", "features.yaml", "usage.yaml", "faults.yaml", "install.yaml"):
                # 型号目录内的相对路径，按 product_id 解析
                base = products.get(pid) if pid else None
                p = base.parent / head if base else None
            else:
                p = ROOT / head
            if p is None:
                err(f"[E7] {rid} source_contains 用了相对路径但没写 product_id：{src}")
            elif not p.exists():
                err(f"[E7] {rid} source_contains 指向的文件不存在：{src}")

        # E8
        basis = r.get("answer_basis")
        if basis not in BASES:
            err(f"[E8] {rid} answer_basis 非法：{basis}")
        if basis != "库内缺失" and not src:
            warn(f"[E8] {rid} 缺 source_contains（依据是「{basis}」却没标出处）")
        if not ans.strip():
            err(f"[E8] {rid} answer 为空")

    # E9 覆盖度
    for pid in sorted(products):
        if pid not in touched:
            warn(f"[E9] 型号 {pid} 没有任何评测题覆盖")
    for sc in SCENARIOS:
        if per_sc[sc] == 0:
            err(f"[E9] 场景 {sc} 一条题都没有")
    by_ap = Counter(r.get("appliance") for r in rows)
    for ap_name in ("air_conditioner", "washer", "rice_cooker"):
        if by_ap[ap_name] == 0:
            err(f"[E9] 品类 {ap_name} 没有被覆盖")

    print(f"评测集 {len(rows)} 条")
    for sc, n in per_sc.items():
        print(f"  {sc:16} {n}")
    print("  -- 品类 --")
    for k, n in by_ap.items():
        print(f"  {str(k):16} {n}")
    print()
    for w in WARNS:
        print("WARN " + w)
    for e in ERRORS:
        print("ERROR " + e)
    print(f"\n{len(ERRORS)} error / {len(WARNS)} warn")
    if a.strict and ERRORS:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
