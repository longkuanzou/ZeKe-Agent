"""归一化工具声明的共享入口。

工具 schema 是「业务后端能力」的唯一事实来源：MockBackend 与 HttpBackend
必须声明同一组 ``ToolSpec``，否则换后端时模型看到的工具面会变化。

当前声明仍定义在 ``mock/backend.py``（P1 起的事实来源）；本模块只做稳定出口，
避免第二个后端直接依赖 MockBackend 的实现细节。
"""

from __future__ import annotations

from zkagent.backends.mock.backend import TOOL_SPECS

__all__ = ["TOOL_SPECS"]
