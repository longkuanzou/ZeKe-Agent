"""zkagent 编排层。

- ``langgraph_adapter.py``：**唯一**允许 import ``langgraph`` / ``langchain`` 的文件（框架接缝）。
- ``runner.py``：编排结果的框架无关映射（会话 → 线程、图状态 → ``AgentTurnResult``）。
"""

from zkagent.core.orchestrator.runner import thread_id_for, to_turn_result

__all__ = ["thread_id_for", "to_turn_result"]
