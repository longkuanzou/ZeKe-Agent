"""策略与审批域的错误类型（错误码取值见 DESIGN §7.3 / §7.7.8）。

这里只定义**异常**，不负责转成 ``ToolResult``：策略层在编排节点里被调用，
它的失败必须打断本轮执行，而不是伪装成一次工具返回。
"""

from __future__ import annotations

__all__ = [
    "PolicyError",
    "RuleValidationError",
    "ApprovalError",
    "APPROVAL_ERROR_CODES",
]


class PolicyError(RuntimeError):
    """策略层通用错误（判定失败、事实缺失等）。"""


class RuleValidationError(PolicyError):
    """规则文件非法：未知算子 / 未知工具 / 结构不符。

    **加载期直接抛**，让进程起不来——规则写错时静默放行，比崩溃危险得多。
    """


class ApprovalError(PolicyError):
    """审批域错误，携带 DESIGN §7.7.8 定义的错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


#: 审批域错误码（扩展 §7.3 的取值域，不新增工具错误码）。
APPROVAL_ERROR_CODES: tuple[str, ...] = (
    "APPROVER_UNAUTHORIZED",
    "SELF_APPROVAL_FORBIDDEN",
    "APPROVAL_ALREADY_RESOLVED",
    "APPROVAL_EXPIRED",
    "APPROVAL_PARAMS_CHANGED",
    "APPROVAL_NOT_FOUND",
    "APPROVAL_NOT_APPROVED",
    "APPROVAL_NOT_CONSUMABLE",
    "POLICY_RECHECK_FAILED",
)
