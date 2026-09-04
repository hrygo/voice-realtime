"""sona-ctl 启停脚本与 run-all 委托链的契约测试。

这些测试锁定 scripts/sona-ctl.sh（统一启停 / 状态 / 日志工具）与兼容入口
scripts/run-all.sh（委托给 sona-ctl）的可观察行为。为避免触碰真实运行时，每个
测试把 ``sona-ctl.sh`` + ``common.sh`` 复制到临时目录（SONA_ROOT 随之落在临时
目录，runtime/ 与 pid 文件完全隔离），并用 PATH stub 替换 ``uv`` / ``route`` /
``ipconfig``，端口使用动态分配的空闲端口。
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from contextlib import suppress
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_UV_STUB = """#!/usr/bin/env bash
set -euo pipefail
if [[ "${2:-}" == "sona-ui" ]]; then
    printf '%s\\n' "$*" > "${SONA_TEST_CAPTURE_PATH}"
    sleep 30 &
    child=$!
    printf '%s\\n' "$child" > "${SONA_TEST_DESCENDANT_PID_PATH:-/dev/null}"
    wait "$child"
else
    exec sleep 0.2
fi
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stage_sona_ctl(tmp_path: Path) -> Path:
    """把 sona-ctl.sh + common.sh 复制到 tmp/scripts，隔离 SONA_ROOT/runtime。"""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    for name in ("sona-ctl.sh", "common.sh"):
        shutil.copy(PROJECT_ROOT / "scripts" / name, scripts_dir / name)
    return scripts_dir / "sona-ctl.sh"


def _stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_stub = bin_dir / "uv"
    uv_stub.write_text(_UV_STUB, encoding="utf-8")
    uv_stub.chmod(0o755)
    for command, output in {
        "route": "interface: en0",
        "ipconfig": "192.168.50.8",
        "hostname": "192.168.50.8",
        "ip": "192.168.50.8",
    }.items():
        stub = bin_dir / command
        stub.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' '{output}'\n", encoding="utf-8")
        stub.chmod(0o755)
    return bin_dir


def _sona_ctl_env(
    bin_dir: Path,
    *,
    ui_port: str | None = None,
    capture_path: Path | None = None,
    descendant_pid_path: Path | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "SONA_BIND_HOST",
        "SONA_HOST",
        "BIND_HOST",
        "HOST",
        "SONA_UI_HOST",
        "SONA_UI_PORT",
        "SONA_SUBTITLE_SPEECHRAIL_URL",
        "SONA_INTERACTION_SPEECHRAIL_REALTIME_URL",
        "SONA_INTERACTION_SPEECHRAIL_TTS_REST_URL",
    ):
        env.pop(name, None)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["SONA_UI_PORT"] = ui_port or str(_free_port())
    env["SONA_TEST_CAPTURE_PATH"] = str(capture_path) if capture_path else "/dev/null"
    env["SONA_TEST_DESCENDANT_PID_PATH"] = (
        str(descendant_pid_path) if descendant_pid_path else "/dev/null"
    )
    return env


