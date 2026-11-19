"""后台索引构建的状态文件（M14）。

状态写在 ``data/indexes/<industry_id>/build_status.json``：
``queued`` → ``running`` → ``succeeded`` / ``failed``。写入用临时文件 + 原子替换，
避免 ``rag status`` 读到半截 JSON。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["STATUS_FILENAME", "read_status", "write_status"]

STATUS_FILENAME = "build_status.json"


def _now() -> str:
    """当前时间（``YYYY-MM-DDTHH:MM:SS``），用于状态的 ``updated_at``。

    :returns: 秒级时间字符串，与库内其它时间列格式一致。
    """
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _path(index_dir: Path | str) -> Path:
    """状态文件路径。

    :param index_dir: 索引目录（也是状态文件的落点）。
    :returns: ``<index_dir>/build_status.json``。
    """
    return Path(index_dir) / STATUS_FILENAME


def write_status(index_dir: Path | str, **fields: Any) -> dict[str, Any]:
    """合并写入状态；返回写入后的完整内容。"""
    target = _path(index_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    current = read_status(index_dir) or {}
    current.update(fields)
    current["updated_at"] = _now()
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, target)
    return current


def read_status(index_dir: Path | str) -> dict[str, Any] | None:
    """读取状态；不存在或损坏时返回 ``None``（由 CLI 给出可读提示）。"""
    target = _path(index_dir)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
