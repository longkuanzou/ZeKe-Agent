"""降级模式下的关键词覆盖度闸门（S1）。

hash 向量只有「字面相似度」，不相关查询的相似度反而可能更高（实测 0.33~0.36 vs
相关的 0.12~0.28），因此**不能**用余弦阈值判断相关性。此时改用查询 2-gram 在候选
文本中的覆盖率：

- 覆盖率低 → 候选只是「碰巧字面接近」，丢弃；
- 覆盖率高 → 至少查询里的关键词真的出现在文档里。

它不能替代语义相关性，只在 `fallback_hash` 模式作为兜底；`local_model` 仍用相似度闸门。
"""

from __future__ import annotations

import re

__all__ = [
    "DEFAULT_MIN_COVERAGE",
    "normalize_text",
    "text_coverage",
]

#: 降级模式默认覆盖率下限。低于它视为「语料里没有这段内容」。
DEFAULT_MIN_COVERAGE = 0.35

_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
#: 疑问/语气词不承载主题，留在 n-gram 里会稀释覆盖率（「保修几年」只剩 2/4）。
_STOPWORDS = re.compile(r"(请问|一下|什么|怎么|为什么|多少|几年|多久|吗|呢|的|了)")


def normalize_text(text: str) -> str:
    """去掉标点/空白/疑问词并转小写；中英文与数字保留。"""
    cleaned = _PUNCT.sub("", str(text).lower())
    return _STOPWORDS.sub("", cleaned)


def _ngrams(text: str, size: int = 2) -> set[str]:
    """切出字符 n-gram 集合（中文不依赖分词器）。

    :param text: 已归一化的文本。
    :param size: n 的取值，默认 2（bigram）。
    :returns: n-gram 集合；文本为空返回空集，短于 ``size`` 时返回原文本身。
    """
    if not text:
        return set()
    if len(text) <= size:
        return {text}
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def text_coverage(query: str, text: str, *, size: int = 2) -> float:
    """查询的 n-gram 有多大比例出现在候选文本里（0~1）。"""
    query_grams = _ngrams(normalize_text(query), size)
    if not query_grams:
        return 0.0
    candidate = normalize_text(text)
    if not candidate:
        return 0.0
    return len(query_grams & _ngrams(candidate, size)) / len(query_grams)
