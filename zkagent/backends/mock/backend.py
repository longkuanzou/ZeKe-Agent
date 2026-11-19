"""基于 SQLite 的模拟业务后端（默认后端，零外部服务）。

**职责边界（重要）**

- 本后端只做**数据完整性与归属校验**：对象是否存在、是否属于当前用户、金额是否合法、
  当前状态是否允许该动作、幂等键是否重复——任何真实业务系统都会做这些服务端校验。
- **业务规则判定**（金额阈值、时限、是否转人工、Poka-yoke 引导）由 ``PolicyEngine``
  依据行业包 ``policies/rules.yaml`` 决定（P4 落地）；后端**不参与**，也**不得**把规则写进代码。

所有写操作在同一事务内完成，并落 ``audit_logs``（前后状态快照）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

from zkagent.backends.mock.db import Database, utc_now
from zkagent.core.interfaces import BusinessBackend
from zkagent.core.tools.schema import ErrorCode, make_error, make_ok
from zkagent.core.types import ToolContext, ToolResult, ToolSpec

__all__ = ["MockBackend", "TOOL_SPECS"]

#: 订单状态全集（与建表约束保持一致）。
ORDER_STATUSES = ("created", "paid", "shipped", "delivered", "cancelled")
#: 允许取消的订单状态（已签收 / 已取消不可取消）。
CANCELLABLE_STATUSES = ("created", "paid", "shipped")
#: 允许创建退货退款单的订单状态（只有已签收才谈得上退货）。
REFUNDABLE_STATUSES = ("delivered",)
#: 服务工单类型全集。
SERVICE_TYPES = ("install", "repair", "maintenance")
#: 商品品类全集（与 ``products`` 表约束、行业包 ``pack.yaml`` 的 products 对齐）。
PRODUCT_CATEGORIES = ("aircon", "fridge", "washer", "rice_cooker", "phone", "tablet")
#: 商品检索的默认 / 最大返回条数（避免一次性把整张目录塞进上下文）。
DEFAULT_PRODUCT_LIMIT = 5

#: 安装收费（**模拟值**，仅用于演示；真实报价以下单页与师傅现场为准）。
INSTALL_BASE_FEES_CENTS = {
    "aircon": 20000,
    "washer": 10000,
    "fridge": 15000,
    "rice_cooker": 0,
    "phone": 0,
    "tablet": 0,
}
#: 无电梯时，3 层以上每层加收。
FLOOR_FEE_PER_LEVEL_CENTS = 5000
#: 外机位需要吊装时的加收。
LIFT_FEE_CENTS = 30000
MAX_PRODUCT_LIMIT = 20


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """构造 JSON Schema 对象描述。"""
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _s(description: str) -> dict[str, str]:
    """声明一个字符串参数（JSON Schema 片段）。

    :param description: 给模型看的参数说明，会原样进入工具 schema。
    :returns: ``{"type": "string", "description": ...}``。
    """
    return {"type": "string", "description": description}


def _i(description: str) -> dict[str, str]:
    """声明一个整数参数（JSON Schema 片段）。

    :param description: 给模型看的参数说明，需写明单位（通常为「分」）。
    :returns: ``{"type": "integer", "description": ...}``。
    """
    return {"type": "integer", "description": description}


#: 归一化标准工具的声明——``ToolSpec`` 是工具定义的唯一事实来源。
TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_user",
        description="查询**当前用户**基本信息（手机号与地址已脱敏），不需要传用户 id",
        parameters=_obj({"user_id": _s("用户标识（可省略，默认当前用户）")}, []),
        risk="low",
        idempotent=True,
        tags=["user", "profile"],
    ),
    ToolSpec(
        name="get_user_home",
        description=(
            "查询**当前用户**已登记的房屋条件（楼层/电梯/门宽/水电位/外机位），"
            "用于判断能不能装、要不要上楼费。不需要传用户 id"
        ),
        parameters=_obj({"user_id": _s("用户标识（可省略，默认当前用户）")}, []),
        risk="low",
        idempotent=True,
        tags=["user", "home", "install"],
    ),
    ToolSpec(
        name="verify_user",
        description=(
            "校验**当前用户**身份：请用户提供下单手机号后四位作为校验码（模拟短信验证码），"
            "不需要传用户 id"
        ),
        parameters=_obj({"factor": _s("校验码（手机号后四位）"),
                         "user_id": _s("用户标识（可省略，默认当前用户）")}, ["factor"]),
        risk="medium",
        idempotent=True,
        tags=["user", "identity"],
    ),
    ToolSpec(
        name="get_order",
        description="查询订单详情（仅限本人订单）",
        parameters=_obj({"order_id": _s("订单号")}, ["order_id"]),
        risk="low",
        idempotent=True,
        tags=["order"],
    ),
    ToolSpec(
        name="list_orders",
        description="按状态列出**当前用户**的订单（仅限本人），不需要传用户 id",
        parameters=_obj(
            {"status": _s(f"订单状态，可选：{'/'.join(ORDER_STATUSES)}"),
             "user_id": _s("用户标识（可省略，默认当前用户）")},
            [],
        ),
        risk="low",
        idempotent=True,
        tags=["order", "list"],
    ),
    ToolSpec(
        name="search_products",
        description=(
            "按品类/关键词/功能/价格上限检索商品，返回型号与关键参数"
            "（容量、能效、尺寸、价格、功能）。售前的型号与参数问题必须用它，不要凭知识库推测"
        ),
        parameters=_obj(
            {
                "category": _s(f"品类，可选：{'/'.join(PRODUCT_CATEGORIES)}"),
                "keyword": _s("关键词，匹配型号/名称/容量，如 滚筒、501L"),
                "feature": _s("必须支持的功能，如 烘干、新风、自清洁、自动集尘"),
                "max_price_cents": _i("价格上限（单位：分）"),
                "include_off_shelf": {
                    "type": "boolean",
                    "description": "是否包含已下架机型（默认否；查「同型号现售价」时应包含）",
                },
                "limit": _i("返回条数上限（默认 5，最大 20）"),
            },
            [],
        ),
        risk="low",
        idempotent=True,
        tags=["product", "read"],
    ),
    ToolSpec(
        name="get_product",
        description="按 SKU 查询商品完整参数（型号、容量、能效、尺寸、价格、功能、安装条件）",
        parameters=_obj({"sku": _s("商品 SKU（可从订单详情中获取）")}, ["sku"]),
        risk="low",
        idempotent=True,
        tags=["product", "read"],
    ),
    ToolSpec(
        name="cancel_order",
        description="取消订单（已签收或已取消的订单不可取消）",
        parameters=_obj({"order_id": _s("订单号"), "reason": _s("取消原因")}, ["order_id", "reason"]),
        risk="high",
        idempotent=True,
        requires_idempotency_key=True,
        confirmation_required=True,
        tags=["order", "write", "risky"],
    ),
    ToolSpec(
        name="create_refund_ticket",
        description="创建退货退款单（仅已签收订单；未发货订单应改为取消订单）",
        parameters=_obj(
            {
                "order_id": _s("订单号"),
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "退货商品，缺省为整单",
                },
                "amount_cents": _i("退款金额（单位：分）"),
                "reason": _s("退款原因"),
            },
            ["order_id", "amount_cents", "reason"],
        ),
        risk="high",
        idempotent=True,
        requires_idempotency_key=True,
        confirmation_required=True,
        tags=["refund", "write", "risky"],
    ),
    ToolSpec(
        name="get_refund_ticket",
        description="查询退款进度：给退款单号或订单号均可（用户通常只知道订单号）",
        parameters=_obj(
            {"ticket_id": _s("退款单号（可选）"), "order_id": _s("订单号（可选）")}, []
        ),
        risk="low",
        idempotent=True,
        tags=["refund"],
    ),
    ToolSpec(
        name="get_invoice",
        description="查询订单发票状态（普票/专票、待开/已开/已红冲）",
        parameters=_obj({"order_id": _s("订单号")}, ["order_id"]),
        risk="low",
        idempotent=True,
        tags=["invoice", "read"],
    ),
    ToolSpec(
        name="submit_invoice_request",
        description=(
            "为已支付订单申请开票（普票/专票），提交后状态为待开；"
            "已开票或已红冲的订单不允许重复申请"
        ),
        parameters=_obj(
            {
                "order_id": _s("订单号"),
                "kind": _s("发票类型：normal（普票）/ special（专票），默认 normal"),
                "title": _s("发票抬头（可省略，默认使用账户名称）"),
            },
            ["order_id"],
        ),
        risk="medium",
        idempotent=True,
        requires_idempotency_key=True,
        tags=["invoice", "write"],
    ),
    ToolSpec(
        name="list_promotions",
        description=(
            "查询当前进行中的优惠活动（直降/满减/赠品/以旧换新/分期），"
            "可按 SKU 或品类过滤；价格类问题必须用它，不要凭知识库推测"
        ),
        parameters=_obj(
            {
                "sku": _s("商品 SKU（可省略）"),
                "category": _s(f"品类，可选：{'/'.join(PRODUCT_CATEGORIES)}"),
            },
            [],
        ),
        risk="low",
        idempotent=True,
        tags=["product", "promotion", "read", "presale"],
    ),
    ToolSpec(
        name="get_installation_quote",
        description=(
            "按 SKU 与用户房屋条件估算安装可行性、收费明细与是否需要现场勘测"
            "（基础安装费、无电梯上楼费、吊装费；报价为模拟值）"
        ),
        parameters=_obj(
            {"sku": _s("商品 SKU"), "home_id": _s("房屋编号（可省略，默认当前用户默认房屋）")},
            ["sku"],
        ),
        risk="low",
        idempotent=True,
        tags=["product", "install", "read"],
    ),
    ToolSpec(
        name="approve_refund",
        description="审批通过退款单（仅在待确认状态下有效）",
        parameters=_obj({"ticket_id": _s("退款单号")}, ["ticket_id"]),
        risk="high",
        idempotent=True,
        requires_idempotency_key=True,
        confirmation_required=True,
        tags=["refund", "write", "risky"],
    ),
    ToolSpec(
        name="create_service_ticket",
        description="创建安装 / 维修 / 保养工单",
        parameters=_obj(
            {
                "order_id": _s("订单号"),
                "type": _s(f"工单类型：{'/'.join(SERVICE_TYPES)}"),
                "description": _s("问题描述"),
            },
            ["order_id", "type"],
        ),
        risk="medium",
        idempotent=True,
        requires_idempotency_key=True,
        tags=["service", "write"],
    ),
    ToolSpec(
        name="schedule_service",
        description="为工单预约上门时间（已完成或已取消的工单不可改约）",
        parameters=_obj(
            {"ticket_id": _s("工单号"), "time": _s("预约时间，形如 2026-10-20T10:00:00")},
            ["ticket_id", "time"],
        ),
        risk="medium",
        idempotent=True,
        requires_idempotency_key=True,
        tags=["service", "write"],
    ),
    ToolSpec(
        name="get_service_ticket",
        description="查询工单状态与进度（状态、预约时间、事件流水）",
        parameters=_obj({"ticket_id": _s("工单号")}, ["ticket_id"]),
        risk="low",
        idempotent=True,
        tags=["service", "read"],
    ),
    ToolSpec(
        name="get_logistics",
        description="查询订单物流轨迹",
        parameters=_obj({"order_id": _s("订单号")}, ["order_id"]),
        risk="low",
        idempotent=True,
        tags=["logistics"],
    ),
    ToolSpec(
        name="escalate_to_human",
        description="把当前问题升级给人工坐席处理",
        parameters=_obj({"reason": _s("升级原因"), "context": _s("补充上下文")}, ["reason"]),
        risk="low",
        idempotent=False,
        tags=["risky", "handoff"],
    ),
)


class _BusinessError(Exception):
    """内部信号：携带统一错误码，由 :meth:`MockBackend.invoke` 统一转成 ``ToolResult``。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------- 参数与查询辅助


