"""Fun-ASR-Nano 原生 PyTorch 离线 adapter 契约测试。"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from voice_realtime.asr.adapters.funasr_nano_pytorch import (
    FunASRNanoPyTorchAdapter,
    FunASRNanoPyTorchEngine,
)
from voice_realtime.asr.contracts import ASREvent, ASRSessionContext


def _context(*, offset_ms: int = 250) -> ASRSessionContext:
    return ASRSessionContext(source_epoch=7, offset_ms=offset_ms, purpose="subtitles")


class FakeEngine:
    def __init__(self, result: object = "你好") -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        audio: object,
        *,
        language: str,
        hotwords: Sequence[str],
        itn: bool,
    ) -> object:
        self.calls.append(
            {
                "audio": audio,
                "language": language,
                "hotwords": tuple(hotwords),
                "itn": itn,
            }
        )
        return self.result


def _adapter(
    engine: object,
    *,
    context: ASRSessionContext | None = None,
    language: str = "中文",
    hotwords: tuple[str, ...] = ("Fun-ASR", "语音助手"),
    itn: bool = True,
    raw_event_sink: Any = None,
) -> FunASRNanoPyTorchAdapter:
    return FunASRNanoPyTorchAdapter(
        engine=engine,
        language=language,
        context=context or _context(),
        hotwords=hotwords,
        itn=itn,
        raw_event_sink=raw_event_sink,
    )


async def _next_event(events: AsyncIterator[ASREvent]) -> ASREvent:
    return await anext(events)


async def test_buffers_pcm_and_passes_one_normalized_float32_array_to_engine() -> None:
    engine = FakeEngine([{"text": "你好"}])
    adapter = _adapter(engine, itn=False)
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"

    pcm = np.asarray([-32768, -1, 0, 1, 32767], dtype=np.int16).tobytes()
    await adapter.send_audio(pcm[:4])
    await adapter.send_audio(pcm[4:])
    final_window = await adapter.finish()

    assert len(engine.calls) == 1
    audio = engine.calls[0]["audio"]
    assert isinstance(audio, np.ndarray)
    assert audio.dtype == np.float32
    np.testing.assert_allclose(
        audio,
        np.asarray([-1.0, -1 / 32768, 0.0, 1 / 32768, 32767 / 32768], dtype=np.float32),
    )
    assert engine.calls[0]["language"] == "中文"
    assert engine.calls[0]["hotwords"] == ("Fun-ASR", "语音助手")
    assert engine.calls[0]["itn"] is False
    assert final_window.segments[0].start_ms == 250
    assert final_window.segments[0].end_ms == 250


async def test_ready_then_exactly_one_final_event_with_whole_audio_boundary() -> None:
    engine = FakeEngine({"text": "整段结果"})
    adapter = _adapter(engine, context=_context(offset_ms=100))
    await adapter.connect()
    events = adapter.events()

    assert (await _next_event(events)).kind == "ready"
    await adapter.send_audio(np.zeros(16_000, dtype=np.int16).tobytes())
    window = await adapter.finish()
    final_event = await _next_event(events)

    assert final_event.kind == "final"
    assert final_event.window == window
    assert len(window.segments) == 1
    assert window.segments[0].start_ms == 100
    assert window.segments[0].end_ms == 1_100
    assert not adapter.capabilities.supports_segment_timestamps
    assert not adapter.capabilities.supports_word_timestamps


async def test_blank_result_produces_empty_window_without_synthetic_segment() -> None:
    adapter = _adapter(FakeEngine([{"text": "  "}]))
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    await adapter.send_audio(np.zeros(320, dtype=np.int16).tobytes())

    window = await adapter.finish()
    event = await _next_event(events)

    assert event.kind == "final"
    assert window.segments == ()
    assert window.partial == ""


async def test_engine_exception_becomes_stable_error_event_and_finish_error() -> None:
    class BrokenEngine:
        def __call__(
            self,
            _audio: object,
            *,
            language: str,
            hotwords: Sequence[str],
            itn: bool,
        ) -> object:
            del language, hotwords, itn
            raise ValueError("checkpoint path should not be exposed")

    adapter = _adapter(BrokenEngine())
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"

    with pytest.raises(RuntimeError, match="FUNASR_ENGINE_ERROR") as exc_info:
        await adapter.finish()
    event = await _next_event(events)

    assert event.kind == "error"
    assert event.error_code == "FUNASR_ENGINE_ERROR"
    assert "checkpoint path" not in (event.error_message or "")
    assert "Traceback" not in str(exc_info.value)


async def test_finish_is_idempotent_and_engine_runs_once() -> None:
    engine = FakeEngine("一次")
    adapter = _adapter(engine)
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    await adapter.send_audio(np.zeros(160, dtype=np.int16).tobytes())

    first, second = await asyncio.gather(adapter.finish(), adapter.finish())
    final_event = await _next_event(events)

    assert first == second
    assert len(engine.calls) == 1
    assert final_event.kind == "final"


async def test_non_bytes_pcm_is_rejected() -> None:
    adapter = _adapter(FakeEngine())
    await adapter.connect()

    with pytest.raises(TypeError, match="bytes"):
        await adapter.send_audio(bytearray(b"\x00\x00"))
    with pytest.raises(ValueError, match="偶数字节"):
        await adapter.send_audio(b"\x00")


async def test_close_is_idempotent_and_unblocks_single_event_consumer() -> None:
    adapter = _adapter(FakeEngine())
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"

    await adapter.close()
    await adapter.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(_next_event(events), timeout=0.2)
    with pytest.raises(RuntimeError, match="FUNASR_CLOSED"):
        await adapter.finish()


async def test_close_waits_for_timed_out_thread_inference() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingEngine:
        def __call__(
            self,
            audio: object,
            *,
            language: str,
            hotwords: Sequence[str],
            itn: bool,
        ) -> object:
            del audio, language, hotwords, itn
            started.set()
            assert release.wait(timeout=2.0)
            return "完成"

    adapter = _adapter(BlockingEngine())
    await adapter.connect()
    await adapter.send_audio(np.zeros(160, dtype=np.int16).tobytes())

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(adapter.finish(), timeout=0.01)
    assert started.is_set()
    close_task = asyncio.create_task(adapter.close())
    await asyncio.sleep(0.01)
    assert not close_task.done()

    release.set()
    await asyncio.wait_for(close_task, timeout=1.0)


async def test_events_allows_only_one_consumer() -> None:
    adapter = _adapter(FakeEngine())
    await adapter.connect()
    first = adapter.events()
    assert (await _next_event(first)).kind == "ready"
    second = adapter.events()

    with pytest.raises(RuntimeError, match="FUNASR_EVENTS_ALREADY_CONSUMED"):
        await _next_event(second)


def test_mps_unavailable_fails_before_model_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_load_called = False

    class FakeTorch:
        backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False))

    def _auto_model(**kwargs: object) -> object:
        nonlocal model_load_called
        model_load_called = True
        return object()

    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=_auto_model))
    engine = FunASRNanoPyTorchEngine(tmp_path, "mps")

    with pytest.raises(RuntimeError, match="FUNASR_MPS_UNAVAILABLE"):
        engine(
            np.zeros(1, dtype=np.float32),
            language="中文",
            hotwords=(),
            itn=True,
        )
    assert not model_load_called


def test_mps_cpu_parameter_fallback_fails_after_mock_model_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeTorch:
        class _MPS:
            @staticmethod
            def is_available() -> bool:
                return True

        backends = SimpleNamespace(mps=_MPS())

    class FakeParameter:
        device = SimpleNamespace(type="cpu", index=None)

    class FakeModel:
        def parameters(self) -> list[FakeParameter]:
            return [FakeParameter()]

    class FakeAutoModel:
        def __init__(self, **kwargs: object) -> None:
            self.model = FakeModel()
            self.kwargs = kwargs

        def generate(self, **kwargs: object) -> list[dict[str, str]]:
            del kwargs
            return [{"text": "不会执行"}]

    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=FakeAutoModel))
    (tmp_path / "config.yaml").write_text("model: Fake", encoding="utf-8")
    (tmp_path / "model.pt").write_bytes(b"fake")
    engine = FunASRNanoPyTorchEngine(tmp_path, "mps")

    with pytest.raises(RuntimeError, match="FUNASR_MPS_DEVICE_MISMATCH"):
        engine(
            np.zeros(1, dtype=np.float32),
            language="中文",
            hotwords=(),
            itn=True,
        )


def test_engine_converts_numpy_to_tensor_before_funasr_generate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sentinel_tensor = object()
    generated_inputs: list[object] = []
    generated_languages: list[object] = []

    class FakeTorch:
        @staticmethod
        def from_numpy(audio: object) -> object:
            assert isinstance(audio, np.ndarray)
            return sentinel_tensor

    class FakeAutoModel:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def generate(self, **kwargs: object) -> list[dict[str, str]]:
            generated_inputs.extend(kwargs["input"])  # type: ignore[arg-type]
            generated_languages.append(kwargs["language"])
            return [{"text": "内存输入"}]

    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=FakeAutoModel))
    (tmp_path / "config.yaml").write_text("model: Fake", encoding="utf-8")
    (tmp_path / "model.pt").write_bytes(b"fake")
    engine = FunASRNanoPyTorchEngine(tmp_path, "cpu")

    result = engine(
        np.zeros(160, dtype=np.float32),
        language="auto",
        hotwords=(),
        itn=True,
    )

    assert result == [{"text": "内存输入"}]
    assert generated_inputs == [sentinel_tensor]
    assert generated_languages == [None]


async def test_raw_vendor_result_is_json_safe_and_bounded() -> None:
    class ModelObject:
        pass

    result = {
        "text": "安全结果",
        "array": np.asarray([1, 2, 3]),
        "bytes": b"raw",
        "model": ModelObject(),
        "huge": "x" * 100_000,
    }
    audited: list[dict[str, object]] = []
    adapter = _adapter(FakeEngine(result), raw_event_sink=audited.append)
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    await adapter.finish()
    await _next_event(events)

    assert len(audited) == 1
    encoded = json.dumps(audited[0], ensure_ascii=False)
    assert len(encoded.encode("utf-8")) <= 32 * 1024
    assert audited[0]["event"] == "inference"
    safe_result = audited[0]["result"]
    assert isinstance(safe_result, dict)
    assert safe_result["array"] == [1, 2, 3]
    assert isinstance(safe_result["model"], str)
    assert isinstance(safe_result["bytes"], str)