def _run_ctl(ctl: Path, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ctl), *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_file(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert path.exists(), f"等待文件超时: {path}"


def test_run_all_delegates_to_sona_ctl_and_never_derives_legacy_bridge_url() -> None:
    source = (PROJECT_ROOT / "scripts" / "run-all.sh").read_text(encoding="utf-8")

    assert "uv run vr-bridge" not in source
    assert "SONA_INTERACTION_TTS_BRIDGE_URL" not in source
    assert "SONA_BRIDGE_PORT" not in source
    assert "sona-ctl.sh start" in source


@pytest.mark.parametrize(
    ("bind_host", "expected_mode", "expected_line"),
    [
        ("localhost", "本机独占 (127.0.0.1，默认)", "Sona Web 控制台: http://127.0.0.1:{port}"),
        ("lan", "全部网络接口 (0.0.0.0)", "本机访问: http://127.0.0.1:{port}"),
        ("0.0.0.0", "全部网络接口 (0.0.0.0)", "本机访问: http://127.0.0.1:{port}"),
    ],
)
def test_sona_ctl_start_daemon_writes_pid_and_banner(
    tmp_path: Path,
    bind_host: str,
    expected_mode: str,
    expected_line: str,
) -> None:
    ctl = _stage_sona_ctl(tmp_path)
    bin_dir = _stub_bin(tmp_path)
    capture = tmp_path / "sona-ui-args.txt"
    env = _sona_ctl_env(bin_dir, capture_path=capture)
    env["SONA_BIND_HOST"] = bind_host

    result = _run_ctl(ctl, ["start", "-d"], env)

    assert result.returncode == 0, result.stderr
    assert "启动 sona 全套服务" in result.stdout
    assert expected_mode in result.stdout
    assert expected_line.format(port=env["SONA_UI_PORT"]) in result.stdout

    pid_file = tmp_path / "runtime" / "sona-ui.pid"
    assert pid_file.exists()
    pid = int(pid_file.read_text().strip())
    assert _pid_alive(pid)
    _wait_for_file(capture)
    assert "sona-ui" in capture.read_text()

    stop = _run_ctl(ctl, ["stop"], env)
    assert stop.returncode == 0, stop.stderr
    assert not pid_file.exists()
    assert not _pid_alive(pid)


def test_sona_ctl_lan_mode_advertises_lan_url(tmp_path: Path) -> None:
    ctl = _stage_sona_ctl(tmp_path)
    bin_dir = _stub_bin(tmp_path)
    env = _sona_ctl_env(bin_dir)
    env["SONA_BIND_HOST"] = "lan"

    result = _run_ctl(ctl, ["start", "-d"], env)

    assert result.returncode == 0, result.stderr
    assert "局域网访问: http://192.168.50.8" in result.stdout
    _run_ctl(ctl, ["stop"], env)


def test_sona_ctl_start_refuses_when_port_occupied(tmp_path: Path) -> None:
    ctl = _stage_sona_ctl(tmp_path)
    bin_dir = _stub_bin(tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = int(blocker.getsockname()[1])
        env = _sona_ctl_env(bin_dir, ui_port=str(port))

        result = _run_ctl(ctl, ["start", "-d"], env)

    assert result.returncode == 1
    assert "已被占用" in result.stdout
    assert not (tmp_path / "runtime" / "sona-ui.pid").exists()


def test_sona_ctl_start_refuses_when_already_running(tmp_path: Path) -> None:
    ctl = _stage_sona_ctl(tmp_path)
    bin_dir = _stub_bin(tmp_path)
    env = _sona_ctl_env(bin_dir)
    pid_file = tmp_path / "runtime" / "sona-ui.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    result = _run_ctl(ctl, ["start", "-d"], env)

    assert result.returncode == 1
    assert "已在运行" in result.stdout


def test_sona_ctl_stop_terminates_tree_and_is_idempotent(tmp_path: Path) -> None:
    ctl = _stage_sona_ctl(tmp_path)
    bin_dir = _stub_bin(tmp_path)
    descendant_pid_path = tmp_path / "ui-descendant.pid"
    env = _sona_ctl_env(bin_dir, descendant_pid_path=descendant_pid_path)

    start = _run_ctl(ctl, ["start", "-d"], env)
    assert start.returncode == 0, start.stderr
    pid_file = tmp_path / "runtime" / "sona-ui.pid"
    pid = int(pid_file.read_text().strip())

    _wait_for_file(descendant_pid_path)
    descendant_pid = int(descendant_pid_path.read_text().strip())

    stop = _run_ctl(ctl, ["stop"], env)
    assert stop.returncode == 0, stop.stderr
    assert "sona UI 已停止" in stop.stdout
    assert not pid_file.exists()
    assert not _pid_alive(pid)
    assert not _pid_alive(descendant_pid)

    second = _run_ctl(ctl, ["stop"], env)
    assert second.returncode == 0, second.stderr
    assert "未在运行" in second.stdout


def test_sona_ctl_status_reports_running_then_stopped(tmp_path: Path) -> None:
    ctl = _stage_sona_ctl(tmp_path)
    bin_dir = _stub_bin(tmp_path)
    env = _sona_ctl_env(bin_dir)

    stopped = _run_ctl(ctl, ["status"], env)
    assert stopped.returncode == 0, stopped.stderr
    assert "未运行" in stopped.stdout

    start = _run_ctl(ctl, ["start", "-d"], env)
    assert start.returncode == 0, start.stderr

    running = _run_ctl(ctl, ["status"], env)
    assert "运行中" in running.stdout
    _run_ctl(ctl, ["stop"], env)


def test_sona_ctl_shutdown_terminates_service_descendants(tmp_path: Path) -> None:
    """前台启动后 SIGTERM 应清理包含子进程的整棵进程树（回归保护）。"""
    ctl = _stage_sona_ctl(tmp_path)
    bin_dir = _stub_bin(tmp_path)
    descendant_pid_path = tmp_path / "ui-descendant.pid"
    env = _sona_ctl_env(bin_dir, descendant_pid_path=descendant_pid_path)

    process = subprocess.Popen(
        ["bash", str(ctl), "start"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    descendant_pid: int | None = None
    try:
        _wait_for_file(descendant_pid_path)
        descendant_pid = int(descendant_pid_path.read_text().strip())

        process.send_signal(signal.SIGTERM)
        process.wait(timeout=8)

        with pytest.raises(ProcessLookupError):
            os.kill(descendant_pid, 0)
        assert not (tmp_path / "runtime" / "sona-ui.pid").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if descendant_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(descendant_pid, signal.SIGTERM)