def _require_str(args: dict[str, Any], name: str) -> str:
    """取必填字符串参数（去首尾空白）。

    :param args: 模型给出的工具参数。
    :param name: 参数名，用于错误信息。
    :returns: 去空白后的非空字符串。
    :raises _BusinessError: 缺失、非字符串或全空白（``INVALID_ARGUMENT``）。
    """
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise _BusinessError(ErrorCode.INVALID_ARGUMENT, f"参数 {name} 必填且必须是非空字符串")
    return value.strip()


def _optional_str(args: dict[str, Any], name: str) -> str | None:
    """取可选字符串参数；空白串按「未提供」处理。

    :param args: 模型给出的工具参数。
    :param name: 参数名。
    :returns: 去空白后的字符串，或 ``None``。
    :raises _BusinessError: 传了值但不是字符串（``INVALID_ARGUMENT``）。
    """
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _BusinessError(ErrorCode.INVALID_ARGUMENT, f"参数 {name} 必须是字符串")
    return value.strip() or None


def _require_positive_int(args: dict[str, Any], name: str) -> int:
    """取必填正整数参数（金额一律以**分**为单位）。

    ``bool`` 是 ``int`` 的子类，必须显式排除，否则 ``True`` 会被当成 1 分。

    :param args: 模型给出的工具参数。
    :param name: 参数名。
    :returns: 正整数。
    :raises _BusinessError: 缺失、为布尔、非整数或 ``<= 0``。
    """
    value = args.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _BusinessError(ErrorCode.INVALID_ARGUMENT, f"参数 {name} 必须是正整数（单位：分）")
    return value


