"""SenseVoice 原生离线 adapter 的 RED 契约测试。

这些测试只验证模型边界与统一 ``StreamingTranscriber`` 契约。真实的
``funasr``、``torch`` 模型永远不在单元测试中加载；vendor 行为全部由 fake
替代，真实模型只在独立 Stage 0/Stage 1 实验进程中验证。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import numpy as np
import pytest
from pydantic import TypeAdapter, ValidationError

from voice_realtime.asr.adapters.sensevoice_native import (
    SenseVoiceNativeAdapter,
    SenseVoiceNativeEngine,
)
from voice_realtime.asr.contracts import ASREvent, ASRSessionContext
from voice_realtime.asr.profiles import ASRProfile, SenseVoiceNativeProfile


def _context(*, offset_ms: int = 250) -> ASRSessionContext:
    return ASRSessionContext(source_epoch=7, offset_ms=offset_ms, purpose="subtitles")


class FakeEngine:
    """Adapter 单元测试使用的内存 inference port。"""

    def __init__(self, result: object = "你好") -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        audio: object,
        *,
        language: str,
        use_itn: bool,
    ) -> object:
        self.calls.append(
            {
                "audio": audio,
                "language": language,
                "use_itn": use_itn,
            }
        )
        return self.result


def _adapter(
    engine: object,
    *,
    context: ASRSessionContext | None = None,
    language: str = "zh",
    use_itn: bool = True,
    raw_event_sink: Any = None,
) -> SenseVoiceNativeAdapter:
    return SenseVoiceNativeAdapter(
        engine=engine,
        language=language,
        context=context or _context(),
        use_itn=use_itn,
        raw_event_sink=raw_event_sink,
    )


async def _next_event(events: AsyncIterator[ASREvent]) -> ASREvent:
    return await anext(events)


async def test_adapter_merges_pcm_and_passes_normalized_float32_to_engine() -> None:
    engine = FakeEngine([{"text": "你好"}])
    adapter = _adapter(engine, use_itn=False)
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"

    pcm = np.asarray([-32768, -1, 0, 1, 32767], dtype="<i2").tobytes()
    await adapter.send_audio(pcm[:4])
    await adapter.send_audio(pcm[4:])
    window = await adapter.finish()

    assert len(engine.calls) == 1
    audio = engine.calls[0]["audio"]
    assert isinstance(audio, np.ndarray)
    assert audio.dtype == np.float32
    np.testing.assert_allclose(
        audio,
        np.asarray(
            [-1.0, -1 / 32768, 0.0, 1 / 32768, 32767 / 32768],
            dtype=np.float32,
        ),
    )
    assert engine.calls[0]["language"] == "zh"
    assert engine.calls[0]["use_itn"] is False
    assert window.segments[0].start_ms == 250
    assert window.segments[0].end_ms == 250


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("zh", "zh"),
        ("中文", "zh"),
        ("Chinese", "zh"),
        ("en", "en"),
        ("English", "en"),
        ("yue", "yue"),
        ("ja", "ja"),
        ("ko", "ko"),
        ("auto", "auto"),
    ],
)
async def test_adapter_strictly_maps_each_supported_language(
    language: str,
    expected: str,
) -> None:
    engine = FakeEngine([{"text": "结果"}])
    adapter = _adapter(engine, language=language)
    await adapter.connect()
    await adapter.send_audio(b"\x00\x00")
    await adapter.finish()

    assert engine.calls[0]["language"] == expected


async def test_adapter_rejects_unknown_language_instead_of_silent_auto_fallback() -> None:
    with pytest.raises(ValueError, match="language"):
        _adapter(FakeEngine(), language="French")


async def test_adapter_cleans_rich_tags_and_keeps_bounded_vendor_result() -> None:
    audited: list[dict[str, object]] = []
    engine = FakeEngine([{"key": "sample-1", "text": "<|zh|><|NEUTRAL|>你好<|HAPPY|>"}])
    adapter = _adapter(engine, raw_event_sink=audited.append)
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    await adapter.send_audio(b"\x00\x00")
    window = await adapter.finish()
    event = await _next_event(events)

    assert event.kind == "final"
    assert window.segments[0].text == "你好"
    assert audited == [
        {
            "event": "inference",
            "result": [
                {"key": "sample-1", "text": "<|zh|><|NEUTRAL|>你好<|HAPPY|>"}
            ],
        }
    ]


async def test_empty_pcm_returns_empty_final_without_loading_or_calling_engine() -> None:
    engine = FakeEngine([{"text": "不应调用"}])
    adapter = _adapter(engine)
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"

    window = await adapter.finish()
    event = await _next_event(events)

    assert event.kind == "final"
    assert event.window == window
    assert window.segments == ()
    assert window.partial == ""
    assert engine.calls == []


async def test_silence_result_is_an_empty_window_without_synthetic_segment() -> None:
    engine = FakeEngine([{"key": "silence", "text": ""}])
    adapter = _adapter(engine)
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"

    await adapter.send_audio(np.zeros(16_000, dtype="<i2").tobytes())
    window = await adapter.finish()
    event = await _next_event(events)

    assert event.kind == "final"
    assert window.segments == ()
    assert len(engine.calls) == 1


async def test_ready_then_exactly_one_final_event() -> None:
    adapter = _adapter(FakeEngine([{"text": "完整结果"}]))
    await adapter.connect()
    events = adapter.events()

    ready = await _next_event(events)
    assert ready.kind == "ready"
    await adapter.send_audio(np.zeros(160, dtype="<i2").tobytes())
    window = await adapter.finish()
    final = await _next_event(events)

    assert final.kind == "final"
    assert final.window == window
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(_next_event(events), timeout=0.2)


async def test_finish_is_idempotent_and_engine_runs_once() -> None:
    engine = FakeEngine([{"text": "只推理一次"}])
    adapter = _adapter(engine)
    await adapter.connect()
    events = adapter.events()
    await _next_event(events)
    await adapter.send_audio(np.zeros(160, dtype="<i2").tobytes())

    first, second = await asyncio.gather(adapter.finish(), adapter.finish())
    final = await _next_event(events)

    assert first == second
    assert final.kind == "final"
    assert len(engine.calls) == 1


async def test_odd_pcm_is_rejected_without_calling_engine() -> None:
    engine = FakeEngine()
    adapter = _adapter(engine)
    await adapter.connect()

    with pytest.raises(ValueError, match="偶数字节"):
        await adapter.send_audio(b"\x00")
    assert engine.calls == []


async def test_malformed_vendor_result_emits_stable_error_event() -> None:
    adapter = _adapter(FakeEngine({"unexpected": "shape"}))
    await adapter.connect()
    events = adapter.events()
    await _next_event(events)
    await adapter.send_audio(b"\x00\x00")

    with pytest.raises(RuntimeError, match="SENSEVOICE_ENGINE_ERROR"):
        await adapter.finish()
    event = await _next_event(events)

    assert event.kind == "error"
    assert event.error_code == "SENSEVOICE_ENGINE_ERROR"
    assert "unexpected" not in (event.error_message or "")


async def test_engine_exception_is_redacted_into_stable_error_event() -> None:
    class BrokenEngine:
        def __call__(
            self,
            _audio: object,
            *,
            language: str,
            use_itn: bool,
        ) -> object:
            del language, use_itn
            raise ValueError("checkpoint=/private/secret/model.pt")

    adapter = _adapter(BrokenEngine())
    await adapter.connect()
    events = adapter.events()
    await _next_event(events)
    await adapter.send_audio(b"\x00\x00")

    with pytest.raises(RuntimeError, match="SENSEVOICE_ENGINE_ERROR") as exc_info:
        await adapter.finish()
    event = await _next_event(events)

    assert event.kind == "error"
    assert event.error_code == "SENSEVOICE_ENGINE_ERROR"
    assert "checkpoint" not in (event.error_message or "")
    assert "secret" not in str(exc_info.value)


def _fake_torch(*, mps_available: bool = False) -> ModuleType:
    module = ModuleType("torch")
    module.backends = SimpleNamespace(
        mps=SimpleNamespace(is_available=lambda: mps_available, is_built=lambda: True)
    )
    module.set_num_threads = lambda _value: None  # type: ignore[attr-defined]
    return module


def _install_fake_funasr(
    monkeypatch: pytest.MonkeyPatch,
    *,
    instances: list[object],
    calls: list[dict[str, object]],
) -> None:
    module = ModuleType("funasr")

    class FakeAutoModel:
        def __init__(self, **kwargs: object) -> None:
            instances.append(self)
            self.kwargs = kwargs

        def generate(self, **kwargs: object) -> list[dict[str, str]]:
            calls.append(kwargs)
            return [{"text": "fake-result"}]

    module.AutoModel = FakeAutoModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "funasr", module)


def _complete_model_dir(tmp_path: Path) -> Path:
    model_dir = tmp_path / "sensevoice"
    model_dir.mkdir()
    (model_dir / "config.yaml").write_text("model: SenseVoiceSmall\n", encoding="utf-8")
    (model_dir / "configuration.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.pt").write_bytes(b"fake-model")
    (model_dir / "am.mvn").write_bytes(b"fake-cmvn")
    (model_dir / "chn_jpn_yue_eng_ko_spectok.bpe.model").write_bytes(b"fake-bpe")
    (model_dir / "tokens.json").write_text("{}", encoding="utf-8")
    return model_dir


def test_engine_is_lazy_and_loads_one_model_for_multiple_samples(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    instances: list[object] = []
    calls: list[dict[str, object]] = []
    _install_fake_funasr(monkeypatch, instances=instances, calls=calls)
    monkeypatch.setitem(sys.modules, "torch", _fake_torch())

    model_dir = _complete_model_dir(tmp_path)
    engine = SenseVoiceNativeEngine(model_dir, device="cpu", ncpu=4)
    assert instances == []

    audio = np.zeros(160, dtype=np.float32)
    first = engine(audio, language="zh", use_itn=True)
    second = engine(audio, language="en", use_itn=False)

    assert first == [{"text": "fake-result"}]
    assert second == [{"text": "fake-result"}]
    assert len(instances) == 1
    assert len(calls) == 2
    assert calls[0]["language"] == "zh"
    assert calls[0]["use_itn"] is True
    assert calls[0]["cache"] == {}
    assert calls[1]["language"] == "en"
    assert calls[1]["use_itn"] is False
    assert instances[0].kwargs["model"] == str(model_dir)
    assert instances[0].kwargs["device"] == "cpu"
    assert instances[0].kwargs["disable_update"] is True


def test_engine_rejects_mps_as_a_cpu_only_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    instances: list[object] = []
    calls: list[dict[str, object]] = []
    _install_fake_funasr(monkeypatch, instances=instances, calls=calls)
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(mps_available=True))

    with pytest.raises(ValueError, match="cpu"):
        SenseVoiceNativeEngine(_complete_model_dir(tmp_path), device="mps")
    assert instances == []
    assert calls == []


def test_engine_rejects_relative_model_path(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(ValueError, match="绝对路径"):
        SenseVoiceNativeEngine(Path("runtime/sensevoice"), device="cpu")


def test_sensevoice_profile_is_cpu_only_and_external_path() -> None:
    profile = TypeAdapter(ASRProfile).validate_python(
        {
            "kind": "sensevoice-native",
            "model_dir": "/model-cache/iic--SenseVoiceSmall/snapshots/master",
            "language": "zh",
            "use_itn": True,
            "ncpu": 4,
        }
    )

    assert isinstance(profile, SenseVoiceNativeProfile)
    assert profile.device == "cpu"
    assert profile.kind == "sensevoice-native"
    assert profile.model_dir.is_absolute()

    with pytest.raises(ValidationError, match="mps"):
        SenseVoiceNativeProfile(
            model_dir="/model-cache/iic--SenseVoiceSmall/snapshots/master",
            language="zh",
            device="mps",  # type: ignore[arg-type]
        )
