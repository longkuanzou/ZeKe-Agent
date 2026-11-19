"""S4 / M14：索引构建锁与后台状态文件。"""

from __future__ import annotations

from pathlib import Path

import pytest

from zkagent.core.rag.build_status import read_status, write_status
from zkagent.core.rag.index_lock import IndexBusyError, index_lock


def test_second_builder_fails_fast(tmp_path: Path) -> None:
    """验证：第二个构建进程**立即失败**（``IndexBusyError``），不排队干等。

    对应 S4：并发建索引会写出互相覆盖的半截索引。
    """
    with index_lock(tmp_path):
        with pytest.raises(IndexBusyError, match="正在被另一个进程构建"):
            with index_lock(tmp_path):
                pass


def test_lock_is_released_after_exit(tmp_path: Path) -> None:
    """验证：锁在上下文退出后即释放——可以串行再建一次（异常退出也必须释放）。"""
    with index_lock(tmp_path):
        pass
    with index_lock(tmp_path):
        pass


def test_build_status_round_trip(tmp_path: Path) -> None:
    """验证：状态文件可写入并读回，后续写入合并而非清空（industry 与 pid 同时存在）。

    对应 M14：面板要展示后台构建进度。
    """
    write_status(tmp_path, status="queued", industry="appliance")
    write_status(tmp_path, status="running", pid=123)

    data = read_status(tmp_path)

    assert data is not None
    assert data["status"] == "running"
    assert data["industry"] == "appliance"
    assert data["pid"] == 123
    assert data["updated_at"]


def test_missing_status_returns_none(tmp_path: Path) -> None:
    """验证：没有状态文件时返回 ``None``——「从未构建」与「构建失败」要能区分开。"""
    assert read_status(tmp_path) is None
