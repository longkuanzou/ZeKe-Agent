"""L12：知识文档命中商品字段值时必须告警。"""

from __future__ import annotations

from pathlib import Path

from scripts.check_knowledge_params import collect_product_values, scan_documents


def _products(path: Path) -> Path:
    path.write_text(
        """
products:
  - sku: TEST-1
    price_cents: 329900
    dimensions_mm: 900×200×300
    capacity: 1.5匹 / 3500W
""".strip(),
        encoding="utf-8",
    )
    return path


def test_scan_flags_product_params_in_knowledge(tmp_path: Path) -> None:
    """验证：知识文档里出现商品硬参数（价格、尺寸）时被扫出，并标出文件与命中值。"""
    products = _products(tmp_path / "products.yaml")
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "bad.md").write_text("这款售价 3299 元，宽 900mm。", encoding="utf-8")
    (knowledge / "good.md").write_text("选购时先量门宽，再确认电梯尺寸。", encoding="utf-8")

    findings = scan_documents(knowledge, collect_product_values(products))

    assert findings
    files = {Path(item["file"]).name for item in findings}
    assert files == {"bad.md"}
    assert {item["value"] for item in findings} == {"3299 元", "900mm"}


def test_scan_ignores_numbers_without_units(tmp_path: Path) -> None:
    """验证：没有单位的裸数字不报——订单号、编号不是商品参数。"""
    products = _products(tmp_path / "products.yaml")
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "ok.md").write_text("订单号 3299，编号 900。", encoding="utf-8")

    assert scan_documents(knowledge, collect_product_values(products)) == []
