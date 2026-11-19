"""RAG 测试共用夹具。

**全部测试使用确定性 hash 兜底嵌入**：离线、可复现、毫秒级，不依赖模型下载。
真实本地模型的表现由 `bash tests/gates/run_p3.sh` 的 `real` 单点检查覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zkagent.core.rag.embedder import HashEmbedder
from zkagent.core.rag.llamaindex_engine import LlamaIndexRetriever

#: 测试语料：三篇文档，其中一篇带 ACL（internal），用于越权检索用例。
DEFAULT_DOCS: dict[str, str] = {
    "warranty.md": "整机保修一年，压缩机保修六年。保修期内非人为故障可免费维修。",
    "returns.md": "签收后七天内可申请无理由退货；未发货订单只能取消，不能创建退货单。",
    "internal/limits.md": "内部审批口径：超过自助额度的退款必须转人工审核，不得口头承诺。",
}

#: 测试元数据：warranty/returns 公开，internal/limits 需要 internal 标签。
DEFAULT_META = """version: 1
defaults:
  doc_type: faq
  acl_tags: []
  roles: []
docs:
  - doc_id: warranty.md
    doc_type: policy
  - doc_id: returns.md
    doc_type: policy
  - doc_id: internal/limits.md
    doc_type: internal
    acl_tags: [internal]
    roles: [agent]
"""


def write_knowledge(
    root: Path, docs: dict[str, str] | None = None, meta: str | None = DEFAULT_META
) -> Path:
    """把给定文档写入 ``root``，返回知识目录。"""
    knowledge = root / "knowledge"
    for doc_id, text in (docs or DEFAULT_DOCS).items():
        target = knowledge / doc_id
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if meta is not None:
        (knowledge / "meta.yaml").write_text(meta, encoding="utf-8")
    return knowledge


@pytest.fixture()
def make_retriever(tmp_path):
    """构造检索器的工厂：默认指向临时知识目录 + 临时索引目录。"""

    def _make(
        knowledge_dir: Path | None = None,
        *,
        index_dir: Path | None = None,
        industry_id: str = "appliance",
        dim: int = 64,
        chunk_size: int = 100,
        chunk_overlap: int = 10,
        top_k: int = 5,
        docs: dict[str, str] | None = None,
        meta: str | None = DEFAULT_META,
    ) -> LlamaIndexRetriever:
        directory = knowledge_dir or write_knowledge(tmp_path, docs, meta)
        return LlamaIndexRetriever(
            industry_id=industry_id,
            knowledge_dir=directory,
            index_dir=index_dir or (tmp_path / "indexes" / industry_id),
            embedder=HashEmbedder(dim),
            embedding_mode="fallback_hash",
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            top_k=top_k,
        )

    return _make
