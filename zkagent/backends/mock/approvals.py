"""审批表的 SQLite 仓储：状态迁移与消费必须**原子**。

并发下最容易出的两个事故：

1. 两个 `approve` 同时成功 → 一条审批被批两次（本实现用 ``WHERE status='pending'`` 兜住）；
2. 两个 `resume` 同时消费 → 一笔退款执行两次（本实现用 ``WHERE consumed_at IS NULL`` 兜住）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from zkagent.backends.mock.db import Database
from zkagent.core.policy.approvals import ApprovalRecord, ApprovalStore
from zkagent.core.policy.errors import ApprovalError

__all__ = ["SqliteApprovalStore"]

_FIELDS = (
    "approval_id",
    "thread_id",
    "session_id",
    "action",
    "payload_json",
    "params_fingerprint",
    "status",
    "requester_id",
    "approver_id",
    "approver_mode",
    "approved_payload_json",
    "consumed_at",
    "execution_status",
    "execution_error_code",
    "reason",
    "rule_id",
    "required_permissions",
    "created_at",
    "resolved_at",
)


def _now() -> str:
    """当前时间，格式与库内其它时间列一致（``YYYY-MM-DDTHH:MM:SS``）。

    :returns: 秒级时间字符串。SQLite 里时间统一存字符串，便于直接比较与排序。
    """
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class SqliteApprovalStore(ApprovalStore):
    """基于 ``approvals`` 表的审批仓储。"""

    def __init__(self, db: Database) -> None:
        self._db = db
        db.initialize()

    def _to_record(self, row: Any) -> ApprovalRecord:
        """把数据库行还原成 :class:`ApprovalRecord`。

        ``required_permissions`` 在库里存的是 JSON 文本，需要反序列化；解析失败时
        退化为空列表（宁可少权限，也不要让审批单读不出来）。

        :param row: ``sqlite3.Row``，列名与 ``_FIELDS`` 对应（缺列会被跳过）。
        :returns: 审批记录模型。
        """
        data = {name: row[name] for name in _FIELDS if name in row.keys()}
        raw = data.get("required_permissions")
        if isinstance(raw, str):
            try:
                data["required_permissions"] = json.loads(raw)
            except ValueError:
                data["required_permissions"] = []
        return ApprovalRecord.model_validate(data)

    def create(self, record: ApprovalRecord) -> ApprovalRecord:
        """幂等创建审批单（``INSERT OR IGNORE`` + 回读）。

        主键冲突说明这张单已经存在（``interrupt`` 重放的正常情况），此时**不覆盖**
        原记录，直接返回库里那条——``created_at`` 也因此不会被刷新，TTL 仍从首次
        挂起开始算。

        :param record: 待创建的审批记录。
        :returns: 库中真实存在的那条记录（新建的或既有的）。
        :raises ApprovalError: 插入后仍查不到记录（理论不可达，属数据损坏兜底）。
        """
        values = record.model_dump()
        columns = ", ".join(f'"{name}"' for name in _FIELDS)
        marks = ", ".join("?" for _ in _FIELDS)
        with self._db.transaction() as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO approvals ({columns}) VALUES ({marks})",
                [
                    json.dumps(values.get(name) or [], ensure_ascii=False)
                    if name == "required_permissions"
                    else values.get(name)
                    for name in _FIELDS
                ],
            )
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (record.approval_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - 插入后必然存在
            raise ApprovalError("APPROVAL_NOT_FOUND", record.approval_id)
        return self._to_record(row)

    def get(self, approval_id: str) -> ApprovalRecord | None:
        """按审批单号取记录。

        :param approval_id: 审批单号。
        :returns: 存在则返回记录，不存在返回 ``None``。
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return self._to_record(row) if row is not None else None

    def list_pending(self, *, limit: int = 50) -> list[ApprovalRecord]:
        """列出待审批记录，按创建时间升序（同秒时用单号兜底，保证顺序可复现）。

        :param limit: 最多返回条数，默认 50。
        :returns: 状态为 ``pending`` 的记录列表。
        """
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status = 'pending' "
                "ORDER BY created_at, approval_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._to_record(row) for row in rows]

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
        """把 ``pending`` 原子地迁移到 ``approved`` / ``rejected``。

        **冲突靠 SQL 判断，不靠先查后写**：``WHERE status = 'pending'`` 让并发下
        只有一个调用能更新到行，其余 ``rowcount == 0``；此时回读一次区分「不存在」
        与「已被处理」，并给出对应错误码。

        :param approval_id: 审批单号。
        :param approve: ``True`` 批准，``False`` 驳回。
        :param approver_id: 审批人标识，须来自可信配置，不取自对话内容。
        :param approver_mode: 审批人身份来源，``token`` 或 ``dev_unauthenticated``。
        :param approved_payload_json: 批准时冻结的参数 JSON；驳回时忽略。
        :param error_code: 非空表示审批前校验（权限 / 四眼 / TTL / 指纹）已失败，
            直接抛错、不落库。
        :returns: 迁移后的审批记录。
        :raises ApprovalError: ``error_code`` 非空、记录不存在
            （``APPROVAL_NOT_FOUND``）或状态已非 ``pending``
            （``APPROVAL_ALREADY_RESOLVED``）。
        """
        if error_code:
            raise ApprovalError(error_code, f"审批校验失败：{approval_id}")
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE approvals SET status = ?, approver_id = ?, approver_mode = ?, "
                "approved_payload_json = ?, resolved_at = ? "
                "WHERE approval_id = ? AND status = 'pending' AND consumed_at IS NULL",
                (
                    "approved" if approve else "rejected",
                    approver_id,
                    approver_mode,
                    approved_payload_json if approve else None,
                    _now(),
                    approval_id,
                ),
            )
            if cursor.rowcount == 0:
                row = conn.execute(
                    "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
                ).fetchone()
                if row is None:
                    raise ApprovalError("APPROVAL_NOT_FOUND", approval_id)
                raise ApprovalError(
                    "APPROVAL_ALREADY_RESOLVED", f"{approval_id} 当前为 {row['status']}"
                )
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return self._to_record(row)

    def consume(self, approval_id: str) -> ApprovalRecord:
        """把 ``approved`` 原子地标记为已消费（防同一笔退款执行两次）。

        ``WHERE status = 'approved' AND consumed_at IS NULL`` 兜住并发恢复：
        只有一个 ``resume`` 能拿到 ``rowcount == 1``，其余按原因报不同错误码——
        ``APPROVAL_NOT_CONSUMABLE`` 表示「别人已执行」（可安全跳过），
        ``APPROVAL_NOT_APPROVED`` 表示「流程顺序错了」。

        :param approval_id: 审批单号。
        :returns: 填好 ``consumed_at`` 的审批记录。
        :raises ApprovalError: 记录不存在、尚未批准或已被消费。
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE approvals SET consumed_at = ? "
                "WHERE approval_id = ? AND status = 'approved' AND consumed_at IS NULL",
                (_now(), approval_id),
            )
            if cursor.rowcount == 0:
                row = conn.execute(
                    "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
                ).fetchone()
                if row is None:
                    raise ApprovalError("APPROVAL_NOT_FOUND", approval_id)
                code = (
                    "APPROVAL_NOT_CONSUMABLE"
                    if row["consumed_at"]
                    else "APPROVAL_NOT_APPROVED"
                )
                raise ApprovalError(code, f"{approval_id} 当前为 {row['status']}")
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return self._to_record(row)

    def finish(
        self, approval_id: str, *, execution_status: str, error_code: str | None = None
    ) -> None:
        """回填业务执行结果，**只动执行两列**，不改审批状态。

        「人同不同意」与「业务成没成」分开存，才能回答「审批通过了但执行失败」。

        :param approval_id: 审批单号。
        :param execution_status: 执行结果，取值受表约束限制
            （``not_started`` / ``succeeded`` / ``failed``）。
        :param error_code: 执行失败的错误码，成功时为 ``None``。
        """
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE approvals SET execution_status = ?, execution_error_code = ? "
                "WHERE approval_id = ?",
                (execution_status, error_code, approval_id),
            )
