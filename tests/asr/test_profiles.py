"""ASR profile 判别配置与旧配置映射测试。"""

from pathlib import Path

from voice_realtime.asr.profiles import (
    WLKAutoProfile,
    WLKQwen3Profile,
    WLKSenseVoiceProfile,
)
from voice_realtime.config import SubtitleSettings


def test_default_subtitle_backend_maps_to_qwen3_profile() -> None:
    settings = SubtitleSettings()

    profile = settings.asr_profile

    assert isinstance(profile, WLKQwen3Profile)
    assert profile.kind == "wlk-qwen3-streaming"
    assert profile.model_dir == Path("runtime/qwen3-asr-1.7b")
    assert profile.device == "mps"
    assert profile.chunk_sec == 2.0


def test_legacy_funasr_maps_to_sensevoice_profile_without_qwen_fields() -> None:
    settings = SubtitleSettings(backend="funasr", model_dir=Path("runtime/sensevoice"))

    profile = settings.asr_profile

    assert isinstance(profile, WLKSenseVoiceProfile)
    assert profile.kind == "wlk-sensevoice"
    assert "chunk_sec" not in profile.model_dump()
    assert "context" not in profile.model_dump()


def test_legacy_auto_remains_explicit_compatibility_profile() -> None:
    settings = SubtitleSettings(backend="auto")

    assert isinstance(settings.asr_profile, WLKAutoProfile)
    assert settings.asr_profile.kind == "wlk-auto"


def test_profile_records_whether_speaker_labels_are_enabled() -> None:
    settings = SubtitleSettings(diarization=False)

    assert not settings.asr_profile.speaker_labels
