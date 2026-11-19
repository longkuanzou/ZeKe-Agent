"""Cross-encoder 精排（rerank）——**框架无关**（只用 ``sentence-transformers``）。

## 它解决什么问题

混合检索（稠密 + BM25 + RRF）解决的是**召回**：尽量别把该找的漏掉。但它的排序依据
（向量相似度、词频）都不直接等于「相关性」。cross-encoder 把「查询 + 候选」**成对**送进
模型，注意力能同时看到两边，区分度明显更高：

实测（行业包语料，bi-encoder 余弦 vs cross-encoder 分数）：

| 查询 | 候选片段 | bi-encoder 余弦 | cross-encoder 分数 |
| --- | --- | --- | --- |
| 保修期多长 | 整机保修一年…（命中） | 0.71 | **0.91** |
| 保修期多长 | 退换货条件说明…（干扰） | 0.62 | **0.00** |

可见 bi-encoder 对「相关 / 干扰」只差 0.09，cross-encoder 直接拉开到 0.91 / 0.00——
这就是精排的价值所在，代价是每个候选都要跑一次前向。

代价是慢：每个候选都要单独跑一次前向，所以只在**小候选集**上用（本实现只在 RRF 融合后的
top-N 上重排）。

## 失败时怎么办

模型不可用（未下载 / 无网络）时**不中断检索**：原样返回融合结果，并通过
:attr:`RerankOutcome.degraded` 与 ``detail`` 说明原因——与嵌入降级同一套报告纪律。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zkagent.core.types import Chunk

__all__ = [
    "DEFAULT_RERANKER_MODEL_ID",
    "CrossEncoderReranker",
    "RerankOutcome",
]

#: 默认精排模型（本地、CPU 可跑；约 1.1GB）。
DEFAULT_RERANKER_MODEL_ID = "BAAI/bge-reranker-base"


@dataclass(frozen=True)
class RerankOutcome:
    """精排结果 + 是否降级（必须随指标一同上报）。"""

    chunks: list[Chunk]
    degraded: bool = False
    detail: str = ""


class CrossEncoderReranker:
    """用 cross-encoder 对候选片段重排。

    :param model_id: 精排模型 id。
    :param device: ``cpu`` / ``cuda``。
    :param max_candidates: 最多重排多少条候选（上限越小越快）。
    """

    def __init__(
        self,
        model_id: str = DEFAULT_RERANKER_MODEL_ID,
        *,
        device: str = "cpu",
        max_candidates: int = 20,
    ) -> None:
        self.model_id = model_id
        self._device = device
        self._max_candidates = max(1, max_candidates)
        self._model: Any = None
        self._error: str | None = None

    @property
    def available(self) -> bool:
        """模型是否可用（不可用时 :meth:`rerank` 会原样返回）。"""
        if self._model is None and self._error is None:
            self._load()
        return self._model is not None

    def _load(self) -> None:
        """惰性加载模型；失败只记录原因，不抛异常（检索必须继续可用）。

        与嵌入实现同样的两条纪律（复用 ``embedder`` 里的内部工具）：**先只认本地缓存**
        （已下载过就不触网），并给 HF 的联网检查设上限——否则断网环境下精排会把检索拖挂数分钟。
        """
        try:
            from sentence_transformers import CrossEncoder

            from zkagent.core.rag.embedder import (
                _bound_hub_timeouts,
                _library_output_to_stderr,
                download_allowed,
            )

            _bound_hub_timeouts()
            kwargs: dict[str, Any] = {"device": self._device}
            try:
                with _library_output_to_stderr():
                    self._model = CrossEncoder(self.model_id, local_files_only=True, **kwargs)
            except Exception:
                if not download_allowed():
                    raise
                with _library_output_to_stderr():
                    self._model = CrossEncoder(self.model_id, **kwargs)
        except Exception as exc:  # 未下载 / 无网络 / 依赖缺失
            self._error = f"{type(exc).__name__}: {exc}"

    def rerank(self, query: str, chunks: list[Chunk], *, top_k: int | None = None) -> RerankOutcome:
        """对候选片段重排；返回的 ``score`` 是 cross-encoder 分数。

        降级时**原样返回**输入顺序（并给出原因），绝不返回空列表——那会让「模型没下好」
        伪装成「知识库没有依据」。
        """
        limit = top_k or len(chunks)
        if len(chunks) <= 1:
            return RerankOutcome(chunks=chunks[:limit])

        if not self.available:
            return RerankOutcome(
                chunks=chunks[:limit],
                degraded=True,
                detail=f"精排模型不可用（{self._error or '未加载'}），已退回 RRF 融合顺序",
            )

        candidates = chunks[: self._max_candidates]
        pairs = [(query, chunk.text) for chunk in candidates]
        scores = self._model.predict(pairs)
        rescored = [
            chunk.model_copy(update={"score": float(score)})
            for chunk, score in zip(candidates, scores, strict=False)
        ]
        rescored.sort(key=lambda chunk: (-chunk.score, chunk.node_id))
        return RerankOutcome(chunks=rescored[:limit])
