"""会议助手领域模型的契约测试。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from voice_realtime.config import MeetingSettings, SubtitleSettings
from voice_realtime.meeting.models import (
    MeetingStatus,
    NormalizedSegment,
    PCMOwner,
    RuntimeMode,
    TranscriptWindow,
)


def test_meeting_settings_are_local_and_bounded(tmp_path: Path) -> None:
    settings = MeetingSettings(
        database_url="postgresql:///knowledge",
        recovery_dir=tmp_path / "recovery",
    )

    assert settings.schema_name == "voice_realtime"
    assert settings.summary_model == "qwen/qwen3.8-27b"
    assert settings.summary_reasoning == "off"
    assert settings.finalization_timeout_secs == 8.0
    assert settings.summary_concurrency == 1


def test_subtitle_diarization_defaults_are_offline_and_bounded(tmp_path: Path) -> None:
    settings = SubtitleSettings(diarization_model_path=tmp_path / "speaker.nemo")

    assert settings.diarization is True
    assert settings.diarization_backend == "sortformer"
    assert settings.diarization_max_speakers == 4
    assert settings.allow_model_downloads is False


def test_normalized_segment_rejects_invalid_time() -> None:
    with pytest.raises(ValidationError):
        NormalizedSegment(
            id=uuid4(),
            order=0,
            source_epoch=1,
            speaker_key="e1:s1",
            start_ms=200,
            end_ms=100,
            text="错误",
        )


def test_normalized_segment_rejects_blank_text() -> None:
    with pytest.raises(ValidationError):
        NormalizedSegment(
            id=uuid4(),
            order=0,
            source_epoch=1,
            speaker_key="e1:s1",
            start_ms=0,
            end_ms=100,
            text="  ",
        )


def test_transport_models_are_immutable_and_normalize_text() -> None:
    segment = NormalizedSegment(
        id=uuid4(),
        order=0,
        source_epoch=1,
        speaker_key="e1:s1",
        start_ms=0,
        end_ms=100,
        text="  你好  ",
    )
    window = TranscriptWindow(source_epoch=1, segments=(segment,))

    assert segment.text == "你好"
    assert window.segments == (segment,)
    with pytest.raises(ValidationError):
        segment.text = "修改"


def test_runtime_and_meeting_status_values_are_stable() -> None:
    assert RuntimeMode.ASSISTANT.value == "assistant"
    assert RuntimeMode.MEETING.value == "meeting"
    assert MeetingStatus.RECORDING.value == "recording"


def test_runtime_mode_and_pcm_owner_include_subtitles() -> None:
    assert RuntimeMode("assistant") is RuntimeMode.ASSISTANT
    assert RuntimeMode("subtitles") is RuntimeMode.SUBTITLES
    assert RuntimeMode("meeting") is RuntimeMode.MEETING
    assert RuntimeMode("idle") is RuntimeMode.IDLE
    assert PCMOwner("assistant") is PCMOwner.ASSISTANT
    assert PCMOwner("subtitles") is PCMOwner.SUBTITLES
    assert PCMOwner("meeting") is PCMOwner.MEETING
    assert PCMOwner("none") is PCMOwner.NONE
