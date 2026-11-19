"""扫描知识文档里的商品参数（L12）。

知识库只写「怎么选 / 怎么装 / 怎么修」，价格与尺寸这类硬参数必须来自
``products`` 表。这里做一条静态检查：文档正文里带单位（元 / mm / 毫米）的数字，
若命中商品字段值就告警。容量单位（L / W / 匹）不参与匹配——「1 匹」「10L」
这类通用表述太多，逐值比对只会制造噪声。

用法::

    python -m scripts.check_knowledge_params --pack homeapp
    python -m scripts.check_knowledge_params --pack homeapp --strict

默认只告警（退出码 0）；``--strict`` 时有命中即退出 1，便于接入 CI。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

__all__ = ["collect_product_values", "scan_documents", "main"]

#: 带单位的数字：``3299 元`` / ``900mm`` / ``10L`` / ``3500W`` / ``1.5 匹``。
_UNIT_NUMBER = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>元|mm|毫米|L|升|W|瓦|匹)")
#: 从商品字段里抽数字（尺寸的 ``×`` 分隔也拆开）。
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

_UNIT_ALIASES = {"mm": "mm", "毫米": "mm", "L": "L", "升": "L", "W": "W", "瓦": "W", "元": "元", "匹": "匹"}


def _normalize(value: float) -> str:
    """把 3299.0 / 3299 统一成 ``3299``，避免字符串比较漏报。"""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _key(number: str, unit: str) -> str:
    return f"{_UNIT_ALIASES.get(unit, unit)}:{number}"


def collect_product_values(products_path: Path) -> dict[str, set[str]]:
    """从商品种子抽取「单位:数值 → 来源字段」，避免把尺寸数字当成价格命中。"""
    payload = yaml.safe_load(products_path.read_text(encoding="utf-8")) or {}
    rows = payload.get("products") if isinstance(payload, dict) else payload
    values: dict[str, set[str]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        price = row.get("price_cents")
        if price is not None:
            try:
                values.setdefault(_key(_normalize(float(price) / 100), "元"), set()).add(
                    "price_cents"
                )
            except (TypeError, ValueError):
                pass
        for number in _NUMBER.findall(str(row.get("dimensions_mm") or "")):
            values.setdefault(_key(_normalize(float(number)), "mm"), set()).add("dimensions_mm")
    return values


def scan_documents(knowledge_dir: Path, values: dict[str, set[str]]) -> list[dict[str, Any]]:
    """扫描 ``*.md``，返回命中商品字段值的告警。"""
    findings: list[dict[str, Any]] = []
    for path in sorted(knowledge_dir.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        seen: set[str] = set()
        for match in _UNIT_NUMBER.finditer(text):
            key = _key(_normalize(float(match.group("num"))), match.group("unit"))
            if key not in values or key in seen:
                continue
            seen.add(key)
            line = text[: match.start()].count("\n") + 1
            findings.append(
                {
                    "file": str(path),
                    "line": line,
                    "value": match.group(0).strip(),
                    "fields": sorted(values[key]),
                }
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描知识文档中泄露的商品参数")
    parser.add_argument("--pack", default="homeapp", help="行业包 id")
    parser.add_argument("--knowledge-dir", default=None, help="覆盖知识目录")
    parser.add_argument("--products", default=None, help="覆盖 products.yaml")
    parser.add_argument("--strict", action="store_true", help="有命中时退出 1")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    pack_dir = root / "zkagent" / "industry_packs" / args.pack
    products_path = Path(args.products) if args.products else pack_dir / "seed_data" / "products.yaml"
    knowledge_dir = Path(args.knowledge_dir) if args.knowledge_dir else pack_dir / "knowledge"

    if not products_path.is_file():
        print(json.dumps({"ok": False, "error": f"缺少商品种子：{products_path}"}, ensure_ascii=False))
        return 2
    if not knowledge_dir.is_dir():
        print(json.dumps({"ok": False, "error": f"缺少知识目录：{knowledge_dir}"}, ensure_ascii=False))
        return 2

    findings = scan_documents(knowledge_dir, collect_product_values(products_path))
    payload = {
        "ok": not findings,
        "pack": args.pack,
        "knowledge_dir": str(knowledge_dir),
        "products": str(products_path),
        "count": len(findings),
        "findings": findings[:50],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for item in findings:
            print(
                f"{item['file']}:{item['line']}: {item['value']} "
                f"（来自商品字段 {', '.join(item['fields'])}）"
            )
        print(f"共 {len(findings)} 处告警")
    return 0 if not findings or not args.strict else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
