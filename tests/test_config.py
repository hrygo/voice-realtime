"""集中配置契约测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sona.config import (
    InteractionSettings,
    LMStudioSettings,
    MeetingSettings,
    Settings,
    SubtitleSettings,
    UISettings,
)


def test_interaction_session_has_no_default_runtime_expiry() -> None:
    assert InteractionSettings().max_session_seconds == 0


def test_inner_os_is_disabled_by_default_and_has_bounded_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SONA_MEETING_INNER_OS_ENABLED", raising=False)
    settings = Settings(_env_file=None, meeting=MeetingSettings(_env_file=None))
    assert settings.meeting.inner_os_enabled is False
    assert settings.meeting.inner_os_analysis_enabled is False
    assert settings.meeting.inner_os_cache_ttl_secs == 1800
    assert settings.meeting.inner_os_max_cache_entries == 128
    assert settings.meeting.inner_os_cancel_timeout_secs == 2.0
    assert settings.meeting.inner_os_fact_timeout_secs == 15.0
    assert settings.meeting.inner_os_analysis_timeout_secs == 35.0
    assert settings.meeting.inner_os_max_output_chars == 65_536
    assert settings.meeting.inner_os_max_context_chars == 48_000
    assert settings.meeting.inner_os_recent_context_chars == 16_000


def test_interaction_llm_api_key_defaults_to_compatible_value_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SONA_INTERACTION_LLM_API_KEY", raising=False)
    assert InteractionSettings(_env_file=None).llm_api_key == "lm-studio"


def test_interaction_llm_api_key_strips_surrounding_whitespace() -> None:
    settings = InteractionSettings(_env_file=None, llm_api_key="  test-key  ")
    assert settings.llm_api_key == "test-key"


def test_top_level_lm_studio_settings_keep_legacy_interaction_values_compatible() -> None:
    settings = Settings(
        _env_file=None,
        interaction=InteractionSettings(
            _env_file=None,
            llm_base_url="http://127.0.0.1:1234/v1",
            llm_api_key="legacy-key",
        ),
    )

    assert settings.lm_studio.base_url == "http://127.0.0.1:1234/v1"
    assert settings.lm_studio.api_key == "legacy-key"


def test_explicit_lm_studio_settings_drive_all_consumers() -> None:
    settings = Settings(
        _env_file=None,
        lm_studio=LMStudioSettings(
            _env_file=None,
            base_url="http://localhost:2234/v1",
            api_key="shared-key",
        ),
        interaction=InteractionSettings(_env_file=None),
    )

    assert settings.interaction.llm_base_url == "http://localhost:2234/v1"
    assert settings.interaction.llm_api_key == "shared-key"


def test_settings_dump_table_redacts_llm_api_key() -> None:
    interaction = InteractionSettings(_env_file=None, llm_api_key="test-key")
    table = Settings(_env_file=None, interaction=interaction).dump_table()

    assert "test-key" not in table
    assert "llm_api_key: <redacted>" in table


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


def test_server_settings_reject_invalid_host() -> None:
    with pytest.raises(ValidationError):
        UISettings(host="invalid host name with spaces")


@pytest.mark.parametrize(
    "host", ["localhost", "127.0.0.1", "0.0.0.0", "192.168.1.100", "10.0.0.1", "::1", "::"]
)
def test_server_settings_accept_loopback_and_lan_hosts(host: str) -> None:
    assert UISettings(host=host).host == host


def test_server_settings_default_to_localhost() -> None:
    assert UISettings().host == "127.0.0.1"


def test_server_settings_resolve_lan_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sona.network.get_lan_ip", lambda: "192.168.1.123")
    assert UISettings(host="lan").host == "192.168.1.123"


def test_removed_configuration_knobs_are_not_model_fields() -> None:
    assert "interrupt_echo_suppression_ms" not in InteractionSettings.model_fields
    assert "device" not in SubtitleSettings.model_fields
    assert "stt_backend" not in InteractionSettings.model_fields
    assert "stt_model" not in InteractionSettings.model_fields
    assert "backend" not in SubtitleSettings.model_fields
    assert "model_dir" not in SubtitleSettings.model_fields


def test_speechrail_tts_configuration_is_explicit_and_uses_public_model() -> None:
    settings = InteractionSettings(
        tts_voice="warm",
        tts_language="ZH",
        speechrail_api_key="  speechrail-test-key  ",
    )

    assert settings.speechrail_tts_model == "speechrail/qwen3-tts"
    assert settings.tts_voice == "warm"
    assert settings.tts_language == "zh"
    assert settings.speechrail_api_key == "speechrail-test-key"


def test_subtitle_speechrail_api_key_is_trimmed_and_optional() -> None:
    assert SubtitleSettings(speechrail_api_key="  subtitle-key  ").speechrail_api_key == (
        "subtitle-key"
    )
    assert SubtitleSettings().speechrail_api_key is None



def test_meeting_settings_reject_invalid_database_url() -> None:
    with pytest.raises(ValidationError):
        MeetingSettings(database_url="mysql://localhost/knowledge")


def test_meeting_settings_rejects_unsafe_schema_name() -> None:
    with pytest.raises(ValidationError):
        MeetingSettings(schema="123-schema")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_base_url", "http://192.168.1.20:1234/v1"),
    ],
)
def test_interaction_accepts_lan_service_urls(field: str, value: str) -> None:
    assert getattr(InteractionSettings(**{field: value}), field) == value.rstrip("/")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_base_url", "ftp://127.0.0.1:1234/v1"),
    ],
)
def test_interaction_rejects_invalid_service_urls(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        InteractionSettings(**{field: value})


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8201/v1/realtime",
        "ws://token@127.0.0.1:8201/v1/realtime",
        "ws://127.0.0.1:8201/asr",
    ],
)
def test_interaction_rejects_non_v2_or_credentialed_speechrail_url(url: str) -> None:
    with pytest.raises(ValidationError):
        InteractionSettings(speechrail_realtime_url=url)


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


def test_meeting_summary_generation_defaults_are_bounded_for_long_reduce() -> None:
    settings = MeetingSettings()
    assert settings.summary_map_max_output_tokens == 2048
    assert settings.summary_reduce_max_output_tokens == 10240
    assert settings.summary_max_output_chars == 65536
    assert settings.summary_job_timeout_secs == 600.0
