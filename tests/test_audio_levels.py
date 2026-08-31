"""服务端 PCM 能量计算与发布节流测试。"""

from __future__ import annotations

import pytest

from voice_realtime.audio.frame import AudioSourceKind
from voice_realtime.audio.levels import AudioLevelMeter, pcm16_level


def test_pcm16_level_maps_silence_to_zero() -> None:
    assert pcm16_level(b"\x00\x00" * 512) == 0.0


def test_pcm16_level_is_bounded_and_monotonic() -> None:
    quiet = pcm16_level((100).to_bytes(2, "little", signed=True) * 512)
    loud = pcm16_level((10_000).to_bytes(2, "little", signed=True) * 512)

    assert 0.0 <= quiet < loud <= 1.0


def test_pcm16_level_rejects_odd_payload() -> None:
    with pytest.raises(ValueError, match="even"):
        pcm16_level(b"\x00")


def test_meter_mirrors_microphone_into_mixed_and_throttles() -> None:
    meter = AudioLevelMeter(publish_interval_ns=50_000_000)

    assert meter.update(
        AudioSourceKind.MICROPHONE,
        b"\x10\x00" * 512,
        now_ns=1,
    )
    assert not meter.update(
        AudioSourceKind.MICROPHONE,
        b"\x10\x00" * 512,
        now_ns=2,
    )

    levels = meter.snapshot()
    assert levels.microphone == levels.mixed
    assert levels.physical_output == 0.0
    assert levels.updated_at_ns == 2


def test_meter_tracks_physical_output_as_current_single_source() -> None:
    meter = AudioLevelMeter()

    meter.update(
        AudioSourceKind.PHYSICAL_OUTPUT,
        b"\xff\x7f" * 512,
        now_ns=1,
    )

    levels = meter.snapshot()
    assert levels.physical_output > 0.0
    assert levels.mixed == levels.physical_output
    assert levels.microphone == 0.0


def test_meter_clear_removes_source_and_mixed_level() -> None:
    meter = AudioLevelMeter()
    meter.update(
        AudioSourceKind.MICROPHONE,
        b"\xff\x7f" * 512,
        now_ns=1,
    )

    meter.clear(AudioSourceKind.MICROPHONE, now_ns=2)

    assert meter.snapshot().microphone == 0.0
    assert meter.snapshot().mixed == 0.0
    assert meter.snapshot().updated_at_ns == 2


def test_meter_validates_publish_interval() -> None:
    with pytest.raises(ValueError, match="publish_interval_ns"):
        AudioLevelMeter(publish_interval_ns=-1)
