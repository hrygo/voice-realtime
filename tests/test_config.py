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


def test_interaction_vad_defaults() -> None:
    settings = InteractionSettings()
    assert settings.vad_confidence == 0.7
    assert settings.vad_start_secs == 0.2
    assert settings.vad_min_volume == 0.65


def test_interaction_input_device_name_is_stripped() -> None:
    settings = InteractionSettings(input_device_name="  MacBook Pro  ")
    assert settings.input_device_name == "MacBook Pro"


def test_interaction_rejects_input_device_name_and_index_together() -> None:
    with pytest.raises(ValidationError, match="不能同时配置"):
        InteractionSettings(input_device=3, input_device_name="MacBook Pro")


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
def test_server_settings_reject_invalid_host(settings_type: type[object]) -> None:
    with pytest.raises(ValidationError):
        settings_type(host="invalid host name with spaces")


@pytest.mark.parametrize(
    "host", ["localhost", "127.0.0.1", "0.0.0.0", "192.168.1.100", "10.0.0.1", "::1", "::"]
)
def test_server_settings_accept_loopback_and_lan_hosts(host: str) -> None:
    assert UISettings(host=host).host == host
    assert BridgeSettings(host=host).host == host
    assert SubtitleSettings(host=host).host == host


def test_server_settings_default_to_localhost() -> None:
    assert UISettings().host == "127.0.0.1"
    assert BridgeSettings().host == "127.0.0.1"
    assert SubtitleSettings().host == "127.0.0.1"


def test_server_settings_resolve_lan_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("voice_realtime.network.get_lan_ip", lambda: "192.168.1.123")
    assert UISettings(host="lan").host == "192.168.1.123"
    assert BridgeSettings(host="LAN").host == "192.168.1.123"
    assert SubtitleSettings(host="lan_ip").host == "192.168.1.123"


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

    assert settings.model_dir.is_absolute()
    assert "Qwen--Qwen3-ASR-1.7B/snapshots/master" in settings.model_dir.as_posix()
    assert settings.qwen3_streaming_chunk_sec == 2.0
    assert settings.qwen3_streaming_left_context_sec == 12.0
    assert settings.qwen3_streaming_right_context_ms == 640
    assert settings.qwen3_streaming_hold_back_words == 6
    assert settings.qwen3_streaming_stable_iterations == 2
    assert settings.qwen3_streaming_max_new_tokens == 256
    assert settings.qwen3_streaming_device == "mps"
    assert settings.diarization_model_path.name == "diar_streaming_sortformer_4spk-v2.nemo"
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


def test_meeting_settings_reject_invalid_database_url() -> None:
    with pytest.raises(ValidationError):
        MeetingSettings(database_url="mysql://localhost/knowledge")


def test_meeting_settings_rejects_unsafe_schema_name() -> None:
    with pytest.raises(ValidationError):
        MeetingSettings(schema="voice-realtime")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_base_url", "http://192.168.1.20:1234/v1"),
        ("tts_bridge_url", "http://192.168.1.20:8765/v1"),
    ],
)
def test_interaction_accepts_lan_service_urls(field: str, value: str) -> None:
    assert getattr(InteractionSettings(**{field: value}), field) == value.rstrip("/")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_base_url", "ftp://127.0.0.1:1234/v1"),
        ("tts_bridge_url", "http://[invalid host]:8765/v1"),
    ],
)
def test_interaction_rejects_invalid_service_urls(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        InteractionSettings(**{field: value})


def test_interaction_smart_turn_and_tts_fast_clause_defaults() -> None:
    settings = InteractionSettings()
    assert settings.smart_turn_enabled is True
    assert settings.smart_turn_stop_secs == 0.45
    assert settings.tts_fast_first_clause is True
    assert settings.tts_first_clause_min_chars == 8


def test_meeting_diarization_smoothing_defaults() -> None:
    settings = MeetingSettings()
    assert settings.diarization_smoothing_enabled is True
    assert settings.diarization_min_duration_ms == 350
    assert settings.diarization_hangover_gap_ms == 800
