"""最简本地 Tracer：生成 trace_id、记录事件、按需落盘为 JSON。

设计约束（DESIGN §11 R10 / §9 安全条）：
- **默认不依赖任何外部服务**：LangSmith 导出是可选且默认关闭的（P6 才接入）；
- 写入前对敏感字段做**脱敏**与长度截断，避免手机号 / 地址 / 令牌进入 trace。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["Tracer", "SENSITIVE_KEY_PATTERN"]

#: 命中这些键名的字段值一律脱敏（大小写不敏感）。
SENSITIVE_KEY_PATTERN = re.compile(
    r"(phone|mobile|address|id_?card|email|token|secret|api_?key|password)",
    re.IGNORECASE,
)

_REDACTED = "***"


def _now_iso() -> str:
    """当前 UTC 时间（毫秒精度，ISO8601）。

    :returns: 形如 ``2026-11-01T10:00:00.000+00:00`` 的字符串，便于跨机器对时比较。
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Tracer:
    """进程内事件收集器；调用 :meth:`flush` 时把某个 trace 写成 JSON 文件。

    事件保存在内存中（按 ``trace_id`` 分组），因此不会影响工具调用的实时性；
    评估（P6）会读取落盘文件做 trace 断言。
    """

    def __init__(self, trace_dir: Path | str | None = None, *, max_value_len: int = 300) -> None:
        """初始化。

        :param trace_dir: 落盘目录；为 ``None`` 时只保留在内存（测试常用）。
        :param max_value_len: 单个字符串值的最大保留长度，超出即截断。
        """
        self.trace_dir = Path(trace_dir) if trace_dir is not None else None
        self.max_value_len = max_value_len
        self._events: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def new_trace_id() -> str:
        """生成新的 trace_id（短 uuid，足够单机场景唯一）。"""
        return uuid.uuid4().hex[:16]

    def record(self, trace_id: str, event: str, **fields: Any) -> None:
        """记录一条事件。字段值会被脱敏与截断。"""
        self._events.setdefault(trace_id, []).append(
            {
                "at": _now_iso(),
                "event": event,
                **{key: self._sanitize(key, value) for key, value in fields.items()},
            }
        )

    def events(self, trace_id: str) -> list[dict[str, Any]]:
        """读取某个 trace 的全部事件（返回副本，避免外部就地修改）。"""
        return list(self._events.get(trace_id, []))

    def flush(self, trace_id: str) -> Path | None:
        """把某个 trace 写入 ``<trace_dir>/<trace_id>.json``；未配置目录时返回 ``None``。"""
        if self.trace_dir is None:
            return None
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        path = self.trace_dir / f"{trace_id}.json"
        payload = {
            "trace_id": trace_id,
            "events": self._events.get(trace_id, []),
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    # ------------------------------------------------------------------ 内部

    def _sanitize(self, key: str, value: Any) -> Any:
        """按键名脱敏，并对字符串做长度截断；容器类型递归处理。"""
        if SENSITIVE_KEY_PATTERN.search(key):
            return _REDACTED
        if isinstance(value, str):
            if len(value) > self.max_value_len:
                return value[: self.max_value_len] + f"...<截断 {len(value)} 字符>"
            return value
        if isinstance(value, dict):
            return {str(k): self._sanitize(str(k), v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._sanitize(key, item) for item in value]
        return value
