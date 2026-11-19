#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打印对比组当前参数，用来对照 04_scenario/s1_presale/compare.yaml

为什么需要这个脚本：compare.yaml 里的表是手写的快照（"595×560×850""16（150~5350）"
这类带口径的字符串没法直接取机械值）。改过 specs.yaml 之后，靠肉眼比对 28 个型号
一定会漏。这个脚本按 compare.yaml 里声明的组和成员，把 specs/fетures 的**原始值**
打出来，人只需比对差异。

用法:
  python tools/dump_compare.py            # 全部品类
  python tools/dump_compare.py washer     # 只看洗衣机
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPARE = ROOT / "04_scenario/s1_presale/compare.yaml"
PROD = ROOT / "03_product"


def load(p):
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def fmt(v):
    """把 specs 里的条目还原成可读值，尺寸统一成 宽×深×高（与 compare.yaml 口径一致）"""
    if v is None:
        return "未标注"
    if not isinstance(v, dict):
        return str(v)
    if {"w", "h", "d"} <= set(v):
        return f"{v['w']}×{v['d']}×{v['h']}"
    val = v.get("value")
    if val is None and "min" in v and "max" in v:
        return f"{v['min']}~{v['max']}"
    conf = v.get("confidence")
    flag = {"web": "（网络值）", "inferred": "（推断）"}.get(conf, "")
    return f"{val}{flag}"


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    cmp = load(COMPARE) or {}
    apps = cmp.get("appliances") or {}

    for ap, block in apps.items():
        if only and ap != only:
            continue
        print("=" * 70)
        print(f"品类 {ap}")
        groups = block.get("groups") or {}
        dims = [d["key"] for d in (block.get("dimensions") or [])]
        for gname, g in groups.items():
            status = g.get("status")
            print(f"\n-- 组 {gname}（{g.get('cn')}）{f'[{status}]' if status else ''}")
            gdims = dims + [d["key"] for d in (g.get("dimensions_extra") or [])]
            for pid in g.get("members") or []:
                hits = list(PROD.glob(f"*/**/{pid.replace('-', '*')}/specs.yaml"))
                # product_id 与目录名不同（如 KFR-35GW-Q2 / haier-kfr-35gw-q2），按内容找
                target = None
                for sp in PROD.glob("*/*/*/specs.yaml"):
                    try:
                        if (load(sp) or {}).get("product_id") == pid:
                            target = sp
                            break
                    except Exception:  # noqa: BLE001
                        continue
                if not target:
                    print(f"   [缺失] {pid} —— 找不到对应 specs.yaml")
                    continue
                sp = load(target).get("specs") or {}
                ft = {}
                fp = target.parent / "features.yaml"
                if fp.exists():
                    ft = (load(fp) or {}).get("features") or {}
                print(f"   {pid}")
                for k in gdims:
                    if k.startswith("feature_"):
                        fk = k[len("feature_"):]
                        val = (ft.get(fk) or {}).get("supported")
                        val = {True: "有", False: "无", None: "未标注"}[val]
                    else:
                        val = fmt(sp.get(k))
                    print(f"      {k:26} {val}")
    print()
    print("提示：比对本输出与 compare.yaml 的 tables，发现差异就改 compare.yaml（不要改 specs 去迁就表）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
