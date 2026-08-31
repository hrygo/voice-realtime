"""ASR profile 判别配置与旧配置映射测试。"""

from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from voice_realtime.asr.profiles import (
    ASRProfile,
    FunASRNanoPyTorchProfile,
    FunASRNanoWSProfile,
    Qwen3NativeProfile,
    SenseVoiceNativeProfile,
    SpeechRailRealtimeProfile,
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
    assert profile.model_dir.is_absolute()
    assert "Qwen--Qwen3-ASR-1.7B/snapshots/master" in profile.model_dir.as_posix()
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


def test_speechrail_realtime_is_an_explicit_subtitle_backend() -> None:
    settings = SubtitleSettings(
        backend="speechrail-realtime-v2",
        language="Chinese",
        speechrail_url="ws://127.0.0.1:8201/v2/realtime",
        speechrail_finish_timeout_secs=12.0,
    )

    profile = settings.asr_profile

    assert isinstance(profile, SpeechRailRealtimeProfile)
    assert profile.kind == "speechrail-realtime-v2"
    assert profile.url == "ws://127.0.0.1:8201/v2/realtime"
    assert profile.final_timeout_secs == 12.0
    assert "model_dir" not in profile.model_dump()


def test_funasr_nano_ws_profile_is_discriminated_and_freezes_runtime_controls() -> None:
    profile = TypeAdapter(ASRProfile).validate_python(
        {
            "kind": "funasr-nano-ws",
            "model_dir": "/model-cache/FunAudioLLM--Fun-ASR-Nano-2512/snapshots/master",
            "language": "中文",
            "host": "127.0.0.1",
            "port": 10095,
            "hotwords": ["Voice Studio", "Fun-ASR"],
            "connect_timeout_secs": 3.0,
            "final_timeout_secs": 12.0,
        }
    )

    assert isinstance(profile, FunASRNanoWSProfile)
    assert profile.model_dir == Path(
        "/model-cache/FunAudioLLM--Fun-ASR-Nano-2512/snapshots/master"
    )
    assert profile.hotwords == ("Voice Studio", "Fun-ASR")
    assert profile.connect_timeout_secs == 3.0
    assert profile.final_timeout_secs == 12.0


@pytest.mark.parametrize("hotwords", [[""], ["   "]])
def test_funasr_nano_ws_profile_rejects_empty_hotwords(hotwords: list[str]) -> None:
    with pytest.raises(ValidationError):
        FunASRNanoWSProfile(
            model_dir="/model-cache/FunAudioLLM--Fun-ASR-Nano-2512/snapshots/master",
            language="中文",
            host="127.0.0.1",
            port=10095,
            hotwords=hotwords,
        )


def test_funasr_nano_ws_profile_rejects_project_relative_model_path() -> None:
    with pytest.raises(ValidationError, match="绝对路径"):
        FunASRNanoWSProfile(
            model_dir="runtime/fun-asr-nano-2512",
            language="中文",
            host="127.0.0.1",
            port=10095,
        )


def test_funasr_nano_pytorch_profile_is_discriminated_without_service_fields() -> None:
    profile = TypeAdapter(ASRProfile).validate_python(
        {
            "kind": "funasr-nano-pytorch",
            "model_dir": "/model-cache/FunAudioLLM--Fun-ASR-Nano-2512/snapshots/master",
            "language": "中文",
            "language_source": "corpus",
            "device": "mps",
            "hotwords": ["开放时间"],
            "itn": True,
            "ncpu": 4,
        }
    )

    assert isinstance(profile, FunASRNanoPyTorchProfile)
    assert profile.device == "mps"
    assert profile.language_source == "corpus"
    assert profile.hotwords == ("开放时间",)
    assert profile.ncpu == 4
    assert "host" not in profile.model_dump()
    assert "port" not in profile.model_dump()


def test_funasr_nano_pytorch_profile_requires_explicit_device() -> None:
    with pytest.raises(ValidationError, match="device"):
        FunASRNanoPyTorchProfile(  # type: ignore[call-arg]
            model_dir="/model-cache/FunAudioLLM--Fun-ASR-Nano-2512/snapshots/master",
            language="中文",
        )


def test_funasr_nano_pytorch_profile_rejects_relative_model_path() -> None:
    with pytest.raises(ValidationError, match="绝对路径"):
        FunASRNanoPyTorchProfile(
            model_dir="runtime/fun-asr-nano-2512",
            language="中文",
            device="mps",
        )


def test_qwen3_native_profile_freezes_isolated_runtime_identity() -> None:
    profile = TypeAdapter(ASRProfile).validate_python(
        {
            "kind": "qwen3-asr-native",
            "model_dir": "/model-cache/Qwen--Qwen3-ASR-1.7B/snapshots/master",
            "python_executable": "/opt/whisperlivekit/.venv/bin/python",
            "language": "Chinese",
            "language_source": "corpus",
            "device": "mps",
            "context": "术语：Voice Studio",
            "max_new_tokens": 512,
            "timeout_secs": 120.0,
        }
    )

    assert isinstance(profile, Qwen3NativeProfile)
    assert profile.model_dir.is_absolute()
    assert profile.python_executable.is_absolute()
    assert profile.device == "mps"
    assert profile.language_source == "corpus"
    assert profile.max_new_tokens == 512
    assert "host" not in profile.model_dump()


@pytest.mark.parametrize("field", ["model_dir", "python_executable"])
def test_qwen3_native_profile_rejects_relative_runtime_paths(field: str) -> None:
    values = {
        "model_dir": "/model-cache/Qwen--Qwen3-ASR-1.7B/snapshots/master",
        "python_executable": "/opt/whisperlivekit/.venv/bin/python",
        "language": "Chinese",
        "device": "mps",
    }
    values[field] = "runtime/local"

    with pytest.raises(ValidationError, match="绝对路径"):
        Qwen3NativeProfile(**values)  # type: ignore[arg-type]


def test_sensevoice_native_profile_defaults_to_corpus_language_and_cpu() -> None:
    profile = SenseVoiceNativeProfile(
        model_dir="/model-cache/iic--SenseVoiceSmall/snapshots/master",
        language="zh",
    )

    assert profile.language_source == "corpus"
    assert profile.device == "cpu"
