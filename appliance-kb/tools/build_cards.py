#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""型号参数卡：把同一型号的数十条单字段参数合并成一篇「完整参数表」。

为什么合并（见 `00_schema/single_source.md`）：

- 一字段一切片时，一个型号有 26~72 篇同构短句（「KFR-35GW/Q2 的额定制冷量为
  3510W」），用户问「这台室内机多大」要在一堆只差字段名的句子里挑一条，
  MRR 天然上不去；
- 参数本来就该走结构化工具（`docs/DESIGN.md` §6.3、`search_products` /
  `get_product`）。合并成卡后，检索只负责「命中这个型号的卡」，
  字段级正确性交给答案级指标（`answer_checks.json` 的 `must_include`）。

本脚本只做**渲染与自检**；写切片由 `build_index.py` 统一负责（避免两个脚本
同时写 `08_index/`）。用法：

  python tools/build_cards.py --check            # 每型号卡片统计（节数/字数）
  python tools/build_cards.py --print <product>  # 打印某型号卡片正文
"""
import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_yaml(path):
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] YAML 解析失败 {path}: {e}", file=sys.stderr)
        return None


def fmt_spec(v):
    """参数值 → 可读文本。逻辑与 `build_index.fmt` 一致，改一处请同步另一处。"""
    if not isinstance(v, dict):
        return str(v)
    if {"w", "h", "d"} <= set(v):
        return f"{v['w']}×{v['h']}×{v['d']}{v.get('unit') or ''}（宽×高×深）"
    if "value" in v and "min" in v:
        return f"{v['value']}{v.get('unit') or ''}（{v.get('min')}~{v.get('max')}）"
    if "value" in v:
        return f"{v['value']}{v.get('unit') or ''}"
    if "min" in v and "max" in v:
        return f"{v['min']}~{v['max']}{v.get('unit') or ''}"
    if "min" in v and "standard" in v:
        return (f"静音{v['min']}／高风{v.get('standard')}／超强{v.get('max')}"
                f"{v.get('unit') or ''}")
    return str(v)


def load_names(appliance):
    """参数键 / 功能键 → 中文名（`00_schema/spec_dictionary/<appliance>.yaml`）。"""
    path = ROOT / "00_schema/spec_dictionary" / f"{appliance}.yaml"
    data = (load_yaml(path) or {}) if path.exists() else {}
    keys = {k: (v or {}).get("cn", k) for k, v in (data.get("keys") or {}).items()}
    features = data.get("feature_names") or {}
    return keys, features


def render_card(product, specs, features, keys_cn, features_cn, extra_asks=(), spec_aliases=None):
    """渲染一张参数卡（Markdown）。

    :param extra_asks: 该型号 FAQ 的问法（由 ``build_index`` 传入）。FAQ 不再作为答案
        候选，但它的**问法**必须进卡，否则用户的口语匹配不到卡里的字段名。
    """
    appliance_cn = product.get("appliance_cn") or product.get("appliance")
    lines = [f"{product.get('model')} 完整参数卡（{appliance_cn}）"]

    spec_items = [(k, v) for k, v in (specs.get("specs") or {}).items() if v is not None]
    feature_items = [(k, v) for k, v in (features.get("features") or {}).items()]

    asks = [keys_cn.get(k, k) for k, _ in spec_items]
    asks.extend(str(item) for item in extra_asks if str(item).strip())
    if asks:
        # 顶部放一遍「用户常问」：用户不会说 cooling_capacity_w，说的是「几匹、多大」
        lines.append("用户常问：" + "、".join(dict.fromkeys(asks)))

    for key, value in spec_items:
        cn = keys_cn.get(key, key)
        source = value.get("source") if isinstance(value, dict) else None
        lines.append("")
        lines.append(f"## {cn}（{key}）")
        asks = [str(item) for item in ((spec_aliases or {}).get(key) or []) if str(item).strip()]
        if asks:
            # 用户不会说 `weight_kg`，也不会说「整机质量」——他说「多重」
            lines.append("用户常问：" + "、".join(asks))
        lines.append(fmt_spec(value) + (f"　出处：{source}" if source else ""))
        note = value.get("note") if isinstance(value, dict) else None
        if note:
            lines.append(f"说明：{note}")

    if feature_items:
        lines.append("")
        lines.append("## 支持的功能")
        for key, value in feature_items:
            supported = value.get("supported") if isinstance(value, dict) else None
            state = {True: "支持", False: "不支持", None: "说明书未标注"}[supported]
            cn = features_cn.get(key, key)
            phrase = value.get("phrase") if isinstance(value, dict) else None
            note = value.get("note") if isinstance(value, dict) else None
            tail = "".join([f"，{phrase}" if phrase else "", f"。{note}" if note else ""])
            lines.append(f"- {cn}（{key}）：{state}{tail}")

    return "\n".join(lines).strip()


def iter_products():
    """遍历 status=ready 的型号，产出 (product, 目录, specs, features)。"""
    for prod_path in sorted((ROOT / "03_product").rglob("product.yaml")):
        product = load_yaml(prod_path)
        if not product or product.get("status") != "ready":
            continue
        directory = prod_path.parent
        specs = load_yaml(directory / "specs.yaml") or {}
        features = load_yaml(directory / "features.yaml") or {}
        yield product, directory, specs, features


def build_cards(spec_aliases=None):
    """返回 [(product, rel_dir, card_text, spec_keys)]，供 build_index 写入切片。"""
    cards = []
    for product, directory, specs, features in iter_products():
        keys_cn, features_cn = load_names(product.get("appliance"))
        text = render_card(
            product, specs, features, keys_cn, features_cn, spec_aliases=spec_aliases
        )
        spec_keys = [k for k, v in (specs.get("specs") or {}).items() if v is not None]
        cards.append((product, directory.relative_to(ROOT), text, spec_keys))
    return cards


def main(argv=None):
    parser = argparse.ArgumentParser(description="型号参数卡渲染与自检")
    parser.add_argument("--check", action="store_true", help="打印每型号统计")
    parser.add_argument("--print", dest="product_id", help="打印某型号卡片正文")
    parser.add_argument("--json", action="store_true", help="--check 时输出 JSON")
    args = parser.parse_args(argv)

    cards = build_cards()
    if args.product_id:
        for product, _directory, text, _keys in cards:
            if product.get("product_id") == args.product_id:
                print(text)
                return 0
        print(f"未找到 ready 型号：{args.product_id}", file=sys.stderr)
        return 1

    rows = [
        {
            "product_id": product.get("product_id"),
            "spec_keys": len(keys),
            "chars": len(text),
            "sections": text.count("\n## "),
        }
        for product, _directory, text, keys in cards
    ]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"参数卡 {len(rows)} 张（每型号 1 张）")
    for row in rows:
        print(f"  {row['product_id']:34s} 键 {row['spec_keys']:3d}  节 {row['sections']:2d}  字 {row['chars']}")
    print(f"合计：键 {sum(r['spec_keys'] for r in rows)}，字 {sum(r['chars'] for r in rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
