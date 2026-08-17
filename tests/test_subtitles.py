"""字幕模块测试：启动器参数构造 + WS 事件协议解析。

事件 payload 形状对齐 WhisperLiveKit FrontData.to_dict()：
{"status", "lines":[{speaker,text,start,end}], "buffer_transcription"(partial), ...}
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from voice_realtime.config import SubtitleSettings
from voice_realtime.subtitles.events import parse_event
from voice_realtime.subtitles.launcher import (
    build_server_argv,
    prepare_whisperlivekit,
    resolve_wlk_command,
)

FIXTURE_PARTIAL = {
    "status": "processing",
    "lines": [],
    "buffer_transcription": "你好世界",
    "buffer_diarization": "",
    "buffer_translation": "",
    "remaining_time_transcription": 0.5,
}
FIXTURE_CONFIRMED = {
    "status": "processing",
    "lines": [
        {
            "speaker": 1,
            "text": "你好世界。",
            "start": "00:00:00.000",
            "end": "00:00:02.500",
            "detected_language": "zh",
        }
    ],
    "buffer_transcription": "",
    "buffer_diarization": "",
    "buffer_translation": "",
    "remaining_time_transcription": 0.0,
}
FIXTURE_ERROR = {"type": "error", "error": "model load failed"}
FIXTURE_CONFIG = {"type": "config", "useAudioWorklet": False, "mode": "full"}


class TestParseEvent:
    def test_partial_payload_maps_to_partial_event(self) -> None:
        event = parse_event(FIXTURE_PARTIAL)
        assert event.kind == "partial"
        assert event.text == "你好世界"

    def test_confirmed_payload_maps_to_confirmed_segments(self) -> None:
        events = parse_event(FIXTURE_CONFIRMED)
        assert events.kind == "confirmed"
        assert events.text == "你好世界。"
        assert events.start == "00:00:00.000"
        assert events.end == "00:00:02.500"
        assert events.speaker == 1

    def test_error_payload_maps_to_error_event(self) -> None:
        event = parse_event(FIXTURE_ERROR)
        assert event.kind == "error"
        assert "model load failed" in (event.text or "")

    def test_config_payload_maps_to_config_event(self) -> None:
        event = parse_event(FIXTURE_CONFIG)
        assert event.kind == "config"

    def test_empty_payload_is_other(self) -> None:
        event = parse_event({})
        assert event.kind == "other"

    def test_raw_payload_preserved(self) -> None:
        event = parse_event(FIXTURE_PARTIAL)
        assert event.raw == FIXTURE_PARTIAL


class TestBuildServerArgv:
    def test_builds_wlk_serve_command(self) -> None:
        settings = SubtitleSettings(
            repo_path=Path("/tmp/WhisperLiveKit"),
            backend="qwen3-streaming",
            language="Chinese",
            host="127.0.0.1",
            port=8001,
        )
        argv = build_server_argv(settings)
        assert argv[0] == "wlk"
        assert "serve" in argv
        assert "--backend" in argv and "qwen3-streaming" in argv
        assert "--language" in argv and "Chinese" in argv
        assert "--host" in argv and "127.0.0.1" in argv
        assert "--port" in argv and "8001" in argv

    def test_funasr_backend_uses_model_dir(self) -> None:
        settings = SubtitleSettings(backend="funasr")
        argv = build_server_argv(settings)
        assert "--backend" in argv and "funasr" in argv

    def test_default_repo_path_resolves(self) -> None:
        settings = SubtitleSettings()
        assert settings.repo_path.name == "WhisperLiveKit"


class TestPrepare:
    def test_resolve_wlk_finds_repo_venv_binary(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "wlk").write_text("#!/bin/sh\necho wlk\n")
        (bin_dir / "wlk").chmod(0o755)
        path = resolve_wlk_command(tmp_path)
        assert path == str(bin_dir / "wlk")

    def test_resolve_wlk_falls_back_to_path(self, tmp_path: Path) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/wlk"):
            path = resolve_wlk_command(tmp_path)
        assert path == "/usr/local/bin/wlk"

    def test_resolve_wlk_none_raises(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(RuntimeError, match="未找到 wlk"),
        ):
            resolve_wlk_command()

    def test_prepare_missing_repo_raises(self, tmp_path: Path) -> None:
        settings = SubtitleSettings(repo_path=tmp_path / "nope")
        with pytest.raises(FileNotFoundError, match="WhisperLiveKit"):
            prepare_whisperlivekit(settings)

    def test_prepare_repo_without_wlk_installs(self, tmp_path: Path) -> None:
        repo = tmp_path / "WhisperLiveKit"
        repo.mkdir()
        settings = SubtitleSettings(repo_path=repo, backend="qwen3-streaming")
        with (
            patch(
                "voice_realtime.subtitles.launcher.resolve_wlk_command",
                side_effect=[RuntimeError("未找到 wlk"), "/fake/wlk"],
            ) as resolve,
            patch("voice_realtime.subtitles.launcher.install_deps", return_value=None) as install,
        ):
            prepare_whisperlivekit(settings)
        install.assert_called_once_with(repo)
        assert resolve.call_count == 2
