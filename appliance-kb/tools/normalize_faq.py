#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FAQ 归一化：只保留「结论一句 + 指向权威文档」，不再复述政策/参数细则。

背景（`00_schema/single_source.md`）：110 条 FAQ 里有 58 条复述参数、19 条复述品牌
政策、20 条复述症状。复述层与权威层一旦漂移，用户会同时拿到两套说法；检索里也会
出现「多个都正确但不同名」的候选，gold 只认一个，MRR 自然上不去。

做法：
  - 结论句 = 原 answer 的第一句（保留关键数值，如「6 年」「7 天」）；
  - 指针   = 由该条 FAQ 的 `source` 反查权威文档（policy_id / 参数卡 / 症状锚点）；
  - 幂等   = answer 里已有「详见」就跳过。

用法：
  python tools/normalize_faq.py --dry     # 预览改动
  python tools/normalize_faq.py --apply    # 写回 04_scenario/*/faq.yaml
"""
import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
HEADER = (
    "# 由 tools/normalize_faq.py 归一化：FAQ 只保留「结论一句 + 指针」，"
    "事实以被指向的权威文档为准（见 00_schema/single_source.md）\n"
)


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def pointer_map():
    """权威文件（相对路径）或型号目录 -> 指针说明。

    返回 ``(files, slugs)``：政策/参数用完整相对路径命中，其余型号级文件
    （usage/install/features/faults）用 ``model_slug`` 命中目录。
    """
    files, slugs = {}, {}
    for path in sorted(list((ROOT / "05_brand_ref").rglob("*.yaml"))
                       + list((ROOT / "06_platform_policy").glob("*.yaml"))):
        data = load_yaml(path) or {}
        if isinstance(data, dict) and data.get("policy_id"):
            files[str(path.relative_to(ROOT))] = f"{data['policy_id']}（{path.name}）"
    for path in sorted((ROOT / "03_product").rglob("product.yaml")):
        data = load_yaml(path) or {}
        if not data.get("product_id"):
            continue
        rel = path.parent.relative_to(ROOT)
        files[f"{rel}/specs.yaml"] = f"{data['product_id']} 参数卡"
        slugs[path.parent.name] = str(data["product_id"])
    return files, slugs


def split_sources(source):
    """`05_brand_ref/haier/warranty.yaml / midea/warranty.yaml` → 两个路径。"""
    text = str(source or "")
    return [part.strip() for part in re.split(r"\s+/\s+|,\s*", text) if part.strip()]


def first_sentence(answer, limit=80):
    text = str(answer or "").strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[。！？])", text, maxsplit=1)
    head = parts[0].strip()
    if len(head) > limit:
        head = head[:limit].rstrip("，、；") + "…"
    return head


TOP_DIRS = ("05_brand_ref", "06_platform_policy", "03_product", "04_scenario")


def expand_shorthand(part, files):
    """`midea/warranty.yaml` 这种简写补上顶层目录，再判断是否命中。"""
    if part in files or part.split("/", 1)[0] in TOP_DIRS:
        return part
    for prefix in TOP_DIRS:
        candidate = f"{prefix}/{part}"
        if candidate in files:
            return candidate
    return part


def resolve_pointer(source, pointers):
    """返回 (指针, 未识别来源列表)。"""
    files, slugs = pointers
    hits, unknown = [], []
    for part in split_sources(source):
        base, _, anchor = part.partition("#")
        base = expand_shorthand(base, files)
        if base in files:
            # 参数卡是整份 specs 的投影，字段锚点不再单独成指针
            hits.append(files[base])
            continue
        matched = re.match(r"^03_product/[^/]+/[^/]+/([^/]+)/([^#]+)$", base)
        if matched:
            slug, filename = matched.group(1), matched.group(2)
            pid = slugs.get(slug)
            if pid:
                hits.append(f"{pid} 的 {filename}" + (f"（{anchor}）" if anchor else ""))
                continue
        # 症状库是文件级共享的，指针保留锚点即可
        if base.endswith("symptoms.yaml") and anchor:
            hits.append(f"症状库 symptoms.yaml#{anchor}")
            continue
        unknown.append(part)
    return hits, unknown


def normalize_item(item, pointers, force=False):
    """返回 (新 item, 是否有改动, 未识别来源)。"""
    answer = str(item.get("answer") or "")
    if "详见" in answer:
        if not force:
            return item, False, []
        # 重算：先剥掉上一次生成的指针，避免「详见 A。详见 B。」
        answer = re.sub(r"\s*详见 [^。]*。?\s*$", "", answer).strip()
    hits, unknown = resolve_pointer(item.get("source"), pointers)
    if not hits:
        return item, False, unknown
    head = first_sentence(answer)
    new_answer = f"{head}详见 {'；'.join(dict.fromkeys(hits))}。"
    updated = dict(item)
    updated["answer"] = new_answer
    return updated, True, unknown


def main(argv=None):
    parser = argparse.ArgumentParser(description="FAQ 归一化为「结论 + 指针」")
    parser.add_argument("--apply", action="store_true", help="写回 faq.yaml")
    parser.add_argument("--force", action="store_true", help="重算已有指针的条目")
    parser.add_argument("--dry", action="store_true", help="只预览")
    args = parser.parse_args(argv)

    pointers = pointer_map()
    changed = skipped_no_pointer = 0
    unknown_all = {}
    for path in sorted((ROOT / "04_scenario").glob("s*/faq.yaml")):
        data = load_yaml(path) or {}
        items = data.get("faq") or []
        new_items = []
        file_changed = 0
        for item in items:
            updated, did, unknown = normalize_item(item, pointers, force=args.force)
            new_items.append(updated)
            if did:
                changed += 1
                file_changed += 1
                if args.dry:
                    print(f"[{path.parent.name}] {item['id']}: {item['answer'][:60]}…")
                    print(f"    → {updated['answer']}")
            else:
                if "详见" not in str(item.get("answer")):
                    skipped_no_pointer += 1
                for src in unknown:
                    unknown_all[src] = unknown_all.get(src, 0) + 1
        if args.apply and file_changed:
            data["faq"] = new_items
            body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
            path.write_text(HEADER + body, encoding="utf-8")

    print(f"归一化 {changed} 条；未识别出处的 {skipped_no_pointer} 条（保留原文）")
    for src, count in sorted(unknown_all.items(), key=lambda x: -x[1]):
        print(f"  未识别 source ×{count}: {src}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