def _optional_int(args: dict[str, Any], name: str) -> int | None:
    """取可选整数参数（同样排除 ``bool``）。

    :param args: 模型给出的工具参数。
    :param name: 参数名。
    :returns: 整数，或 ``None``（未提供）。
    :raises _BusinessError: 传了值但不是整数。
    """
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise _BusinessError(ErrorCode.INVALID_ARGUMENT, f"参数 {name} 必须是整数")
    return value


def _require_str_list(args: dict[str, Any], name: str) -> list[str]:
    """取字符串数组参数；未提供时返回空列表（等价于「没有明细」）。

    :param args: 模型给出的工具参数。
    :param name: 参数名。
    :returns: 去空白、去空项后的字符串列表。
    :raises _BusinessError: 不是数组或含非字符串元素。
    """
    value = args.get(name)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _BusinessError(ErrorCode.INVALID_ARGUMENT, f"参数 {name} 必须是字符串数组")
    return [item.strip() for item in value if item.strip()]


def _product_view(row: dict[str, Any]) -> dict[str, Any]:
    """商品行 → 对外视图：功能解析成数组，并补一个「元」字符串。

    金额的**唯一真相仍是 ``price_cents``**；``price_yuan`` 只为便于模型转述，
    避免它自己做除法时算错一位。
    """
    view = {key: value for key, value in row.items() if key != "features_json"}
    view["features"] = json.loads(row.get("features_json") or "[]")
    view["price_yuan"] = f"{int(row['price_cents']) / 100:.2f}"
    return view


def _fetch_one(conn: sqlite3.Connection, table: str, key: str, value: str) -> dict[str, Any] | None:
    """按主键查一行并转成 dict。

    :param conn: 已开启事务的连接。
    :param table: 表名（仅内部常量，不接受模型输入）。
    :param key: 列名。
    :param value: 主键值。
    :returns: 行字典；查不到返回 ``None``。
    """
    row = conn.execute(f'SELECT * FROM "{table}" WHERE {key} = ?', (value,)).fetchone()
    return dict(row) if row is not None else None


def _require_owner(owner_id: str | None, ctx: ToolContext, *, what: str) -> None:
    """归属校验：**服务端强制**，绝不因模型或用户的任何声明而放行。"""
    if not ctx.user_id:
        raise _BusinessError(ErrorCode.PERMISSION_DENIED, "缺少调用方身份，无法执行该操作")
    if owner_id != ctx.user_id:
        raise _BusinessError(ErrorCode.PERMISSION_DENIED, f"{what}不属于当前用户，拒绝执行")


def _write_audit(
    conn: sqlite3.Connection,
    ctx: ToolContext,
    action: str,
    target: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    decision: str,
    rule_id: str | None = None,
) -> None:
    """写入审计日志：前后状态快照 + actor + decision。"""
    conn.execute(
        "INSERT INTO audit_logs "
        "(trace_id, actor, action, target, before_json, after_json, decision, rule_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ctx.trace_id,
            ctx.actor,
            action,
            target,
            json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
            json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
            decision,
            rule_id,
            utc_now(),
        ),
    )


# --------------------------------------------------------------------- 后端实现


