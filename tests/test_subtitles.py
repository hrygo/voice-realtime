"""字幕模块测试：启动器参数构造 + WS 事件协议解析。

事件 payload 形状对齐 WhisperLiveKit FrontData.to_dict()：
{"status", "lines":[{speaker,text,start,end}], "buffer_transcription"(partial), ...}
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from voice_realtime.config import SubtitleSettings
from voice_realtime.subtitles.events import SubtitleStream, parse_event, parse_events
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

    def test_snapshot_emits_every_confirmed_line_and_current_partial(self) -> None:
        payload = {
            "lines": [
                {"speaker": 1, "text": "第一句", "start": "00:00:00.000", "end": "00:00:01.000"},
                {"speaker": 2, "text": "第二句", "start": "00:00:01.000", "end": "00:00:02.000"},
            ],
            "buffer_transcription": "正在说第三句",
        }

        events = parse_events(payload)

        assert [event.kind for event in events] == ["confirmed", "confirmed", "partial"]
        assert [event.text for event in events] == ["第一句", "第二句", "正在说第三句"]

    def test_partial_wins_single_event_compatibility_after_confirmed_history(self) -> None:
        payload = {
            "lines": [
                {"speaker": 1, "text": "已确认", "start": "00:00:00.000", "end": "00:00:01.000"}
            ],
            "buffer_transcription": "新的临时内容",
        }

        event = parse_event(payload)

        assert event.kind == "partial"
        assert event.text == "新的临时内容"


class TestBuildServerArgv:
    def test_builds_wlk_serve_command(self) -> None:
        model_dir = Path("/tmp/WhisperLiveKit-model")
        settings = SubtitleSettings(
            repo_path=Path("/tmp/WhisperLiveKit"),
            backend="qwen3-streaming",
            language="Chinese",
            host="127.0.0.1",
            port=8001,
            model_dir=model_dir,
            allow_model_downloads=True,
        )
        argv = build_server_argv(settings)
        assert argv[0] == "wlk"
        assert "serve" in argv
        assert "--backend" in argv and "qwen3-streaming" in argv
        assert "--language" in argv and "Chinese" in argv
        assert "--host" in argv and "127.0.0.1" in argv
        assert "--port" in argv and "8001" in argv
        assert "--pcm-input" in argv

    def test_custom_executable_is_used(self) -> None:
        settings = SubtitleSettings(allow_model_downloads=True)
        argv = build_server_argv(settings, executable="/repo/.venv/bin/wlk")
        assert argv[0] == "/repo/.venv/bin/wlk"

    def test_funasr_backend_uses_model_dir(self) -> None:
        settings = SubtitleSettings(backend="funasr", allow_model_downloads=True)
        argv = build_server_argv(settings)
        assert "--backend" in argv and "funasr" in argv

    def test_existing_local_model_directory_is_used(self, tmp_path: Path) -> None:
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        settings = SubtitleSettings(model_dir=model_dir)

        argv = build_server_argv(settings)

        assert argv[argv.index("--model_dir") + 1] == str(model_dir)
        assert "--model" not in argv

    def test_missing_local_model_fails_fast_offline(self, tmp_path: Path) -> None:
        settings = SubtitleSettings(model_dir=tmp_path / "missing")

        with pytest.raises(FileNotFoundError, match="allow_model_downloads"):
            build_server_argv(settings)

    def test_missing_local_model_uses_explicit_download_fallback(self, tmp_path: Path) -> None:
        settings = SubtitleSettings(
            model_dir=tmp_path / "missing",
            model_size="Qwen3-ASR-1.7B",
            allow_model_downloads=True,
        )

        argv = build_server_argv(settings)

        assert argv[argv.index("--model") + 1] == "Qwen3-ASR-1.7B"

    def test_default_repo_path_resolves(self) -> None:
        settings = SubtitleSettings()
        assert settings.repo_path.name == "WhisperLiveKit"

    def test_subtitle_stream_exposes_uri(self) -> None:
        stream = SubtitleStream("ws://127.0.0.1:8001", language="Chinese")
        assert "/asr?language=Chinese" in stream.uri
        assert "mode=full" in stream.uri


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


class TestLaunch:
    def test_launch_subtitles(self, tmp_path: Path) -> None:
        from voice_realtime.subtitles.launcher import launch_subtitles

        model_dir = tmp_path / "model"
        model_dir.mkdir()
        settings = SubtitleSettings(repo_path=tmp_path, model_dir=model_dir)
        log_dir = tmp_path / "logs"
        with (
            patch(
                "voice_realtime.subtitles.launcher.resolve_wlk_command",
                return_value="/fake/wlk",
            ),
            patch("subprocess.Popen") as mock_popen,
        ):
            proc = launch_subtitles(settings, log_dir)
            assert proc is not None
            assert mock_popen.called
            assert (log_dir / "subtitles.out.log").exists()
            assert (log_dir / "subtitles.err.log").exists()


class TestConfigDump:
    def test_dump_table_contains_all_sections(self) -> None:
        from voice_realtime.config import Settings

        cfg = Settings()
        table = cfg.dump_table()
        assert "BridgeSettings" in table
        assert "InteractionSettings" in table
        assert "SubtitleSettings" in table
        assert "UISettings" in table
