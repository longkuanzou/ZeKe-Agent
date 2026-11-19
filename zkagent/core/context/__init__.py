"""zkagent 上下文工程：静态前缀、轨迹隔离、状态栏、压缩与按需披露。

P2 只实现「静态前缀 + 轨迹隔离 + 状态栏」三件套；压缩与渐进披露在 P5 扩展。
"""

from zkagent.core.context.engine import ContextBundle, ContextEngine

__all__ = ["ContextEngine", "ContextBundle"]