class MockBackend(BusinessBackend):
    """SQLite 模拟后端：把归一化工具映射到本地数据表。"""

    backend_id = "mock"

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def database(self) -> Database:
        """暴露只读入口，供事实提供者 / 审批仓储共用同一个库连接配置。"""
        return self._db

    # ------------------------------------------------------------------ 能力声明

    def list_tools(self) -> list[ToolSpec]:
        """返回归一化工具的声明（返回副本，避免调用方就地修改全局常量）。"""
        return [spec.model_copy(deep=True) for spec in TOOL_SPECS]

    # ------------------------------------------------------------------ 调用入口

    def invoke(self, tool_name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行一次工具调用，统一收敛错误码与异常。"""
        started = time.perf_counter()
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None or tool_name not in {spec.name for spec in TOOL_SPECS}:
            result = make_error(
                ErrorCode.NOT_FOUND, f"后端 {self.backend_id} 未实现工具：{tool_name}"
            )
        else:
            try:
                result = handler(dict(args or {}), ctx)
            except _BusinessError as exc:
                result = make_error(exc.code, exc.message)
            except sqlite3.Error as exc:  # 存储层故障：归为上游错误，交由执行器决定是否重试
                result = make_error(ErrorCode.UPSTREAM_ERROR, f"数据存储异常：{exc}")
            except Exception as exc:  # 兜底：绝不把异常直接抛给编排层
                result = make_error(ErrorCode.UNKNOWN, f"未预期错误：{type(exc).__name__}: {exc}")

        result.latency_ms = int((time.perf_counter() - started) * 1000)
        result.trace_id = ctx.trace_id
        return result

    # ------------------------------------------------------------------ 读操作

    def _tool_get_user(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """查用户基本信息（手机号/地址已脱敏）。

        :param args: 可选 ``user_id``，缺省取会话身份。
        :param ctx: 工具上下文，提供当前用户。
        :returns: 用户行；仅本人可查。
        :raises _BusinessError: 缺身份、用户不存在或越权查询他人。
        """
        user_id = _optional_str(args, "user_id") or ctx.user_id
        if not user_id:
            raise _BusinessError(ErrorCode.INVALID_ARGUMENT, "缺少用户身份（user_id）")
        with self._db.transaction() as conn:
            user = _fetch_one(conn, "users", "user_id", user_id)
        if user is None:
            raise _BusinessError(ErrorCode.NOT_FOUND, f"用户不存在：{user_id}")
        _require_owner(user["user_id"], ctx, what="该用户资料")
        return make_ok(user, trace_id=ctx.trace_id)

    def _tool_verify_user(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """核验用户身份，成功后置 ``ctx.verified``（订单类工具的门禁依据）。

        Mock 后端用脱敏手机号后四位充当校验码；真实后端应走短信/实名核验。

        :param args: ``factor`` 必填为校验因子，``user_id`` 可选（缺省会话身份）。
        :param ctx: 工具上下文。
        :returns: ``{"user_id": ..., "verified": true}``。
        :raises _BusinessError: 缺身份、用户不存在、越权核验他人或因子错误。
        """
        user_id = _optional_str(args, "user_id") or ctx.user_id
        if not user_id:
            raise _BusinessError(ErrorCode.INVALID_ARGUMENT, "缺少用户身份（user_id）")
        factor = _require_str(args, "factor")
        with self._db.transaction() as conn:
            user = _fetch_one(conn, "users", "user_id", user_id)
        if user is None:
            raise _BusinessError(ErrorCode.NOT_FOUND, f"用户不存在：{user_id}")
        _require_owner(user["user_id"], ctx, what="该用户身份")
        # 模拟后端没有明文手机号：用脱敏值的后四位充当校验码（真实后端应走短信 / 实名核验）
        expected = str(user["phone_masked"])[-4:]
        if factor != expected:
            raise _BusinessError(ErrorCode.PERMISSION_DENIED, "身份校验未通过")
        return make_ok({"user_id": user_id, "verified": True}, trace_id=ctx.trace_id)

    def _tool_get_order(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """按订单号查订单详情。

        :param args: ``order_id`` 必填。
        :param ctx: 工具上下文，用于归属校验。
        :returns: 订单行。
        :raises _BusinessError: 缺参、订单不存在或不属于当前用户。
        """
        order_id = _require_str(args, "order_id")
        with self._db.transaction() as conn:
            order = _fetch_one(conn, "orders", "order_id", order_id)
        if order is None:
            raise _BusinessError(ErrorCode.NOT_FOUND, f"订单不存在：{order_id}")
        _require_owner(order["user_id"], ctx, what="该订单")
        return make_ok(order, trace_id=ctx.trace_id)

    def _tool_list_orders(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """列出某用户的订单，可按状态过滤。

        :param args: 可选 ``user_id``（缺省会话身份）与 ``status``。
        :param ctx: 工具上下文，用于归属校验。
        :returns: ``{"orders": [...], "count": n}``，按创建时间升序。
        :raises _BusinessError: 缺身份、状态值非法或越权查他人订单。
        """
        user_id = _optional_str(args, "user_id") or ctx.user_id
        if not user_id:
            raise _BusinessError(ErrorCode.INVALID_ARGUMENT, "缺少用户身份（user_id）")
        status = _optional_str(args, "status")
        if status is not None and status not in ORDER_STATUSES:
            raise _BusinessError(
                ErrorCode.INVALID_ARGUMENT, f"status 取值非法：{status}（可选：{'/'.join(ORDER_STATUSES)}）"
            )
        _require_owner(user_id, ctx, what="该用户的订单")
        sql = "SELECT * FROM orders WHERE user_id = ?"
        params: list[Any] = [user_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at"
        with self._db.transaction() as conn:
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        return make_ok({"orders": rows, "count": len(rows)}, trace_id=ctx.trace_id)

    def _tool_search_products(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """商品检索：品类/关键词/功能/价格上限。

        **过滤与排序都在服务端做**：模型给的只是收窄条件，不能借它绕过「只在售」这类默认约束
        （``include_off_shelf`` 是显式开关，用于「同型号现售价」这类需要看下架机的场景）。
        """
        category = _optional_str(args, "category")
        if category is not None and category not in PRODUCT_CATEGORIES:
            raise _BusinessError(
                ErrorCode.INVALID_ARGUMENT,
                f"category 取值非法：{category}（可选：{'/'.join(PRODUCT_CATEGORIES)}）",
            )
        keyword = _optional_str(args, "keyword")
        feature = _optional_str(args, "feature")
        max_price = _optional_int(args, "max_price_cents")
        if max_price is not None and max_price < 0:
            raise _BusinessError(ErrorCode.INVALID_ARGUMENT, "max_price_cents 不能为负")
        include_off = args.get("include_off_shelf", False)
        if not isinstance(include_off, bool):
            raise _BusinessError(ErrorCode.INVALID_ARGUMENT, "include_off_shelf 必须是布尔值")
        limit_arg = _optional_int(args, "limit")
        # 注意不能用 `or`：limit=0 会被当成「没传」而静默变成默认值
        limit = DEFAULT_PRODUCT_LIMIT if limit_arg is None else limit_arg
        if not 1 <= limit <= MAX_PRODUCT_LIMIT:
            raise _BusinessError(
                ErrorCode.INVALID_ARGUMENT, f"limit 必须在 1..{MAX_PRODUCT_LIMIT} 之间"
            )

        sql = "SELECT * FROM products WHERE 1 = 1"
        params: list[Any] = []
        if category is not None:
            sql += " AND category = ?"
            params.append(category)
        if max_price is not None:
            sql += " AND price_cents <= ?"
            params.append(max_price)
        if not include_off:
            sql += " AND status = 'on_sale'"
        sql += " ORDER BY price_cents, sku"
        with self._db.transaction() as conn:
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]

        def _matches(row: dict[str, Any]) -> bool:
            """判断一行商品是否同时满足关键词与功能条件。

            :param row: ``products`` 表的一行。
            :returns: 两个条件都成立返回 ``True``；功能按**子串**匹配，
                靠数据里写清功能名，而不是在代码里维护同义词表。
            """
            features = [str(item) for item in json.loads(row.get("features_json") or "[]")]
            if keyword:
                haystack = " ".join(
                    str(row.get(key) or "")
                    for key in ("sku", "model", "name", "capacity", "install_notes")
                )
                if keyword not in haystack and not any(keyword in item for item in features):
                    return False
            # 功能按**子串**匹配：「烘干」命中「洗烘一体」靠的是数据里写了「烘干」，
            # 而不是代码里维护同义词表（同义词属业务知识，不该写进后端）
            return not feature or any(feature in item for item in features)

        matched_rows = [row for row in rows if _matches(row)][:limit]
        stock: dict[str, dict[str, Any]] = {}
        promotions: dict[str, list[dict[str, Any]]] = {}
        if matched_rows:
            skus = [row["sku"] for row in matched_rows]
            categories = sorted({str(row["category"]) for row in matched_rows})
            now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            marks = ",".join("?" for _ in skus)
            cat_marks = ",".join("?" for _ in categories)
            with self._db.transaction() as conn:
                stock_rows = conn.execute(
                    f"SELECT sku, qty, restock_eta FROM inventory WHERE sku IN ({marks})", skus
                ).fetchall()
                promo_rows = conn.execute(
                    "SELECT * FROM promotions "
                    f"WHERE start_at <= ? AND end_at >= ? AND (sku IN ({marks}) "
                    f"OR (sku IS NULL AND category IN ({cat_marks})))",
                    [now, now, *skus, *categories],
                ).fetchall()
            stock = {item["sku"]: dict(item) for item in stock_rows}
            for item in promo_rows:
                key = item["sku"] or f"category:{item['category']}"
                promotions.setdefault(key, []).append(dict(item))

        matched = []
        for row in matched_rows:
            view = _product_view(row)
            info = stock.get(row["sku"])
            # 库存与促销随列表一起返回：用户问「有货吗 / 现在多少钱」时模型往往只调一次
            # search，再要求它补一次 get_product 才能答，实际使用中经常漏掉。
            view["stock_qty"] = int(info["qty"]) if info else None
            view["restock_eta"] = info["restock_eta"] if info else None
            view["active_promotions"] = [
                *promotions.get(row["sku"], []),
                *promotions.get(f"category:{row['category']}", []),
            ]
            matched.append(view)

        payload: dict[str, Any] = {
            "products": matched,
            "count": len(matched),
            "filters": {
                "category": category,
                "keyword": keyword,
                "feature": feature,
                "max_price_cents": max_price,
                "include_off_shelf": include_off,
                "limit": limit,
            },
        }
        if not matched:
            payload["note"] = "没有匹配的商品（不得编造型号或参数；可放宽品类/功能/价格条件再查）"
        return make_ok(payload, trace_id=ctx.trace_id)

    def _tool_get_product(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """按 SKU 查完整参数；不存在时显式 NOT_FOUND（不猜最近型号）。

        同时带上**库存**与**当前有效促销**：用户问「有货吗 / 现在多少钱」时，
        参数、库存、优惠是一次查询里最常一起出现的三件事，分三次查既慢又容易口径不一致。
        """
        sku = _require_str(args, "sku")
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with self._db.transaction() as conn:
            row = _fetch_one(conn, "products", "sku", sku)
            if row is None:
                raise _BusinessError(
                    ErrorCode.NOT_FOUND, f"商品不存在：{sku}（请先用 search_products 查型号）"
                )
            stock = _fetch_one(conn, "inventory", "sku", sku)
            promotions = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM promotions "
                    "WHERE (sku = ? OR (sku IS NULL AND category = ?)) "
                    "AND start_at <= ? AND end_at >= ? "
                    "ORDER BY kind, promo_id",
                    (sku, row["category"], now, now),
                ).fetchall()
            ]
        view = _product_view(row)
        view["stock"] = dict(stock) if stock is not None else None
        view["active_promotions"] = promotions
        return make_ok(view, trace_id=ctx.trace_id)

    def _tool_get_user_home(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """查用户默认房屋条件（安装适配用）：楼层、电梯、门宽、水电位、外机位。

        只返回**本人**的房屋信息；没有登记时显式 NOT_FOUND，让坐席去问，
        而不是拿城市平均值糊弄——门宽差 5cm 就是「进不去」。
        """
        # 默认取会话身份：模型不需要（也不应该）知道内部 user_id
        user_id = _optional_str(args, "user_id") or ctx.user_id
        if not user_id:
            raise _BusinessError(ErrorCode.INVALID_ARGUMENT, "缺少用户身份（user_id）")
        _require_owner(user_id, ctx, what="该用户的房屋信息")
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM user_homes WHERE user_id = ? "
                "ORDER BY is_default DESC, home_id LIMIT 1",
                (user_id,),
            ).fetchone()
        if row is None:
            raise _BusinessError(
                ErrorCode.NOT_FOUND, f"用户 {user_id} 尚未登记房屋信息（需先询问楼层/门宽/水电位）"
            )
        return make_ok(dict(row), trace_id=ctx.trace_id)

    def _tool_get_invoice(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """按订单查发票状态；没有申请记录时如实返回 not_requested 语义的空结果。"""
        order_id = _require_str(args, "order_id")
        with self._db.transaction() as conn:
            order = _fetch_one(conn, "orders", "order_id", order_id)
            if order is None:
                raise _BusinessError(ErrorCode.NOT_FOUND, f"订单不存在：{order_id}")
            _require_owner(order["user_id"], ctx, what="该订单的发票")
            row = _fetch_one(conn, "invoices", "order_id", order_id)
        if row is None:
            return make_ok(
                {"order_id": order_id, "status": "not_requested", "invoice": None},
                trace_id=ctx.trace_id,
            )
        return make_ok(row, trace_id=ctx.trace_id)

    def _tool_submit_invoice_request(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """申请开票：``not_requested → pending``；已开票不允许重复申请。

        同一订单已有 ``pending`` 时直接返回原记录（``reused=True``），
        避免用户重复点击产生两张待开票单。
        """
        order_id = _require_str(args, "order_id")
        kind = (_optional_str(args, "kind") or "normal").strip()
        if kind not in {"normal", "special"}:
            raise _BusinessError(
                ErrorCode.INVALID_ARGUMENT, "发票类型只能是 normal（普票）或 special（专票）"
            )

        with self._db.transaction() as conn:
            order = _fetch_one(conn, "orders", "order_id", order_id)
            if order is None:
                raise _BusinessError(ErrorCode.NOT_FOUND, f"订单不存在：{order_id}")
            _require_owner(order["user_id"], ctx, what="该订单")
            if order["status"] == "cancelled":
                raise _BusinessError(ErrorCode.CONFLICT_STATE, "已取消订单不能申请开票")

            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM invoices WHERE order_id = ? ORDER BY updated_at, invoice_id",
                    (order_id,),
                ).fetchall()
            ]
            pending = next((row for row in rows if row["status"] == "pending"), None)
            if pending is not None:
                payload = dict(pending)
                payload["reused"] = True
                return make_ok(payload, trace_id=ctx.trace_id)
            if any(row["status"] == "issued" for row in rows):
                raise _BusinessError(
                    ErrorCode.CONFLICT_STATE, "该订单发票已开具，不能重复申请"
                )

            user = _fetch_one(conn, "users", "user_id", order["user_id"]) or {}
            title = _optional_str(args, "title") or f"{user.get('name') or '个人'}（个人）"
            now = utc_now()
            # 种子里已存在 not_requested 占位行：就地更新，避免同一订单出现两条记录
            placeholder = next((row for row in rows if row["status"] == "not_requested"), None)
            if placeholder is not None:
                invoice_id = str(placeholder["invoice_id"])
                conn.execute(
                    "UPDATE invoices SET kind = ?, title = ?, status = 'pending', updated_at = ? "
                    "WHERE invoice_id = ?",
                    (kind, title, now, invoice_id),
                )
                before = placeholder
            else:
                invoice_id = f"INV-{order_id}" if not rows else f"INV-{order_id}-{len(rows) + 1}"
                conn.execute(
                    "INSERT INTO invoices "
                    "(invoice_id, order_id, user_id, kind, title, amount_cents, status, "
                    " issued_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?)",
                    (
                        invoice_id,
                        order_id,
                        order["user_id"],
                        kind,
                        title,
                        int(order["amount_cents"]),
                        now,
                    ),
                )
                before = None
            after = _fetch_one(conn, "invoices", "invoice_id", invoice_id)
            _write_audit(
                conn, ctx, "submit_invoice_request", invoice_id, before, after, "allowed", None
            )

        return make_ok(after, trace_id=ctx.trace_id)

    def _tool_list_promotions(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """查当前进行中的活动：SKU 专属 + 品类级 + 全店通用。"""
        now = utc_now()
        sku = _optional_str(args, "sku")
        category = _optional_str(args, "category")
        with self._db.transaction() as conn:
            if sku:
                product = _fetch_one(conn, "products", "sku", sku)
                if product is None:
                    raise _BusinessError(ErrorCode.NOT_FOUND, f"商品不存在：{sku}")
                category = category or str(product["category"])

            clauses = ["start_at <= ?", "end_at >= ?"]
            params: list[Any] = [now, now]
            if sku and category:
                clauses.append(
                    "(sku = ? OR (sku IS NULL AND (category = ? OR category IS NULL)))"
                )
                params.extend([sku, category])
            elif sku:
                clauses.append("(sku = ? OR sku IS NULL)")
                params.append(sku)
            elif category:
                clauses.append("(category = ? OR (sku IS NULL AND category IS NULL))")
                params.append(category)

            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM promotions WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY end_at, promo_id",
                    params,
                ).fetchall()
            ]

        return make_ok(
            {
                "as_of": now,
                "sku": sku,
                "category": category,
                "count": len(rows),
                "promotions": rows,
                "note": "" if rows else "当前没有进行中的优惠活动",
            },
            trace_id=ctx.trace_id,
        )

    def _tool_get_installation_quote(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """按房屋条件估算安装可行性与收费；不臆造「能不能装」。

        判定只看登记事实（楼层 / 电梯 / 门宽 / 水电位 / 外机位 / 电源），
        缺信息时给 ``requires_survey`` 而不是猜一个结论。
        """
        sku = _require_str(args, "sku")
        home_id = _optional_str(args, "home_id")
        with self._db.transaction() as conn:
            product = _fetch_one(conn, "products", "sku", sku)
            if product is None:
                raise _BusinessError(ErrorCode.NOT_FOUND, f"商品不存在：{sku}")
            if home_id:
                home = _fetch_one(conn, "user_homes", "home_id", home_id)
                if home is None:
                    raise _BusinessError(ErrorCode.NOT_FOUND, f"房屋信息不存在：{home_id}")
                _require_owner(home["user_id"], ctx, what="该房屋信息")
            else:
                if not ctx.user_id:
                    raise _BusinessError(ErrorCode.INVALID_ARGUMENT, "缺少调用方身份")
                row = conn.execute(
                    "SELECT * FROM user_homes WHERE user_id = ? "
                    "ORDER BY is_default DESC, home_id LIMIT 1",
                    (ctx.user_id,),
                ).fetchone()
                if row is None:
                    raise _BusinessError(
                        ErrorCode.NOT_FOUND,
                        "尚未登记房屋信息（需先询问楼层/电梯/门宽/水电位/外机位）",
                    )
                home = dict(row)

        category = str(product["category"])
        fees: list[dict[str, Any]] = []
        notes: list[str] = []
        installable = True
        requires_survey = False

        base = INSTALL_BASE_FEES_CENTS.get(category, 0)
        if base:
            fees.append({"item": "基础安装费", "amount_cents": base})

        floor = int(home["floor"])
        if not int(home["has_elevator"]) and floor > 3:
            fees.append(
                {
                    "item": f"无电梯上楼费（{floor - 3} 层 × {FLOOR_FEE_PER_LEVEL_CENTS // 100} 元）",
                    "amount_cents": (floor - 3) * FLOOR_FEE_PER_LEVEL_CENTS,
                }
            )

        slot = str(home.get("outdoor_slot") or "")
        if category == "aircon":
            if slot in {"", "无"}:
                installable = False
                notes.append("无外机位，需先确认可安装位置")
            elif "吊" in slot:
                fees.append({"item": "外机吊装费", "amount_cents": LIFT_FEE_CENTS})

        if category == "washer":
            if not int(home["water_inlet"]) or not int(home["drain"]):
                installable = False
                notes.append("缺少进水或排水条件，该品类无法安装")

        if int(home["door_width_cm"]) < 60:
            requires_survey = True
            notes.append("门宽小于 60cm，可能无法整机通过，需现场勘测（拆门/吊装另计）")

        if category == "aircon" and "16" not in str(home.get("power_spec") or ""):
            notes.append("该品类需 16A 独立回路，当前登记电源条件可能不满足，需现场确认")

        total = sum(int(fee["amount_cents"]) for fee in fees)
        return make_ok(
            {
                "sku": sku,
                "model": product["model"],
                "category": category,
                "home": home,
                "installable": installable,
                "requires_survey": requires_survey,
                "fees": fees,
                "total_cents": total,
                "simulated": True,
                "note": "报价为模拟值，实际以下单页与师傅现场确认为准" if installable else "当前条件不满足，需先解决上述问题",
            },
            trace_id=ctx.trace_id,
        )

    def _tool_get_logistics(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """查订单的物流信息。

        :param args: ``order_id`` 必填。
        :param ctx: 工具上下文，用于归属校验。
        :returns: 物流记录（承运商 + 运单号 + 轨迹）。
        :raises _BusinessError: 订单不存在/不属于本人、或尚无物流记录
            （未发货时如实报 ``NOT_FOUND``，绝不编造轨迹）。
        """
        order_id = _require_str(args, "order_id")
        with self._db.transaction() as conn:
            order = _fetch_one(conn, "orders", "order_id", order_id)
            if order is None:
                raise _BusinessError(ErrorCode.NOT_FOUND, f"订单不存在：{order_id}")
            _require_owner(order["user_id"], ctx, what="该订单")
            record = _fetch_one(conn, "logistics", "order_id", order_id)
        if record is None:
            # 如实告知：没有物流记录（例如订单尚未发货），绝不编造轨迹
            raise _BusinessError(ErrorCode.NOT_FOUND, f"订单 {order_id} 暂无物流记录（可能尚未发货）")
        return make_ok(record, trace_id=ctx.trace_id)

    def _tool_get_refund_ticket(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """按退款单号**或订单号**查询；都查不到时明确区分「单据不存在」与「该订单没有退款」。"""
        ticket_id = _optional_str(args, "ticket_id")
        order_id = _optional_str(args, "order_id")
        if not ticket_id and not order_id:
            raise _BusinessError(ErrorCode.INVALID_ARGUMENT, "请提供退款单号或订单号")
        with self._db.transaction() as conn:
            if ticket_id:
                refund = _fetch_one(conn, "refund_requests", "refund_id", ticket_id)
            else:
                # 同一订单可能有多笔（拒后重提），取最新一笔
                row = conn.execute(
                    "SELECT * FROM refund_requests WHERE order_id = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (order_id,),
                ).fetchone()
                refund = dict(row) if row is not None else None
        if refund is None:
            target = ticket_id or order_id
            raise _BusinessError(
                ErrorCode.NOT_FOUND, f"没有查到退款单（{target}）；该订单可能尚未申请退款"
            )
        _require_owner(refund["user_id"], ctx, what="该退款单")
        return make_ok(refund, trace_id=ctx.trace_id)

    # ------------------------------------------------------------------ 写操作

    def _tool_cancel_order(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """取消订单（写操作，带审计）。

        :param args: ``order_id`` 与 ``reason`` 均必填。
        :param ctx: 工具上下文，用于归属校验与审计 actor。
        :returns: ``{"order_id": ..., "status": "cancelled", "reason": ...}``。
        :raises _BusinessError: 缺参、订单不存在、越权，或状态不在可取消集合内
            （``CONFLICT_STATE``，不靠提示词约束）。
        """
        order_id = _require_str(args, "order_id")
        reason = _require_str(args, "reason")
        with self._db.transaction() as conn:
            order = _fetch_one(conn, "orders", "order_id", order_id)
            if order is None:
                raise _BusinessError(ErrorCode.NOT_FOUND, f"订单不存在：{order_id}")
            _require_owner(order["user_id"], ctx, what="该订单")
            if order["status"] not in CANCELLABLE_STATUSES:
                raise _BusinessError(
                    ErrorCode.CONFLICT_STATE,
                    f"订单当前状态为 {order['status']}，不可取消",
                )
            before = dict(order)
            conn.execute(
                "UPDATE orders SET status = 'cancelled' WHERE order_id = ? AND status = ?",
                (order_id, order["status"]),
            )
            after = _fetch_one(conn, "orders", "order_id", order_id)
            _write_audit(conn, ctx, "cancel_order", order_id, before, after, "allowed", None)
        return make_ok(
            {"order_id": order_id, "status": after["status"] if after else None, "reason": reason},
            trace_id=ctx.trace_id,
        )

    def _tool_create_refund_ticket(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """创建退款单（写操作，状态初始为 ``pending_confirmation``）。

        服务端校验三件事：订单归属、订单状态可退、退款金额不超过订单金额。
        另外做一层**数据库级幂等兜底**：同一幂等键重复请求直接回放历史结果，
        即便有人绕过了 ``ToolExecutor`` 也不会落下两笔退款。

        :param args: ``order_id`` / ``amount_cents``（分）/ ``reason`` 必填，
            ``items`` 可选（缺省用订单商品名）。
        :param ctx: 工具上下文，提供身份与幂等键。
        :returns: 新建的退款单行；幂等重放时带 ``IDEMPOTENT_REPLAY``。
        :raises _BusinessError: 缺参、金额非正或超订单金额、订单不可退、越权。
        """
        order_id = _require_str(args, "order_id")
        amount_cents = _require_positive_int(args, "amount_cents")
        reason = _require_str(args, "reason")
        items = _require_str_list(args, "items")

        with self._db.transaction() as conn:
            order = _fetch_one(conn, "orders", "order_id", order_id)
            if order is None:
                raise _BusinessError(ErrorCode.NOT_FOUND, f"订单不存在：{order_id}")
            _require_owner(order["user_id"], ctx, what="该订单")
            if order["status"] not in REFUNDABLE_STATUSES:
                # 数据完整性：未签收的订单不存在“退货”语义（如何引导用户由 PolicyEngine 决定）
                raise _BusinessError(
                    ErrorCode.CONFLICT_STATE,
                    f"订单当前状态为 {order['status']}，不满足创建退款单的条件",
                )
            if amount_cents > int(order["amount_cents"]):
                raise _BusinessError(ErrorCode.INVALID_ARGUMENT, "退款金额不得超过订单金额")

            # 兜底防护：即便绕过了 ToolExecutor，同一幂等键也不会落下两笔退款单。
            # （参数是否一致由执行器用 request_hash 判定，此处只防重复落库。）
            if ctx.idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM refund_requests WHERE idempotency_key = ?",
                    (ctx.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return make_ok(
                        dict(existing),
                        message="同一幂等键的请求已处理，返回历史结果",
                        error_code=ErrorCode.IDEMPOTENT_REPLAY,
                        trace_id=ctx.trace_id,
                    )

            refund_id = f"R{uuid4().hex[:10].upper()}"
            now = utc_now()
            conn.execute(
                "INSERT INTO refund_requests "
                "(refund_id, order_id, user_id, items_json, amount_cents, reason, status, "
                " idempotency_key, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending_confirmation', ?, ?, ?)",
                (
                    refund_id,
                    order_id,
                    order["user_id"],
                    json.dumps(items or [order["product"]], ensure_ascii=False),
                    amount_cents,
                    reason,
                    ctx.idempotency_key,
                    now,
                    now,
                ),
            )
            after = _fetch_one(conn, "refund_requests", "refund_id", refund_id)
            _write_audit(conn, ctx, "create_refund_ticket", refund_id, None, after, "allowed", None)
        return make_ok(after, trace_id=ctx.trace_id)

    def _tool_approve_refund(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """审批退款单：``pending_confirmation`` → ``approved``（只允许一次）。

        :param args: ``ticket_id`` 必填，即退款单号。
        :param ctx: 工具上下文，用于审计 actor。
        :returns: 迁移后的退款单行。
        :raises _BusinessError: 单据不存在，或状态已非 ``pending_confirmation``
            （``CONFLICT_STATE``——重复审批会重复打款）。
        """
        ticket_id = _require_str(args, "ticket_id")
        with self._db.transaction() as conn:
            refund = _fetch_one(conn, "refund_requests", "refund_id", ticket_id)
            if refund is None:
                raise _BusinessError(ErrorCode.NOT_FOUND, f"退款单不存在：{ticket_id}")
            if refund["status"] != "pending_confirmation":
                raise _BusinessError(
                    ErrorCode.CONFLICT_STATE,
                    f"退款单当前状态为 {refund['status']}，不可审批",
                )
            before = dict(refund)
            conn.execute(
                "UPDATE refund_requests SET status = 'approved', updated_at = ? "
                "WHERE refund_id = ?",
                (utc_now(), ticket_id),
            )
            after = _fetch_one(conn, "refund_requests", "refund_id", ticket_id)
            _write_audit(conn, ctx, "approve_refund", ticket_id, before, after, "allowed", None)
        return make_ok(after, trace_id=ctx.trace_id)

    def _tool_create_service_ticket(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """创建售后工单（初始状态 ``created``，尚未预约时间）。

        :param args: ``order_id`` 与 ``type``（repair/install/maintenance 等）必填，
            ``description`` 可选。
        :param ctx: 工具上下文，用于归属校验与审计。
        :returns: 新建的工单行。
        :raises _BusinessError: 缺参、类型非法、订单不存在或不属于当前用户。
        """
        order_id = _require_str(args, "order_id")
        service_type = _require_str(args, "type")
        description = _optional_str(args, "description") or ""
        if service_type not in SERVICE_TYPES:
            raise _BusinessError(
                ErrorCode.INVALID_ARGUMENT,
                f"type 取值非法：{service_type}（可选：{'/'.join(SERVICE_TYPES)}）",
            )
        with self._db.transaction() as conn:
            order = _fetch_one(conn, "orders", "order_id", order_id)
            if order is None:
                raise _BusinessError(ErrorCode.NOT_FOUND, f"订单不存在：{order_id}")
            _require_owner(order["user_id"], ctx, what="该订单")
            ticket_id = f"T{uuid4().hex[:10].upper()}"
            conn.execute(
                "INSERT INTO service_tickets "
                "(ticket_id, order_id, user_id, type, description, status, "
                " scheduled_time, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'created', NULL, ?)",
                (ticket_id, order_id, order["user_id"], service_type, description, utc_now()),
            )
            after = _fetch_one(conn, "service_tickets", "ticket_id", ticket_id)
            _write_audit(
                conn, ctx, "create_service_ticket", ticket_id, None, after, "allowed", None
            )
        return make_ok(after, trace_id=ctx.trace_id)

    def _tool_schedule_service(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """给工单预约/改约上门时间，成功后状态变为 ``scheduled``。

        :param args: ``ticket_id`` 与 ``time``（ISO8601 字符串）必填。
        :param ctx: 工具上下文，用于归属校验与审计。
        :returns: 更新后的工单行。
        :raises _BusinessError: 工单不存在、越权，或状态不可改约
            （已完成/已取消时 ``CONFLICT_STATE``）。
        """
        ticket_id = _require_str(args, "ticket_id")
        scheduled_time = _require_str(args, "time")
        with self._db.transaction() as conn:
            ticket = _fetch_one(conn, "service_tickets", "ticket_id", ticket_id)
            if ticket is None:
                raise _BusinessError(ErrorCode.NOT_FOUND, f"工单不存在：{ticket_id}")
            _require_owner(ticket["user_id"], ctx, what="该工单")
            if ticket["status"] not in ("created", "scheduled"):
                raise _BusinessError(
                    ErrorCode.CONFLICT_STATE, f"工单当前状态为 {ticket['status']}，不可改约"
                )
            before = dict(ticket)
            conn.execute(
                "UPDATE service_tickets SET scheduled_time = ?, status = 'scheduled' "
                "WHERE ticket_id = ?",
                (scheduled_time, ticket_id),
            )
            after = _fetch_one(conn, "service_tickets", "ticket_id", ticket_id)
            _write_audit(conn, ctx, "schedule_service", ticket_id, before, after, "allowed", None)
        return make_ok(after, trace_id=ctx.trace_id)

    def _tool_get_service_ticket(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """工单进度：状态 + 预约时间 + 事件流水。

        ``status`` 只说明「进行到哪一格」，用户真正问的是「卡在哪一步」——所以必须带
        ``events``（备件等待、师傅已联系、二次上门都在事件里，不在状态枚举里）。
        """
        ticket_id = _require_str(args, "ticket_id")
        with self._db.transaction() as conn:
            ticket = _fetch_one(conn, "service_tickets", "ticket_id", ticket_id)
            if ticket is None:
                raise _BusinessError(ErrorCode.NOT_FOUND, f"工单不存在：{ticket_id}")
            _require_owner(ticket["user_id"], ctx, what="该工单")
            events = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM ticket_events WHERE ticket_id = ? "
                    "ORDER BY happened_at, event_id",
                    (ticket_id,),
                ).fetchall()
            ]
        payload = dict(ticket)
        payload["events"] = events
        return make_ok(payload, trace_id=ctx.trace_id)

    def _tool_escalate_to_human(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """转人工：落一条 ``decision=escalate`` 的审计并返回排队号。

        :param args: ``reason`` 必填（要能说清为什么转），``context`` 可选补充。
        :param ctx: 工具上下文，``trace_id`` 前 8 位用作排队号。
        :returns: ``{"handoff_id": ..., "status": "queued", "reason": ...}``。
        :raises _BusinessError: 缺少 ``reason``。
        """
        reason = _require_str(args, "reason")
        context = _optional_str(args, "context") or ""
        handoff_id = f"HO-{ctx.trace_id[:8].upper()}"
        with self._db.transaction() as conn:
            _write_audit(
                conn,
                ctx,
                "escalate_to_human",
                handoff_id,
                None,
                {"reason": reason, "context": context},
                "escalate",
                None,
            )
        return make_ok(
            {"handoff_id": handoff_id, "status": "queued", "reason": reason},
            trace_id=ctx.trace_id,
        )
