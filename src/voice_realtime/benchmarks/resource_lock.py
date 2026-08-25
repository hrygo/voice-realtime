"""主机级高负载实验排他锁。"""

from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from voice_realtime.benchmarks.asr.stage_executors import CloseObservation

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


class ResourceQuarantinedError(RuntimeError):
    """前次运行清理不完整，必须先完成显式资源审计。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"RESOURCE_QUARANTINED: cleanup audit required: {path}")


@dataclass(frozen=True, slots=True)
class ResourceReleaseAudit:
    """人工/运维确认资源已释放的最小审计结果。"""

    released: bool
    remaining_process_ids: tuple[int, ...]
    remaining_ports: tuple[int, ...]
    remaining_tasks: int
    remaining_connections: int


def resource_quarantine_path(lock_path: Path | None = None) -> Path:
    """返回与实验锁相邻的项目外 quarantine marker 路径。"""

    resolved_lock = lock_path or default_resource_lock_path()
    return resolved_lock.with_name(f"{resolved_lock.name}.quarantine.json")


def _require_quarantine_regular(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(path) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ResourceQuarantinedError(path)
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ResourceQuarantinedError(path)
    return info


def require_no_resource_quarantine(path: Path) -> None:
    """在取得 flock 后检查是否存在未处置 marker。"""

    marker = Path(path)
    try:
        _require_quarantine_regular(marker)
    except FileNotFoundError:
        return
    raise ResourceQuarantinedError(marker)


def write_resource_quarantine(
    path: Path,
    *,
    run_id: str,
    executor_id: str,
    observation: CloseObservation,
) -> None:
    """原子写入 0600 quarantine marker，不记录命令或环境。"""

    if observation.released:
        raise ValueError("released observation does not require resource quarantine")
    marker = Path(path)
    parent = marker.parent
    try:
        parent_info = parent.lstat()
    except FileNotFoundError:
        parent.mkdir(parents=True, mode=0o700)
        parent_info = parent.lstat()
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise ResourceQuarantinedError(marker)
    try:
        marker_info = marker.lstat()
    except FileNotFoundError:
        marker_info = None
    if marker_info is not None:
        raise ResourceQuarantinedError(marker)

    payload = {
        "schema_version": "1.0",
        "created_at": _utc_now(),
        "run_id": run_id,
        "executor_id": executor_id,
        "released": False,
        "remaining_process_ids": list(observation.remaining_process_ids),
        "remaining_ports": list(observation.remaining_ports),
        "remaining_tasks": observation.remaining_tasks,
        "remaining_connections": observation.remaining_connections,
    }
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
    fd, temporary_name = tempfile.mkstemp(prefix=f".{marker.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("unable to write resource quarantine")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        # A hard-link publish is atomic and refuses to overwrite an existing
        # marker, unlike os.replace.  The temporary inode remains private.
        os.link(temporary, marker)
        temporary.unlink()
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except FileExistsError as exc:
        raise ResourceQuarantinedError(marker) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        with suppress(FileNotFoundError):
            temporary.unlink()


def clear_resource_quarantine(path: Path, audit: ResourceReleaseAudit) -> None:
    """只凭完整零资源审计删除 marker。"""

    if not (
        audit.released
        and not audit.remaining_process_ids
        and not audit.remaining_ports
        and audit.remaining_tasks == 0
        and audit.remaining_connections == 0
    ):
        raise ValueError("resource quarantine requires a clean release audit")
    marker = Path(path)
    _require_quarantine_regular(marker)
    marker.unlink()
    parent_fd = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


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
    if not math.isfinite(timeout_secs) or timeout_secs < 0:
        raise ValueError("timeout_secs must be finite and non-negative")
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
