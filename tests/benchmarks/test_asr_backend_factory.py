"""Benchmark profile 调度、身份冻结与共享资源生命周期测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from voice_realtime.asr.adapters.qwen3_native import (
    Qwen3WorkerIdentity,
    Qwen3WorkerResult,
)
from voice_realtime.asr.contracts import ASRSessionContext
from voice_realtime.asr.profiles import (
    FunASRNanoPyTorchProfile,
    Qwen3NativeProfile,
    SenseVoiceNativeProfile,
)
from voice_realtime.benchmarks.asr.backend_factory import (
    build_backend_runtime,
    require_compatible_mode,
    sample_profile,
    verify_profile_identity,
)
from voice_realtime.benchmarks.asr.manifest import CorpusSample


def _sample(language: str = "en") -> CorpusSample:
    return CorpusSample(
        sample_id="sample-1",
        audio_path="sample.pcm",
        audio_sha256="a" * 64,
        duration_ms=1_000,
        scenario="code-switch",
        language=language,
        reference_raw="test",
        reference_normalized="test",
        license_or_consent="public",
    )


def test_native_profiles_require_offline_mode() -> None:
    qwen = Qwen3NativeProfile(
        model_dir="/model-cache/qwen",
        python_executable="/runtime/python",
        language="Chinese",
        device="mps",
    )
    sense = SenseVoiceNativeProfile(
        model_dir="/model-cache/sensevoice",
        language="zh",
    )

    require_compatible_mode(qwen, "offline")
    require_compatible_mode(sense, "offline")
    with pytest.raises(ValueError, match="offline"):
        require_compatible_mode(qwen, "realtime-1x")
    with pytest.raises(ValueError, match="offline"):
        require_compatible_mode(sense, "realtime-1x")


def test_corpus_language_source_is_applied_to_each_native_profile() -> None:
    sample = _sample(language="en")
    qwen = Qwen3NativeProfile(
        model_dir="/model-cache/qwen",
        python_executable="/runtime/python",
        language="Chinese",
        language_source="corpus",
        device="mps",
    )
    sense = SenseVoiceNativeProfile(
        model_dir="/model-cache/sensevoice",
        language="zh",
        language_source="corpus",
    )

    assert sample_profile(qwen, sample).language == "en"
    assert sample_profile(sense, sample).language == "en"


@pytest.mark.parametrize("language", ["zh-en", "en-zh", "mixed", "auto"])
def test_mixed_corpus_language_uses_native_auto_detection(language: str) -> None:
    sample = _sample(language=language)
    profiles = (
        Qwen3NativeProfile(
            model_dir="/model-cache/qwen",
            python_executable="/runtime/python",
            language="Chinese",
            language_source="corpus",
            device="mps",
        ),
        SenseVoiceNativeProfile(
            model_dir="/model-cache/sensevoice",
            language="zh",
            language_source="corpus",
        ),
        FunASRNanoPyTorchProfile(
            model_dir="/model-cache/funasr",
            language="中文",
            language_source="corpus",
            device="mps",
        ),
    )

    assert [sample_profile(profile, sample).language for profile in profiles] == [
        "auto",
        "auto",
        "auto",
    ]


def test_native_profile_identity_rejects_manifest_drift() -> None:
    profile = SenseVoiceNativeProfile(
        model_dir="/model-cache/sensevoice",
        language="zh",
        language_source="corpus",
        use_itn=True,
        ncpu=4,
    )
    parameters = {
        "language": "zh",
        "language_source": "corpus",
        "use_itn": True,
        "ncpu": 4,
    }

    verify_profile_identity(profile, device="cpu", dtype="float32", parameters=parameters)
    with pytest.raises(ValueError, match="use_itn"):
        verify_profile_identity(
            profile,
            device="cpu",
            dtype="float32",
            parameters={**parameters, "use_itn": False},
        )
    with pytest.raises(ValueError, match="dtype"):
        verify_profile_identity(
            profile,
            device="cpu",
            dtype="float16",
            parameters=parameters,
        )


async def test_qwen_runtime_reuses_worker_across_samples_and_closes_once(
    tmp_path: Path,
) -> None:
    model_dir = tmp_path / "model"
    python = tmp_path / "wlkit-python"
    model_dir.mkdir()
    python.write_bytes(b"fake")

    class FakeWorker:
        identity = Qwen3WorkerIdentity(device="mps", dtype="float16")

        def __init__(self) -> None:
            self.close_calls = 0

        async def start(self) -> Qwen3WorkerIdentity:
            return self.identity

        async def transcribe(
            self,
            audio: object,
            *,
            language: str,
            context: str,
        ) -> Qwen3WorkerResult:
            del audio, context
            return Qwen3WorkerResult("ok", language, "mps", "float16")

        async def close(self) -> None:
            self.close_calls += 1

    worker = FakeWorker()
    profile = Qwen3NativeProfile(
        model_dir=model_dir,
        python_executable=python,
        language="Chinese",
        language_source="corpus",
        device="mps",
    )
    runtime = build_backend_runtime(
        profile,
        repo_root=tmp_path / "repo",
        model_dir=model_dir,
        qwen_worker=worker,  # type: ignore[arg-type]
    )
    context = ASRSessionContext(source_epoch=1, offset_ms=0, purpose="subtitles")

    first = runtime.create_transcriber(_sample("zh"), context, lambda payload: None)
    second = runtime.create_transcriber(_sample("en"), context, lambda payload: None)
    await first.connect()
    await first.close()
    await second.connect()
    await second.close()

    assert worker.close_calls == 0
    await runtime.close()
    await runtime.close()
    assert worker.close_calls == 1


def test_sensevoice_runtime_reuses_injected_engine(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def engine(audio: object, *, language: str, use_itn: bool) -> object:
        calls.append({"audio": audio, "language": language, "use_itn": use_itn})
        return [{"text": "ok"}]

    model_dir = tmp_path / "sensevoice"
    model_dir.mkdir()
    profile = SenseVoiceNativeProfile(model_dir=model_dir, language="zh")
    runtime = build_backend_runtime(
        profile,
        repo_root=tmp_path / "repo",
        model_dir=model_dir,
        sensevoice_engine=engine,
    )
    backend = runtime.create_transcriber(
        _sample("en"),
        ASRSessionContext(source_epoch=1, offset_ms=0, purpose="subtitles"),
        lambda payload: None,
    )

    assert backend.backend_id == "sensevoice-native"
    assert backend.uri == "offline://sensevoice-native"
    assert calls == []
