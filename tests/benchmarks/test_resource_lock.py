"""主机级实验资源排他锁测试。"""

from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path
from typing import Any

import pytest

from voice_realtime.benchmarks.resource_lock import (
    ResourceBusyError,
    exclusive_resource_lock,
)


def _hold_resource_lock(lock_path: str, ready: Any) -> None:
    with exclusive_resource_lock(Path(lock_path), timeout_secs=0.0, run_id="child"):
        ready.set()
        time.sleep(10.0)


def test_resource_lock_records_owner_and_enforces_private_permissions(tmp_path: Path) -> None:
    lock_path = tmp_path / "private" / "asr-experiment.lock"

    with exclusive_resource_lock(lock_path, timeout_secs=0.0, run_id="run-001") as owner:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))

        assert owner.run_id == "run-001"
        assert payload["pid"] == owner.pid
        assert payload["run_id"] == "run-001"
        assert payload["started_at"].endswith("Z")
        assert lock_path.parent.stat().st_mode & 0o777 == 0o700
        assert lock_path.stat().st_mode & 0o777 == 0o600


def test_custom_lock_does_not_change_existing_parent_permissions(tmp_path: Path) -> None:
    shared_parent = tmp_path / "shared"
    shared_parent.mkdir(mode=0o755)
    lock_path = shared_parent / "asr-experiment.lock"

    with exclusive_resource_lock(lock_path, timeout_secs=0.0):
        assert shared_parent.stat().st_mode & 0o777 == 0o755


def test_second_owner_fails_fast_with_stable_resource_busy_error(tmp_path: Path) -> None:
    lock_path = tmp_path / "asr-experiment.lock"

    with (
        exclusive_resource_lock(lock_path, timeout_secs=0.0, run_id="first"),
        pytest.raises(ResourceBusyError, match="RESOURCE_BUSY") as error,
        exclusive_resource_lock(lock_path, timeout_secs=0.0, run_id="second"),
    ):
        pytest.fail("competing owner unexpectedly acquired the resource lock")

    assert error.value.lock_path == lock_path


def test_resource_lock_is_released_after_body_error(tmp_path: Path) -> None:
    lock_path = tmp_path / "asr-experiment.lock"

    with (
        pytest.raises(RuntimeError, match="boom"),
        exclusive_resource_lock(lock_path, timeout_secs=0.0, run_id="failed"),
    ):
        raise RuntimeError("boom")

    with exclusive_resource_lock(lock_path, timeout_secs=0.0, run_id="recovered") as owner:
        assert owner.run_id == "recovered"


def test_resource_lock_excludes_another_process_and_recovers_after_exit(tmp_path: Path) -> None:
    lock_path = tmp_path / "asr-experiment.lock"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=_hold_resource_lock, args=(str(lock_path), ready))
    process.start()
    try:
        assert ready.wait(timeout=5.0)
        with pytest.raises(ResourceBusyError, match="RESOURCE_BUSY"), exclusive_resource_lock(
            lock_path,
            timeout_secs=0.0,
            run_id="parent",
        ):
            pytest.fail("parent unexpectedly acquired the child process lock")
    finally:
        process.terminate()
        process.join(timeout=5.0)

    assert process.exitcode is not None
    with exclusive_resource_lock(lock_path, timeout_secs=0.0, run_id="after-exit") as owner:
        assert owner.run_id == "after-exit"


def test_resource_lock_rejects_negative_timeout(tmp_path: Path) -> None:
    with (
        pytest.raises(ValueError, match="timeout_secs"),
        exclusive_resource_lock(tmp_path / "lock", timeout_secs=-0.1),
    ):
        pytest.fail("negative timeout unexpectedly accepted")
