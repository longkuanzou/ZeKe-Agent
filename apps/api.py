"""P7 交付层：FastAPI 入口。

设计约束：
- API 只做「归一化消息 → AgentRunner → 归一化响应」，不重复业务校验；
- 业务校验、策略、审批仍在 Core / Container 内完成；
- 启动时装配 Container，配置或知识缺失立即失败，不拖到第一个请求。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import zkagent
from zkagent.channels.api import ApiChannel
from zkagent.config import Container, IndustryPackError, load_settings
from zkagent.core.llm import ProviderCallError, ProviderConfigError
from zkagent.core.policy.approvals import is_expired
from zkagent.core.policy.errors import ApprovalError
from zkagent.core.types import PolicyDecision

__all__ = ["ChatRequest", "ResolveRequest", "create_app", "app"]


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1)
    #: resume 时不需要新文本；普通 chat 由业务层校验空输入。
    text: str = ""
    user_id: str | None = None
    provider: str | None = None
    permissions: list[str] | None = None
    #: Fake 模型的确定性脚本（真实模型忽略；主要用于接口契约与 HITL 测试）。
    script: list[dict[str, Any]] | None = None
    #: 传入审批单号时走 resume，而不是重新开始一轮。
    approval_id: str | None = None


class ResolveRequest(BaseModel):
    approve: bool = True
    actor: str
    token: str | None = None
    permissions: list[str] = Field(default_factory=list)


def _approver_identity(settings, actor: str, token: str | None) -> tuple[str, str]:
    """与 CLI 同口径：只信配置里的 token，不接受对话自称。"""
    raw = str(getattr(settings, "hitl_approvers", "") or "").strip()
    if not raw:
        return actor, "dev_unauthenticated"
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        name, _, secret = item.partition(":")
        if name.strip():
            pairs[name.strip()] = secret.strip()
    if actor not in pairs:
        raise ApprovalError("APPROVER_UNAUTHORIZED", f"未知审批人：{actor}")
    if not token or token != pairs[actor]:
        raise ApprovalError("APPROVER_UNAUTHORIZED", "审批人 token 校验失败")
    return actor, "token_verified"


def _approver_permissions(settings, actor: str, extra: list[str]) -> set[str]:
    raw = str(getattr(settings, "hitl_approver_permissions", "") or "").strip()
    permissions = {item for item in extra if item}
    if raw:
        for item in raw.split(","):
            name, _, values = item.partition("=")
            if name.strip() != actor:
                continue
            permissions.update(
                value.strip() for value in values.replace(";", "|").split("|") if value.strip()
            )
    return permissions


def _approval_payload(record: Any) -> dict[str, Any]:
    return {
        "approval_id": record.approval_id,
        "status": record.status,
        "action": record.action,
        "execution_status": record.execution_status,
        "execution_error_code": record.execution_error_code,
        "rule_id": record.rule_id,
        "requester_id": record.requester_id,
        "approver_id": record.approver_id,
        "approver_mode": record.approver_mode,
        "reason": record.reason,
        "params_fingerprint": record.params_fingerprint,
        "required_permissions": record.required_permissions,
        "created_at": record.created_at,
    }


def create_app(settings=None, *, container: Container | None = None) -> FastAPI:
    """构造 API；测试可注入临时 Settings / Container。"""
    app = FastAPI(title="ZeKe-Agent API", version=zkagent.__version__)
    app.state.container = container or Container.build(
        settings or load_settings(), channel=ApiChannel()
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        current: Container = app.state.container
        settings_obj = current.settings
        hitl_mode = (
            "token_verified"
            if str(getattr(settings_obj, "hitl_approvers", "") or "").strip()
            else "dev_unauthenticated"
        )
        return {
            "status": "ok",
            "version": zkagent.__version__,
            "industry": current.pack.id,
            "backend": settings_obj.backend,
            "provider": settings_obj.provider,
            "hitl_mode": hitl_mode,
        }

    @app.post("/chat")
    def chat(body: ChatRequest) -> dict[str, Any]:
        current: Container = app.state.container
        if body.approval_id:
            return _resume(current, body)

        try:
            runner, ctx = current.make_runner(
                user_id=body.user_id,
                session_id=body.session_id,
                provider=body.provider,
                task=body.text,
                permissions=body.permissions,
                script=body.script,
            )
            result = runner.run(body.session_id, body.text, ctx)
            current.tracer.flush(result.trace_id or ctx.trace_id)
        except (ApprovalError, IndustryPackError, ProviderConfigError, ProviderCallError) as exc:
            raise _http_error(exc) from exc
        return ApiChannel.payload(result)

    @app.get("/hitl/{approval_id}")
    def hitl_status(approval_id: str) -> dict[str, Any]:
        current: Container = app.state.container
        record = current.approval_store.get(approval_id)
        if record is None:
            raise HTTPException(status_code=404, detail="approval 不存在")
        return _approval_payload(record)

    @app.post("/hitl/{approval_id}/resolve")
    def hitl_resolve(approval_id: str, body: ResolveRequest) -> dict[str, Any]:
        current: Container = app.state.container
        record = current.approval_store.get(approval_id)
        if record is None:
            raise HTTPException(status_code=404, detail="approval 不存在")
        if record.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"approval 当前为 {record.status}，不能重复处理",
            )
        ttl = current.policy_engine.settings.approval_ttl_hours
        if is_expired(record, ttl):
            raise HTTPException(status_code=409, detail="approval 已过期")
        try:
            actor, mode = _approver_identity(current.settings, body.actor, body.token)
            if current.policy_engine.settings.four_eyes and actor == record.requester_id:
                raise ApprovalError("SELF_APPROVAL_FORBIDDEN", "审批人不得与申请人同人")
            if body.approve and record.required_permissions:
                missing = set(record.required_permissions) - _approver_permissions(
                    current.settings, actor, body.permissions
                )
                if missing:
                    raise ApprovalError(
                        "APPROVER_UNAUTHORIZED",
                        f"审批人 {actor} 缺少权限：{', '.join(sorted(missing))}",
                    )
            updated = current.approval_store.resolve(
                approval_id,
                approve=body.approve,
                approver_id=actor,
                approver_mode=mode,
                approved_payload_json=record.payload_json if body.approve else None,
            )
        except ApprovalError as exc:
            raise _http_error(exc) from exc
        return _approval_payload(updated)

    @app.get("/traces/{trace_id}")
    def trace(trace_id: str) -> dict[str, Any]:
        current: Container = app.state.container
        path = Path(current.paths.trace_dir) / f"{trace_id}.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="trace 不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.post("/channels/{channel_id}/callback")
    def channel_callback(channel_id: str, request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=501,
            content={"detail": "渠道回调协议尚未实现", "channel_id": channel_id},
        )

    return app


def _http_error(exc: BaseException) -> HTTPException:
    code = getattr(exc, "code", "REQUEST_FAILED")
    status = 409 if code in {
        "APPROVAL_NOT_FOUND",
        "APPROVAL_ALREADY_RESOLVED",
        "APPROVAL_EXPIRED",
        "APPROVAL_ALREADY_CONSUMED",
        "APPROVAL_PARAMS_CHANGED",
        "APPROVER_UNAUTHORIZED",
        "SELF_APPROVAL_FORBIDDEN",
    } else 400
    return HTTPException(status_code=status, detail={"error_code": code, "error": str(exc)})


def _resume(current: Container, body: ChatRequest) -> dict[str, Any]:
    record = current.approval_store.get(body.approval_id or "")
    if record is None:
        raise HTTPException(status_code=404, detail="approval 不存在")
    if record.status == "pending":
        raise HTTPException(status_code=409, detail="审批尚未完成，请先 resolve")
    session = body.session_id or record.session_id or "api-session"
    try:
        runner, _ctx = current.make_runner(
            user_id=record.requester_id,
            session_id=session,
            provider=body.provider,
            # resume 不再走模型决策；给 Fake 一个合法脚本，避免「无 task」被当成配置错误。
            script=[{"content": "审批恢复"}],
        )
        decision = PolicyDecision(
            action=record.action,
            allowed=record.status == "approved",
            decision="require_confirmation",
            reason=record.reason,
            rule_id=record.rule_id,
            approval_id=record.approval_id,
            approver_id=record.approver_id,
            approver_mode=record.approver_mode,
        )
        result = runner.resume(session, decision)
        current.tracer.flush(result.trace_id or "")
    except (ApprovalError, ProviderConfigError, ProviderCallError) as exc:
        raise _http_error(exc) from exc
    return ApiChannel.payload(result)


app = create_app()
