"""SpeechRail-only ASR profile tests."""

import pytest
from pydantic import ValidationError

from voice_realtime.asr.profiles import SpeechRailRealtimeProfile
from voice_realtime.config import InteractionSettings, SubtitleSettings


def test_subtitle_settings_always_constructs_speechrail_profile() -> None:
    settings = SubtitleSettings(
        language="Chinese",
        speechrail_url="ws://127.0.0.1:8201/v2/realtime",
        speechrail_finish_timeout_secs=12.0,
    )

    profile = settings.asr_profile

    assert profile.kind == "speechrail-realtime-v2"
    assert profile.url == "ws://127.0.0.1:8201/v2/realtime"
    assert profile.final_timeout_secs == 12.0
    assert settings.speechrail_health_url == "http://127.0.0.1:8201/health"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8201/v2/realtime",
        "ws://token@127.0.0.1:8201/v2/realtime",
        "ws://127.0.0.1:8201/asr",
    ],
)
def test_speechrail_profile_rejects_legacy_or_credentialed_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        SpeechRailRealtimeProfile(url=url, language="zh")


def test_interaction_has_no_runtime_backend_selector() -> None:
    assert "stt_backend" not in InteractionSettings.model_fields
    assert "stt_model" not in InteractionSettings.model_fields
    assert "allow_model_downloads" not in InteractionSettings.model_fields


def test_subtitle_settings_has_no_legacy_asr_controls() -> None:
    removed = {
        "backend",
        "host",
        "port",
        "model_dir",
        "allow_model_downloads",
        "diarization",
        "qwen3_streaming_device",
    }
    assert not (removed & SubtitleSettings.model_fields.keys())
