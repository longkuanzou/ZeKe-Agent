"""zkagent —— 通用客服 / 业务办理 Agent。

分层约定（详见 docs/DESIGN.md）：
- ``zkagent.core``：行业无关、后端无关、渠道无关的核心（契约、工具、编排、安全、评估、可观测）。
- ``zkagent.backends``：业务后端适配器（订单 / 用户 / 工单 / 退款 / 物流）。
- ``zkagent.channels``：消息渠道适配器（CLI / HTTP / 未来的 IM）。
- ``zkagent.industry_packs``：行业差异的**数据**载体（规则、知识、话术、用例、种子数据）。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
