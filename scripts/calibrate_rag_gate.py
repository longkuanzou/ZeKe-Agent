"""按行业包标定相关性闸门（M3）。

做法：用当前包的检索用例（正例）与一组固定无关查询（负例），各取稠密 top-1
相似度；正例最小值与负例最大值之间的中点即建议阈值。标定结果写入
``zkagent/industry_packs/<id>/evals/gate_calibration.json``，组合根会优先读取它。

用法：

```bash
python -m scripts.calibrate_rag_gate --industry appliance
```
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from zkagent.config import Container, load_settings, pack_file
from zkagent.core.rag.embedder import default_dense_min_similarity
from zkagent.core.rag.metrics import load_eval_set

__all__ = ["main"]

NEGATIVE_QUERIES = (
    "红楼梦金陵十二钗判词",
    "如何做红烧肉",
    "2026 年世界杯冠军",
    "Python 异步编程教程",
    "明天北京天气",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="标定检索相关性闸门")
    parser.add_argument("--industry", default="appliance")
    args = parser.parse_args(argv)

    container = Container.build(load_settings(industry_id=args.industry))
    retriever, bundle = container.build_retriever()
    retriever.load()
    eval_path = pack_file(container.paths.industry_pack_dir, container.pack.eval_retrieval)
    eval_set = load_eval_set(eval_path)

    # 临时关掉闸门，取原始 top-1 相似度
    retriever._dense_min_similarity = -1.0  # noqa: SLF001 - 标定脚本需要原始分布
    positives = []
    for case in eval_set.queries:
        hits = retriever._dense_hits(case.query, 1)  # noqa: SLF001
        if hits:
            positives.append(hits[0][1])
    negatives = []
    for query in NEGATIVE_QUERIES:
        hits = retriever._dense_hits(query, 1)  # noqa: SLF001
        if hits:
            negatives.append(hits[0][1])

    if not positives or not negatives:
        print(json.dumps({"ok": False, "error": "正例或负例没有召回"}, ensure_ascii=False))
        return 1

    lowest_positive = min(positives)
    highest_negative = max(negatives)
    overlap = lowest_positive <= highest_negative
    threshold = (
        default_dense_min_similarity(bundle.mode)
        if overlap
        else round((lowest_positive + highest_negative) / 2, 4)
    )
    payload = {
        "version": 1,
        "industry": container.pack.id,
        "embedding_mode": bundle.mode,
        "embed_model_id": bundle.embedder.model_id,
        "positive_top1_min": round(lowest_positive, 6),
        "negative_top1_max": round(highest_negative, 6),
        "threshold": threshold,
        #: 正负分布重叠 = 单一绝对阈值分不开「有依据」与「没依据」。
        #: 这时**不能**写 null（运行时就没有闸门了），退回该嵌入模式的默认阈值，
        #: 并把这个事实写进文件，别让读者以为标定成功（KNOWN_ISSUES M26）。
        "overlap": overlap,
        "positive_samples": len(positives),
        "negative_samples": len(negatives),
        "note": (
            "正负分布重叠：已退回默认阈值；需要相对阈值/两段式闸门，绝对阈值在这套语料上分不开"
            if overlap
            else "阈值取正例最小值与负例最大值的中间点"
        ),
    }
    target = pack_file(container.paths.industry_pack_dir, "evals/gate_calibration.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(target), **payload}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
