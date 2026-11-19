"""索引构建的文件锁（S4）。

两个 ``rag index`` 同时跑会互相覆盖 ``vector_store.json`` / ``chunks.json``，
最终得到「清单与向量不一致」的索引。这里用 ``flock`` 保证同一索引目录只有一个
构建者；第二个进程立即得到明确错误，而不是排队等待或悄悄覆盖。
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from zkagent.core.rag.errors import RagIndexError

__all__ = ["IndexBusyError", "index_lock"]


class IndexBusyError(RagIndexError):
    """已有进程在构建同一索引。"""


@contextmanager
def index_lock(index_dir: Path | str, *, blocking: bool = False) -> Iterator[None]:
    """对索引目录加排他锁。

    :param blocking: ``False`` 时立即失败（CLI 默认）；``True`` 时等待（脚本可用）。
    """
    directory = Path(index_dir)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".build.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            raise IndexBusyError(
                f"索引正在被另一个进程构建：{directory}（稍后重试，或先用 `rag status` 查看）"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
