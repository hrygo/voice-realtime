"""集中配置契约测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from voice_realtime.config import (
    BridgeSettings,
    InteractionSettings,
    SubtitleSettings,
    UISettings,
)


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
    assert SubtitleSettings().allow_model_downloads is False
    assert InteractionSettings().allow_model_downloads is False


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
