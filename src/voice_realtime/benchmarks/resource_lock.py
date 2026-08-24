"""主机级高负载实验排他锁。"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

_POLL_INTERVAL_SECS = 0.05


@dataclass(frozen=True, slots=True)
class ResourceLockMetadata:
    """写入锁文件的最小所有者信息。"""

    pid: int
    started_at: str
    command: str
    run_id: str | None


class ResourceBusyError(OSError):
    """另一进程或实验已经持有主机资源。"""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        super().__init__(f"RESOURCE_BUSY: ASR experiment lock is held: {lock_path}")


def default_resource_lock_path() -> Path:
    """返回项目外的用户级默认实验锁路径。"""
    return Path.home() / ".cache" / "voice-realtime" / "locks" / "asr-experiment.lock"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@contextmanager
def exclusive_resource_lock(
    path: Path | None = None,
    *,
    timeout_secs: float = 0.0,
    run_id: str | None = None,
) -> Iterator[ResourceLockMetadata]:
    """在整个高负载实验期间持有一个 POSIX 排他锁。"""
    if timeout_secs < 0:
        raise ValueError("timeout_secs must be non-negative")
    if path is None:
        lock_path = default_resource_lock_path()
        uses_default_path = True
    else:
        lock_path = path
        uses_default_path = False
    parent_existed = lock_path.parent.exists()
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if uses_default_path or not parent_existed:
        lock_path.parent.chmod(0o700)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    os.fchmod(descriptor, 0o600)
    deadline = time.monotonic() + timeout_secs
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ResourceBusyError(lock_path) from exc
                time.sleep(min(_POLL_INTERVAL_SECS, remaining))

        metadata = ResourceLockMetadata(
            pid=os.getpid(),
            started_at=_utc_now(),
            command=Path(sys.argv[0]).name,
            run_id=run_id,
        )
        payload = (json.dumps(asdict(metadata), ensure_ascii=False, sort_keys=True) + "\n").encode()
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, payload)
        os.fsync(descriptor)
        yield metadata
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
