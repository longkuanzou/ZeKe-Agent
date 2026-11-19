"""本地嵌入实现与失败降级——**框架无关**（不 import ``llama_index`` / ``langchain``）。

对应 DESIGN §6.3.6：

| 情形 | 行为 |
| --- | --- |
| 本地模型不可用（无网络 / 未下载 / 依赖缺失） | 降级为**确定性 hash 向量**，流程不中断，BM25 通路仍完整 |
| 已有索引但嵌入模型不一致 | **拒绝增量**并要求 ``--rebuild``（由 manifest 的 ``embed_model_id`` 判定） |

关键设计：降级后 ``model_id`` 会变成 ``deterministic-hash-ngram2``，与真实模型的 id 不同，
因此**切换嵌入实现必然触发「嵌入模型不匹配 → 重建」**，不会把两种向量混进同一个索引
（那会让检索指标不可比，且错误极难察觉）。

报告纪律：任何检索指标都必须携带 ``embedding_mode``（``local_model`` / ``fallback_hash``），
降级口径的数字**不得**与正常口径混在同一条基线里比较。
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from zkagent.core.interfaces import Embedder

__all__ = [
    "DEFAULT_EMBED_MODEL_ID",
    "DEFAULT_EMBED_DIM",
    "LOCAL_MODE",
    "FALLBACK_MODE",
    "HashEmbedder",
    "SentenceTransformerEmbedder",
    "EmbedderBundle",
    "build_embedder",
    "default_dense_min_similarity",
    "LOCAL_DENSE_MIN_SIMILARITY",
    "FALLBACK_DENSE_MIN_SIMILARITY",
]

#: 默认本地嵌入模型（DESIGN §6.3；中文效果好、体积小、CPU 可跑）。
DEFAULT_EMBED_MODEL_ID = "BAAI/bge-small-zh-v1.5"

#: 默认向量维度（bge-small-zh-v1.5 = 512）。
DEFAULT_EMBED_DIM = 512

#: 嵌入模式取值（必须出现在指标报告里）。
LOCAL_MODE = "local_model"
FALLBACK_MODE = "fallback_hash"


#: 是否允许联网下载模型（``0``/``false`` 表示只用本地缓存，缺失即降级）。
ALLOW_DOWNLOAD_ENV = "ZKAGENT_EMBED_ALLOW_DOWNLOAD"


def download_allowed() -> bool:
    """按环境变量判断是否允许联网下载模型（默认允许）。"""
    return os.environ.get(ALLOW_DOWNLOAD_ENV, "1").strip().lower() not in {"0", "false", "no"}


def _bound_hub_timeouts() -> None:
    """给 HuggingFace 的联网检查设置上限（不覆盖用户已有设置）。

    **为什么必须有这个**：``sentence-transformers`` 每次加载模型都会对模型仓库做
    HEAD 检查；网络不可达时会按 1/2/4/8/16 秒重试，单条命令因此挂起数分钟
    （实测：已缓存的模型在断网环境下加载 >150 秒仍未返回，直接让 P3 Gate 超时）。
    这不是「下载慢」，而是「已经缓存好了却还在联网」。
    """
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "5")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "10")
    # 关闭匿名遥测：它**不遵守** ``local_files_only``，在「模型已缓存、应当完全离线」的
    # 场景下仍会发起外连（实测 2 次 connect），让「不触网」这件事不再成立。
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


@contextlib.contextmanager
def _library_output_to_stderr() -> Iterator[None]:
    """把模型加载期的 Python 级 stdout 输出改道到 stderr。

    背景：``transformers`` / ``sentence-transformers`` 在加载模型时会往 stdout 写
    进度与提示（实测会留下一个空行）。而 ``zkagent`` 的子命令把 **stdout 当作结构化输出通道**
    （``rag index`` 打印 JSON），被塞入空行会导致下游 ``json.load`` 直接失败。
    因此这里只改道「库的加载期输出」，命令自身的结构化输出仍写 stdout。
    """
    with contextlib.redirect_stdout(sys.stderr):
        yield


#: 真实模型的稠密相似度下限（相关性闸门），实测标定见 ``llamaindex_engine.DEFAULT_DENSE_MIN_SIMILARITY``。
LOCAL_DENSE_MIN_SIMILARITY = 0.45

#: 降级模式的稠密相似度下限：**关闭闸门**。
#:
#: 实测（appliance 语料，hash 嵌入）：不相关查询 top-1 相似度 0.33~0.36，
#: 反而**高于**相关查询的 0.12~0.28——即 hash 向量的相似度不是相关性信号，
#: 用它设阈值只会误杀。因此降级模式下不设闸门，并接受「可能把无关内容当作依据」的代价，
#: 该局限记录在 ``docs/KNOWN_ISSUES.md``。
FALLBACK_DENSE_MIN_SIMILARITY = 0.0


def default_dense_min_similarity(mode: str) -> float:
    """按嵌入模式返回默认相关性闸门。

    :param mode: :data:`LOCAL_MODE` 或 :data:`FALLBACK_MODE`。
    """
    return LOCAL_DENSE_MIN_SIMILARITY if mode == LOCAL_MODE else FALLBACK_DENSE_MIN_SIMILARITY


def _l2_normalize(vector: list[float]) -> list[float]:
    """L2 归一化；全零向量回退为单位基（避免除零）。"""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        vector = [0.0] * len(vector)
        if vector:
            vector[0] = 1.0
        return vector
    return [value / norm for value in vector]


class HashEmbedder(Embedder):
    """确定性 hash 向量：**不是**假数据，但没有语义能力。

    做法：字符 n-gram（含单字）经 SHA-256 映射到固定维度桶，带符号累加后 L2 归一化。
    它提供的是**字面相似度**，因此「同词命中」有效、「换种说法」基本失效——
    这正是必须在报告里标注降级口径的原因。
    """

    def __init__(self, dim: int = DEFAULT_EMBED_DIM, *, ngram: int = 2) -> None:
        if dim <= 0:
            raise ValueError("dim 必须为正整数")
        if ngram <= 0:
            raise ValueError("ngram 必须为正整数")
        self._dim = dim
        self._ngram = ngram

    @property
    def model_id(self) -> str:
        """嵌入实现标识（写入 manifest，用于判定是否需要重建）。"""
        return f"deterministic-hash-ngram{self._ngram}"

    @property
    def dim(self) -> int:
        """向量维度。"""
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """把文本编码为确定性向量（同一输入永远得到同一结果）。"""
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        """把单条文本映射成归一化的确定性向量（hash 分桶 + 符号）。

        同一输入必得同一向量，是「索引可复现」的前提；归一化后余弦相似度
        可直接用点积计算。

        :param text: 待编码文本（内部会去多余空白并转小写）。
        :returns: 长度为 ``dim`` 的 L2 归一化向量。
        """
        normalized = " ".join(text.split()).lower()
        vector = [0.0] * self._dim
        for gram in self._grams(normalized):
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self._dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign
        return _l2_normalize(vector)

    def _grams(self, text: str) -> list[str]:
        """字符 n-gram：单字 + 连续 n 字（对中文有效，且不依赖分词器）。"""
        if not text:
            return []
        chars = list(text)
        grams = list(chars)
        for size in range(2, self._ngram + 1):
            grams.extend(
                "".join(chars[index : index + size]) for index in range(len(chars) - size + 1)
            )
        return grams


class SentenceTransformerEmbedder(Embedder):
    """本地 sentence-transformers 嵌入（真正的语义向量）。

    ``sentence_transformers`` 在**首次使用时**才导入：无网络 / 未安装 / 未下载模型时
    由 :func:`build_embedder` 捕获并降级，而不是让整个进程崩掉。
    """

    def __init__(
        self,
        model_id: str = DEFAULT_EMBED_MODEL_ID,
        *,
        device: str = "cpu",
        cache_dir: str | Path | None = None,
        batch_size: int = 32,
        allow_download: bool | None = None,
    ) -> None:
        from sentence_transformers import SentenceTransformer  # 延迟导入

        _bound_hub_timeouts()
        self._model_id = model_id
        self._batch_size = batch_size
        may_download = download_allowed() if allow_download is None else allow_download
        load_kwargs = {
            "device": device,
            "cache_folder": str(cache_dir) if cache_dir else None,
        }
        try:
            with _library_output_to_stderr():  # 不让库的加载提示污染命令的结构化输出
                # 先只认本地缓存：已下载过的模型**完全不触网**（离线环境必须能秒开）
                self._model = SentenceTransformer(
                    model_id, local_files_only=True, **load_kwargs
                )
        except Exception:
            if not may_download:
                raise  # 明确禁止下载时，缓存缺失就如实失败（由上层降级为 hash）
            with _library_output_to_stderr():
                self._model = SentenceTransformer(model_id, **load_kwargs)
        self._dim = self._detect_dim()

    def _detect_dim(self) -> int:
        """从已加载的模型读取向量的维度。

        不同版本 sentence-transformers 的方法名不一样，因此两种都试。

        :returns: 向量维度（正整数）。
        :raises ValueError: 两个方法都取不到维度，或返回非正值。
        """
        getter = getattr(self._model, "get_embedding_dimension", None) or getattr(
            self._model, "get_sentence_embedding_dimension", None
        )
        dim = int(getter()) if getter else 0
        if dim <= 0:
            raise ValueError(f"无法确定嵌入维度：{self._model_id}")
        return dim

    @property
    def model_id(self) -> str:
        """嵌入模型 id（写入 manifest）。"""
        return self._model_id

    @property
    def dim(self) -> int:
        """向量维度。"""
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量编码；返回 Python 列表（避免 numpy 类型泄漏到契约层）。"""
        if not texts:
            return []
        vectors = self._model.encode(
            texts, batch_size=self._batch_size, normalize_embeddings=True
        )
        return [[float(value) for value in row] for row in vectors]


