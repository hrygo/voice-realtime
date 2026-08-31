import os
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_all_with_stubbed_services(
    tmp_path: Path,
    *,
    bind_host: str,
    tts_bridge_url: str | None = None,
) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture_path = tmp_path / "vr-ui.env"

    uv_stub = bin_dir / "uv"
    uv_stub.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${2:-}" == "vr-ui" ]]; then
    printf '%s\\n' \
        "VR_UI_HOST=${VR_UI_HOST:-}" \
        "VR_BRIDGE_HOST=${VR_BRIDGE_HOST:-}" \
        "VR_SUBTITLE_SPEECHRAIL_URL=${VR_SUBTITLE_SPEECHRAIL_URL:-}" \
        "VR_INTERACTION_SPEECHRAIL_REALTIME_URL=${VR_INTERACTION_SPEECHRAIL_REALTIME_URL:-}" \
        "VR_INTERACTION_TTS_BRIDGE_URL=${VR_INTERACTION_TTS_BRIDGE_URL:-}" \
        > "${VR_TEST_CAPTURE_PATH}"
else
    exec sleep 0.2
fi
""",
        encoding="utf-8",
    )
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

    env = os.environ.copy()
    for name in (
        "VR_HOST",
        "BIND_HOST",
        "HOST",
        "VR_UI_HOST",
        "VR_BRIDGE_HOST",
        "VR_SUBTITLE_SPEECHRAIL_URL",
        "VR_INTERACTION_SPEECHRAIL_REALTIME_URL",
        "VR_INTERACTION_TTS_BRIDGE_URL",
    ):
        env.pop(name, None)
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "VR_BIND_HOST": bind_host,
            "VR_TEST_CAPTURE_PATH": str(capture_path),
        }
    )
    if tts_bridge_url is not None:
        env["VR_INTERACTION_TTS_BRIDGE_URL"] = tts_bridge_url

    result = subprocess.run(
        ["bash", "scripts/run-all.sh"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    captured = dict(line.split("=", 1) for line in capture_path.read_text().splitlines())
    captured["__stdout__"] = result.stdout
    return captured


@pytest.mark.parametrize(
    ("bind_host", "expected_bind_host", "expected_tts_url"),
    [
        ("localhost", "127.0.0.1", "http://127.0.0.1:8765/v1"),
        ("lan", "0.0.0.0", "http://127.0.0.1:8765/v1"),
        ("0.0.0.0", "0.0.0.0", "http://127.0.0.1:8765/v1"),
    ],
)
def test_run_all_derives_reachable_internal_tts_url_for_bind_mode(
    tmp_path: Path,
    bind_host: str,
    expected_bind_host: str,
    expected_tts_url: str,
) -> None:
    captured = _run_all_with_stubbed_services(tmp_path, bind_host=bind_host)

    assert captured["VR_UI_HOST"] == expected_bind_host
    assert captured["VR_BRIDGE_HOST"] == expected_bind_host
    assert captured["VR_SUBTITLE_SPEECHRAIL_URL"] == "ws://127.0.0.1:8201/v2/realtime"
    assert captured["VR_INTERACTION_SPEECHRAIL_REALTIME_URL"] == (
        "ws://127.0.0.1:8201/v2/realtime"
    )
    assert captured["VR_INTERACTION_TTS_BRIDGE_URL"] == expected_tts_url


def test_run_all_preserves_explicit_tts_bridge_url(tmp_path: Path) -> None:
    explicit_url = "https://tts.internal.example/v1"

    captured = _run_all_with_stubbed_services(
        tmp_path,
        bind_host="lan",
        tts_bridge_url=explicit_url,
    )

    assert captured["VR_INTERACTION_TTS_BRIDGE_URL"] == explicit_url


def test_run_all_lan_mode_advertises_localhost_and_lan_urls(tmp_path: Path) -> None:
    captured = _run_all_with_stubbed_services(tmp_path, bind_host="lan")

    assert "本机访问: http://127.0.0.1:8100" in captured["__stdout__"]
    assert "局域网访问: http://192.168.50.8:8100" in captured["__stdout__"]


def test_run_all_shutdown_terminates_service_descendants(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    descendant_pid_path = tmp_path / "bridge-descendant.pid"
    uv_stub = bin_dir / "uv"
    uv_stub.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${2:-}" == "vr-bridge" ]]; then
    sleep 30 &
    descendant_pid=$!
    printf '%s\n' "$descendant_pid" > "${VR_TEST_DESCENDANT_PID_PATH}"
    trap 'exit 0' TERM
    wait "$descendant_pid"
else
    exec sleep 30
fi
""",
        encoding="utf-8",
    )
    uv_stub.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "VR_BIND_HOST": "localhost",
            "VR_TEST_DESCENDANT_PID_PATH": str(descendant_pid_path),
        }
    )
    process = subprocess.Popen(
        ["bash", "scripts/run-all.sh"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    descendant_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while not descendant_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert descendant_pid_path.exists()
        descendant_pid = int(descendant_pid_path.read_text().strip())

        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)

        with pytest.raises(ProcessLookupError):
            os.kill(descendant_pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if descendant_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(descendant_pid, signal.SIGTERM)
