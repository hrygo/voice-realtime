"""统一音频帧与采集配置契约测试。"""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from voice_realtime.audio.frame import (
    AudioFrame,
    AudioFrameFlag,
    AudioSourceKind,
    AudioSourceRole,
)
from voice_realtime.audio.profile import CaptureProfile


def test_audio_frame_accepts_normalized_pcm() -> None:
    frame = AudioFrame(
        capture_id=UUID("00000000-0000-0000-0000-000000000001"),
        source_id="mic-main",
        source_kind=AudioSourceKind.MICROPHONE,
        source_role=AudioSourceRole.NEAR_END,
        device_generation=0,
        sequence=7,
        host_time_ns=123_000,
        pcm=b"\x00\x00" * 512,
    )

    assert frame.samples_per_channel == 512
    assert frame.duration_ns == 32_000_000


def test_audio_frame_rejects_wrong_payload_size() -> None:
    with pytest.raises(ValueError, match="PCM payload"):
        AudioFrame(
            capture_id=UUID(int=1),
            source_id="mic-main",
            source_kind=AudioSourceKind.MICROPHONE,
            source_role=AudioSourceRole.NEAR_END,
            device_generation=0,
            sequence=0,
            host_time_ns=1,
            pcm=b"\x00\x00",
        )


def test_audio_frame_allows_empty_eof_only() -> None:
    frame = AudioFrame(
        capture_id=UUID(int=1),
        source_id="mic-main",
        source_kind=AudioSourceKind.MICROPHONE,
        source_role=AudioSourceRole.NEAR_END,
        device_generation=0,
        sequence=9,
        host_time_ns=1,
        flags=AudioFrameFlag.END_OF_STREAM,
        pcm=b"",
    )

    assert frame.flags & AudioFrameFlag.END_OF_STREAM

    with pytest.raises(ValueError, match="PCM payload"):
        AudioFrame(
            capture_id=UUID(int=1),
            source_id="mic-main",
            source_kind=AudioSourceKind.MICROPHONE,
            source_role=AudioSourceRole.NEAR_END,
            device_generation=0,
            sequence=10,
            host_time_ns=1,
            pcm=b"",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sample_rate", 48_000),
        ("channels", 2),
        ("sample_width", 4),
        ("samples_per_channel", 160),
        ("device_generation", -1),
        ("sequence", -1),
        ("host_time_ns", -1),
    ],
)
def test_audio_frame_rejects_non_normalized_or_negative_fields(
    field: str,
    value: int,
) -> None:
    values: dict[str, object] = {
        "capture_id": UUID(int=1),
        "source_id": "mic-main",
        "source_kind": AudioSourceKind.MICROPHONE,
        "source_role": AudioSourceRole.NEAR_END,
        "device_generation": 0,
        "sequence": 0,
        "host_time_ns": 1,
        "pcm": b"\x00\x00" * 512,
    }
    values[field] = value

    with pytest.raises(ValueError):
        AudioFrame(**values)  # type: ignore[arg-type]


def test_capture_profile_defaults_to_legacy_microphone() -> None:
    profile = CaptureProfile.microphone()

    assert profile.legacy_audio_source == "microphone"
    assert profile.sources[0].kind is AudioSourceKind.MICROPHONE
    assert profile.sources[0].role is AudioSourceRole.NEAR_END


def test_capture_profile_projects_output_and_dual_for_v1_compatibility() -> None:
    output = CaptureProfile.model_validate(
        {
            "sources": [
                {"kind": "physical_output", "role": "far_end"},
            ]
        }
    )
    dual = CaptureProfile.model_validate(
        {
            "mode": "dual",
            "sources": [
                {"kind": "microphone", "role": "near_end"},
                {"kind": "physical_output", "role": "far_end"},
            ],
        }
    )

    assert output.legacy_audio_source == "physical_output"
    assert dual.legacy_audio_source == "mixed"


def test_capture_profile_rejects_invalid_dual_layout() -> None:
    with pytest.raises(ValidationError, match="dual"):
        CaptureProfile.model_validate(
            {
                "mode": "dual",
                "sources": [
                    {"kind": "microphone", "role": "near_end"},
                    {"kind": "microphone", "role": "near_end"},
                ],
            }
        )


def test_capture_profile_rejects_multiple_single_sources_and_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="single"):
        CaptureProfile.model_validate(
            {
                "sources": [
                    {"kind": "microphone", "role": "near_end"},
                    {"kind": "physical_output", "role": "far_end"},
                ]
            }
        )

    with pytest.raises(ValidationError, match="extra_forbidden"):
        CaptureProfile.model_validate(
            {
                "sources": [{"kind": "microphone", "role": "near_end"}],
                "device_uid": "must-not-cross-domain-boundary",
            }
        )
