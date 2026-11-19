"""zkagent.backends —— 业务后端适配器。

每个后端实现 ``zkagent.core.interfaces.BusinessBackend``：
把归一化工具映射到具体数据源，并把后端方言错误归一为统一错误码。
"""

__all__: list[str] = []
