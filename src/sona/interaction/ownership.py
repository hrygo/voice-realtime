"""交互管道跨进程单一所有者锁。"""

from __future__ import annotations

import fcntl
from pathlib import Path
from typing import TextIO

DEFAULT_OWNERSHIP_PATH = (
    Path.home() / "Library" / "Caches" / "sona" / "interaction.lock"
)


class InteractionOwnershipError(RuntimeError):
    """已有另一个进程拥有麦克风交互管道。"""


class InteractionOwnership:
    """通过非阻塞文件锁保证 `sona-ui` 与 `sona-interact` 互斥。"""

    def __init__(self, lock_path: Path = DEFAULT_OWNERSHIP_PATH) -> None:
        self._lock_path = lock_path
        self._file: TextIO | None = None

    @property
    def acquired(self) -> bool:
        return self._file is not None

    def acquire(self) -> None:
        if self._file is not None:
            return
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self._lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise InteractionOwnershipError(
                "交互管道已由另一个 sona-ui 或 sona-interact 进程占用"
            ) from exc
        self._file = lock_file

    def close(self) -> None:
        lock_file = self._file
        if lock_file is None:
            return
        self._file = None
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

    def __enter__(self) -> InteractionOwnership:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
