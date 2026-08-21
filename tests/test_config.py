"""集中配置契约测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from voice_realtime.config import (
    BridgeSettings,
    InteractionSettings,
    MeetingSettings,
    SubtitleSettings,
    UISettings,
)


def test_interaction_session_has_no_default_runtime_expiry() -> None:
    assert InteractionSettings().max_session_seconds == 0


def test_interaction_context_compaction_defaults() -> None:
    settings = InteractionSettings()
    config = settings.context_compaction_config()

    assert config.enabled is True
    assert config.soft_input_tokens == 16384
    assert config.hard_input_tokens == 32768
    assert config.target_input_tokens == 8192
    assert config.recent_turn_pairs == 16
    assert config.max_unsummarized_messages == 128
    assert config.ttft_soft_seconds == 3.0
    assert config.summary_max_output_tokens == 2048
    assert config.summary_timeout_seconds == 30.0
    assert config.capacity_ratio == 0.8


@pytest.mark.parametrize(
    "kwargs",
    [
        {"context_target_input_tokens": 16384},
        {"context_soft_input_tokens": 32768},
        {"context_hard_input_tokens": 16383},
        {"context_recent_turn_pairs": 0},
        {"context_capacity_ratio": 0.99},
    ],
)
def test_interaction_context_compaction_rejects_invalid_ranges(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        InteractionSettings(**kwargs)


@pytest.mark.parametrize("sample_rate", [8000, 24000, 44100, 48000])
def test_interaction_rejects_non_16k_sample_rate(sample_rate: int) -> None:
    with pytest.raises(ValidationError):
        InteractionSettings(sample_rate=sample_rate)


@pytest.mark.parametrize("sample_rate", [8000, 16000, 44100, 48000])
def test_bridge_rejects_non_native_sample_rate(sample_rate: int) -> None:
    with pytest.raises(ValidationError):
        BridgeSettings(sample_rate=sample_rate)


@pytest.mark.parametrize("settings_type", [BridgeSettings, UISettings, SubtitleSettings])
def test_server_settings_reject_non_loopback_host(settings_type: type[object]) -> None:
    with pytest.raises(ValidationError):
        settings_type(host="0.0.0.0")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_server_settings_accept_loopback_host(host: str) -> None:
    assert UISettings(host=host).host == host


def test_removed_configuration_knobs_are_not_model_fields() -> None:
    assert "tts_voice" not in InteractionSettings.model_fields
    assert "interrupt_echo_suppression_ms" not in InteractionSettings.model_fields
    assert "device" not in SubtitleSettings.model_fields


def test_subtitle_downloads_are_disabled_by_default() -> None:
    assert BridgeSettings().allow_model_downloads is False
    assert SubtitleSettings().allow_model_downloads is False
    assert InteractionSettings().allow_model_downloads is False


def test_subtitle_defaults_to_qwen3_asr_1_7b_quality_profile() -> None:
    settings = SubtitleSettings()

    assert settings.model_dir.as_posix() == "runtime/qwen3-asr-1.7b"
    assert settings.qwen3_streaming_chunk_sec == 2.0
    assert settings.qwen3_streaming_left_context_sec == 12.0
    assert settings.qwen3_streaming_right_context_ms == 640
    assert settings.qwen3_streaming_hold_back_words == 6
    assert settings.qwen3_streaming_stable_iterations == 2
    assert settings.qwen3_streaming_max_new_tokens == 256
    assert settings.qwen3_streaming_device == "mps"
    assert settings.punctuation_split is True
    assert "Qwen3-ASR" in settings.context
    assert "保留英文、数字、连字符和大小写" in settings.context


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("qwen3_streaming_chunk_sec", 0.1),
        ("qwen3_streaming_left_context_sec", 0.5),
        ("qwen3_streaming_right_context_ms", -1),
        ("qwen3_streaming_hold_back_words", -1),
        ("qwen3_streaming_stable_iterations", 0),
        ("qwen3_streaming_max_new_tokens", 8),
        ("qwen3_streaming_device", "cuda"),
    ],
)
def test_subtitle_quality_profile_rejects_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        SubtitleSettings(**{field: value})


def test_subtitle_context_is_bounded_and_stripped() -> None:
    assert SubtitleSettings(context="  专有名词  ").context == "专有名词"
    with pytest.raises(ValidationError):
        SubtitleSettings(context="词" * 2001)


def test_meeting_settings_reject_non_loopback_database_url() -> None:
    with pytest.raises(ValidationError):
        MeetingSettings(database_url="postgresql://db.example.com/knowledge")


def test_meeting_settings_rejects_unsafe_schema_name() -> None:
    with pytest.raises(ValidationError):
        MeetingSettings(schema="voice-realtime")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_base_url", "https://example.com/v1"),
        ("tts_bridge_url", "http://192.168.1.20:8765/v1"),
    ],
)
def test_interaction_rejects_non_loopback_service_urls(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        InteractionSettings(**{field: value})
