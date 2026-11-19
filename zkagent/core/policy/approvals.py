"""HITL 审批：参数指纹、确定性审批号、状态机与仓储协议。

**不碰 SQL**：持久化由 ``ApprovalStore`` 的实现负责（见 ``backends/mock/approvals.py``）。
这样 Core 的审批语义可以脱离数据库单测，换存储也不改状态机。

状态机（DESIGN §7.7.4）::

    pending → approved / rejected / expired / cancelled
    approved → consumed（一次性，终态）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, Field

from zkagent.core.policy.errors import ApprovalError

__all__ = [
    "ApprovalRecord",
    "ApprovalStore",
    "MemoryApprovalStore",
    "approval_id_for",
    "fingerprint_of",
    "is_expired",
]

ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "cancelled"]


def fingerprint_of(tool_name: str, args: dict[str, Any]) -> str:
    """参数指纹：与执行器同口径（排序 + 去空），保证「审批的」就是「执行的」。"""
    from zkagent.core.tools.executor import request_fingerprint

    return request_fingerprint(tool_name, args)


def approval_id_for(thread_id: str, step: int, fingerprint: str) -> str:
    """确定性审批号：``interrupt`` 恢复时节点会重放，必须生成同一个 id。

    否则一次挂起会产生两条审批记录（DESIGN §7.7.1 I3 的隐蔽破坏方式）。
    """
    digest = sha256(f"{thread_id}|{step}|{fingerprint}".encode("utf-8")).hexdigest()
    return f"AP-{digest[:12].upper()}"


class ApprovalRecord(BaseModel):
    """一条审批记录（与 ``approvals`` 表字段一一对应）。"""

    approval_id: str
    thread_id: str | None = None
    session_id: str | None = None
    action: str
    payload_json: str = "{}"
    params_fingerprint: str
    status: ApprovalStatus = "pending"
    requester_id: str | None = None
    approver_id: str | None = None
    approver_mode: str = "dev_unauthenticated"
    approved_payload_json: str | None = None
    consumed_at: str | None = None
    execution_status: str = "not_started"
    execution_error_code: str | None = None
    reason: str = ""
    rule_id: str = ""
    #: 审批人必须同时具备的权限；空列表表示不限制
    required_permissions: list[str] = Field(default_factory=list)
    created_at: str = ""
    resolved_at: str | None = None

    @property
    def is_terminal(self) -> bool:
        """是否已到终态（不可再流转）。

        终态包括 ``rejected`` / ``expired`` / ``cancelled``，以及**已消费**
        （``consumed_at`` 非空）的 ``approved``——审批通过并执行完的也不该再动。

        :returns: 已到终态返回 ``True``，否则 ``False``。
        """
        return self.status in {"rejected", "expired", "cancelled"} or self.consumed_at is not None


def is_expired(record: ApprovalRecord, ttl_hours: int, *, now: datetime | None = None) -> bool:
    """判断审批单是否超过 TTL（**惰性判定**，不改写记录状态）。

    TTL 取值来自行业包 ``settings.approval_ttl_hours``；过期只在查询（CLI）
    与恢复（runner）时现算，没有后台任务扫表。

    :param record: 待判断的审批记录。
    :param ttl_hours: 有效小时数，需为正值。
    :param now: 参照时间，默认当前时间；主要供测试注入。
    :returns: 超时返回 ``True``；``created_at`` 为空或格式非法时返回 ``False``
        （宁可放过，也不要因脏数据把有效审批判死）。
    """
    if not record.created_at:
        return False
    try:
        created = datetime.strptime(record.created_at, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return (now or datetime.now()) - created > timedelta(hours=ttl_hours)


class ApprovalStore(ABC):
    """审批仓储协议（实现方负责原子性与并发）。"""

    @abstractmethod
    def create(self, record: ApprovalRecord) -> ApprovalRecord:
        """幂等创建：同 ``approval_id`` 已存在时返回已有记录，不覆盖。"""

    @abstractmethod
    def get(self, approval_id: str) -> ApprovalRecord | None:
        """按 id 取记录。"""

    @abstractmethod
    def list_pending(self, *, limit: int = 50) -> list[ApprovalRecord]:
        """列出待审批（按创建时间）。"""

    @abstractmethod
    def resolve(
        self,
        approval_id: str,
        *,
        approve: bool,
        approver_id: str,
        approver_mode: str,
        approved_payload_json: str | None = None,
        error_code: str | None = None,
    ) -> ApprovalRecord:
        """把 pending 迁移到 approved / rejected（**原子**，并发只有一个成功）。"""

    @abstractmethod
    def consume(self, approval_id: str) -> ApprovalRecord:
        """把 approved 原子地标记为已消费；已消费 / 非 approved 必须报错。"""

    @abstractmethod
    def finish(
        self,
        approval_id: str,
        *,
        execution_status: str,
        error_code: str | None = None,
    ) -> None:
        """回填业务执行结果（与审批状态正交）。"""


class MemoryApprovalStore(ApprovalStore):
    """进程内实现：只用于单测与本地试跑。"""

    def __init__(self) -> None:
        self._items: dict[str, ApprovalRecord] = {}

    def create(self, record: ApprovalRecord) -> ApprovalRecord:
        """幂等创建：同 ``approval_id`` 已存在时返回**原有记录**，不覆盖。

        :param record: 待创建的审批记录。
        :returns: 真实落库的那条记录（新建的或已存在的）。
        """
        existing = self._items.get(record.approval_id)
        if existing is not None:
            return existing
        self._items[record.approval_id] = record
        return record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        """按审批单号取记录。

        :param approval_id: 审批单号（形如 ``AP-XXXXXXXXXXXX``）。
        :returns: 存在则返回记录，不存在返回 ``None``。
        """
        return self._items.get(approval_id)

    def list_pending(self, *, limit: int = 50) -> list[ApprovalRecord]:
        """列出待审批记录，按创建时间升序（先到先审）。

        :param limit: 最多返回条数，默认 50。
        :returns: 状态为 ``pending`` 的记录列表，可能为空。
        """
        rows = [item for item in self._items.values() if item.status == "pending"]
        return sorted(rows, key=lambda item: item.created_at)[:limit]

    def resolve(
        self,
        approval_id: str,
        *,
        approve: bool,
        approver_id: str,
        approver_mode: str,
        approved_payload_json: str | None = None,
        error_code: str | None = None,
    ) -> ApprovalRecord:
        """把 ``pending`` 迁移到 ``approved`` / ``rejected``（**只允许一次**）。

        :param approval_id: 审批单号。
        :param approve: ``True`` 批准，``False`` 驳回。
        :param approver_id: 审批人标识，须来自可信配置，不取自对话内容。
        :param approver_mode: 审批人身份来源，``token`` 或 ``dev_unauthenticated``。
        :param approved_payload_json: 批准时**冻结**的参数 JSON；执行只认它，
            防止恢复后被改写。驳回时置空。
        :param error_code: 非空表示审批前校验已失败，直接抛错、不落库。
        :returns: 迁移后的审批记录。
        :raises ApprovalError: 记录不存在（``APPROVAL_NOT_FOUND``）、已处理过
            （``APPROVAL_ALREADY_RESOLVED``），或传入 ``error_code``。
        """
        record = self._items.get(approval_id)
        if record is None:
            raise ApprovalError("APPROVAL_NOT_FOUND", approval_id)
        if record.status != "pending" or record.consumed_at:
            raise ApprovalError("APPROVAL_ALREADY_RESOLVED", f"{approval_id} 当前为 {record.status}")
        if error_code:
            raise ApprovalError(error_code, f"审批校验失败：{approval_id}")
        updated = record.model_copy(
            update={
                "status": "approved" if approve else "rejected",
                "approver_id": approver_id,
                "approver_mode": approver_mode,
                "approved_payload_json": approved_payload_json if approve else None,
                "resolved_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
        self._items[approval_id] = updated
        return updated

    def consume(self, approval_id: str) -> ApprovalRecord:
        """把 ``approved`` 原子地标记为已消费（一次性，防重复执行）。

        :param approval_id: 审批单号。
        :returns: 填好 ``consumed_at`` 的审批记录。
        :raises ApprovalError: 记录不存在（``APPROVAL_NOT_FOUND``）、尚未批准
            （``APPROVAL_NOT_APPROVED``）或已被消费（``APPROVAL_NOT_CONSUMABLE``）。
        """
        record = self._items.get(approval_id)
        if record is None:
            raise ApprovalError("APPROVAL_NOT_FOUND", approval_id)
        if record.status != "approved":
            raise ApprovalError("APPROVAL_NOT_APPROVED", f"{approval_id} 当前为 {record.status}")
        if record.consumed_at:
            raise ApprovalError("APPROVAL_NOT_CONSUMABLE", f"{approval_id} 已被消费")
        updated = record.model_copy(
            update={"consumed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
        )
        self._items[approval_id] = updated
        return updated

    def finish(
        self, approval_id: str, *, execution_status: str, error_code: str | None = None
    ) -> None:
        """回填业务执行结果，与审批状态**正交**（失败不改写 ``status``）。

        :param approval_id: 审批单号。
        :param execution_status: 执行结果，``succeeded`` / ``failed`` 等。
        :param error_code: 执行失败的错误码，成功时为 ``None``。
        :raises ApprovalError: 记录不存在（``APPROVAL_NOT_FOUND``）。
        """
        record = self._items.get(approval_id)
        if record is None:
            raise ApprovalError("APPROVAL_NOT_FOUND", approval_id)
        self._items[approval_id] = record.model_copy(
            update={"execution_status": execution_status, "execution_error_code": error_code}
        )
