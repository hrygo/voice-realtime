from __future__ import annotations

import asyncio
import plistlib
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import smoke_audio_capture_helper as smoke

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_DIR = PROJECT_ROOT / "native/vr-audio-capture/Resources"
INFO_PLIST = RESOURCE_DIR / "Info.plist"
ENTITLEMENTS = RESOURCE_DIR / "VRAudioCapture.entitlements"
BUILD_SCRIPT = PROJECT_ROOT / "scripts/build-audio-capture-helper.sh"
TEST_SCRIPT = PROJECT_ROOT / "scripts/test-audio-capture-helper.sh"
SMOKE_SCRIPT = PROJECT_ROOT / "scripts/smoke_audio_capture_helper.py"
APP_PATH = PROJECT_ROOT / "build/vr-audio-capture/vr-audio-capture.app"


def test_audio_capture_bundle_metadata_is_minimal_and_permission_scoped() -> None:
    with INFO_PLIST.open("rb") as file:
        info = plistlib.load(file)

    assert info["CFBundleIdentifier"] == "local.voice-realtime.audio-capture"
    assert info["CFBundleExecutable"] == "vr-audio-capture-helper"
    assert info["CFBundlePackageType"] == "APPL"
    assert info["LSMinimumSystemVersion"] == "14.2"
    assert info["LSUIElement"] is True
    assert info["NSAudioCaptureUsageDescription"]
    assert "NSMicrophoneUsageDescription" not in info


def test_audio_capture_entitlements_have_no_network_or_sandbox_expansion() -> None:
    with ENTITLEMENTS.open("rb") as file:
        entitlements = plistlib.load(file)

    assert entitlements == {}


def test_audio_capture_build_and_test_scripts_enforce_release_contract() -> None:
    build_script = BUILD_SCRIPT.read_text(encoding="utf-8")
    test_script = TEST_SCRIPT.read_text(encoding="utf-8")

    assert BUILD_SCRIPT.stat().st_mode & 0o111
    assert TEST_SCRIPT.stat().st_mode & 0o111
    assert "-c release" in build_script
    assert "--options runtime" in build_script
    assert "VR_AUDIO_CAPTURE_SIGNING_IDENTITY" in build_script
    assert "VR_AUDIO_CAPTURE_CODESIGN_TIMESTAMP" in build_script
    assert "Developer ID Application:" not in build_script
    assert "--verify --deep --strict" in test_script
    assert "com\\.apple\\.security\\.network" in test_script
    assert "--list-devices-json" in test_script


def test_audio_capture_resources_never_bundle_audio_or_socket_files() -> None:
    forbidden_suffixes = {".wav", ".pcm", ".sock", ".socket"}
    assert not [
        path
        for path in RESOURCE_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in forbidden_suffixes
    ]


def test_manual_capture_smoke_requires_explicit_permission_confirmation() -> None:
    source = SMOKE_SCRIPT.read_text(encoding="utf-8")
    assert "--i-understand-this-will-request-system-audio-permission" in source
    assert "prepare_capture" in source
    assert "commit_capture" in source
    assert "stop_capture" in source
    assert ".pcm" in source and ".wav" in source

    result = subprocess.run(
        [sys.executable, str(SMOKE_SCRIPT), "--help"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "系统音频录制权限" in result.stdout


def test_manual_capture_collection_finishes_cleanly_at_deadline() -> None:
    class FakeClient:
        def pcm_messages(self) -> AsyncIterator[SimpleNamespace]:
            async def frames() -> AsyncIterator[SimpleNamespace]:
                yield SimpleNamespace(sequence=7, pcm=bytes(1_024))
                await asyncio.Event().wait()

            return frames()

    stats = asyncio.run(smoke._collect_frames(FakeClient(), duration=0.01))  # type: ignore[arg-type]

    assert stats.frames == 1
    assert stats.first_sequence == 7
    assert stats.last_sequence == 7


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS bundle verification")
def test_built_audio_capture_bundle_when_present() -> None:
    if not APP_PATH.exists():
        pytest.skip("ignored app bundle has not been built")
    subprocess.run(
        [str(TEST_SCRIPT), "--static"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
