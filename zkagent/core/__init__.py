"""zkagent.core —— 行业无关、后端无关、渠道无关的核心层。

本层**不得** import ``langgraph`` / ``langchain`` / ``llama_index``，
也不得 import 两个框架接缝实现（``langgraph_adapter`` / ``llamaindex_engine``）。
该约束由 ``tests/test_framework_boundary.py`` 静态扫描强制。
"""

__all__: list[str] = []
