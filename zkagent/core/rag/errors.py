"""知识侧错误类型——**框架无关**，便于组合根与 CLI 统一上报。

拆成两个类型是为了让「索引没建」与「索引建了但这次检索失败」在日志与用户答复里可区分：
前者是部署/运维问题，后者才是检索问题。
"""

from __future__ import annotations

__all__ = ["RagError", "RagIndexError", "RetrievalError"]


class RagError(ValueError):
    """知识侧错误基类（继承 ``ValueError``：本质是配置/状态问题，不是未知崩溃）。"""


class RagIndexError(RagError):
    """索引缺失、损坏或与当前配置不兼容（如维度不符，需要 ``--rebuild``）。"""


class RetrievalError(RagError):
    """索引可用，但本次检索无法完成（如空查询）。"""
