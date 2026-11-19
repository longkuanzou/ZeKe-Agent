"""结构化日志：单行 JSON，便于后续 trace / 评估按字段检索。

约定：**禁止**把密钥、手机号、地址写入日志（脱敏在数据写入侧已完成，
此处再做一层兜底：值命中敏感键名时替换为 ``***``）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

__all__ = ["setup_logging", "get_logger", "SENSITIVE_KEY_PATTERN"]

SENSITIVE_KEY_PATTERN = re.compile(
    r"(phone|mobile|address|id_?card|email|token|secret|api_?key|password)",
    re.IGNORECASE,
)

#: 由 ``JsonLineFormatter`` 自身输出、不需要从 ``record.__dict__`` 重复采集的字段。
_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "thread", "threadName", "taskName",
    }
)


class JsonLineFormatter(logging.Formatter):
    """把日志格式化为单行 JSON；``extra`` 中的自定义字段会被合并进来。"""

    def format(self, record: logging.LogRecord) -> str:
        """把一条日志渲染成单行 JSON。

        标准字段之外的 ``extra`` 会平铺进来；键名命中敏感词
        （phone / token / api_key 等）时值替换为 ``***``。

        :param record: 标准库日志记录。
        :returns: 单行 JSON 字符串（``ensure_ascii=False``，中文不转义）。
        """
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = "***" if SENSITIVE_KEY_PATTERN.search(key) else value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str | int = "INFO") -> None:
    """初始化根 logger（幂等：重复调用不会叠加 handler）。

    :param level: 日志级别，接受 ``"INFO"`` 这类名称或 ``logging`` 常量。
    """
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLineFormatter())
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """获取 logger（统一入口，便于将来替换实现）。"""
    return logging.getLogger(name)