@dataclass(frozen=True)
class EmbedderBundle:
    """嵌入实现 + 模式 + 降级原因（模式必须进报告）。"""

    embedder: Embedder
    mode: str
    detail: str = ""

    @property
    def degraded(self) -> bool:
        """是否处于降级模式。"""
        return self.mode != LOCAL_MODE

    def describe(self) -> str:
        """人类可读描述。"""
        base = f"embedding_mode={self.mode}（{self.embedder.model_id}, dim={self.embedder.dim}）"
        return f"{base}；原因：{self.detail}" if self.detail else base


def build_embedder(
    model_id: str = DEFAULT_EMBED_MODEL_ID,
    *,
    prefer_local: bool = True,
    cache_dir: str | Path | None = None,
    fallback_dim: int = DEFAULT_EMBED_DIM,
) -> EmbedderBundle:
    """构造嵌入实现，失败时显式降级并说明原因。

    :param prefer_local: 为 ``False`` 时直接使用 hash 兜底（离线测试与快速 CI 用）。
    """
    if not prefer_local:
        return EmbedderBundle(
            embedder=HashEmbedder(fallback_dim),
            mode=FALLBACK_MODE,
            detail="调用方显式要求不使用本地模型（离线/快速模式）",
        )
    try:
        return EmbedderBundle(
            embedder=SentenceTransformerEmbedder(model_id, cache_dir=cache_dir),
            mode=LOCAL_MODE,
        )
    except Exception as exc:  # 下载失败 / 依赖缺失 / 模型损坏
        return EmbedderBundle(
            embedder=HashEmbedder(fallback_dim),
            mode=FALLBACK_MODE,
            detail=f"本地模型不可用（{type(exc).__name__}: {exc}）",
        )
