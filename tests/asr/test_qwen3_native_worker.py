"""Qwen3 隔离 worker 的同步帧循环测试；不加载真实模型。"""

from __future__ import annotations

import base64
import json
import struct
from io import BytesIO
from pathlib import Path

from voice_realtime.asr.workers.qwen3_native_worker import (
    Qwen3ASREngine,
    WorkerIdentity,
    serve,
)


def _frame(payload: dict[str, object]) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode()
    return struct.pack(">I", len(body)) + body


def _read_frames(payload: bytes) -> list[dict[str, object]]:
    stream = BytesIO(payload)
    frames: list[dict[str, object]] = []
    while header := stream.read(4):
        length = struct.unpack(">I", header)[0]
        value = json.loads(stream.read(length))
        assert isinstance(value, dict)
        frames.append(value)
    return frames


def test_engine_uses_none_for_auto_detect_language() -> None:
    calls: list[object] = []

    class Result:
        text = "mixed result"
        language = "Chinese,English"

    class FakeModel:
        def transcribe(self, **kwargs: object) -> list[Result]:
            calls.append(kwargs["language"])
            return [Result()]

    engine = object.__new__(Qwen3ASREngine)
    engine._model = FakeModel()  # type: ignore[attr-defined]

    text, language = engine.transcribe(b"\x00\x00", language="auto", context="")

    assert calls == [None]
    assert (text, language) == ("mixed result", "Chinese,English")


def test_worker_loads_once_and_serves_multiple_framed_requests(tmp_path: Path) -> None:
    pcm = b"\x00\x00\x01\x00"
    requests = BytesIO(
        _frame(
            {
                "type": "start",
                "backend_id": "qwen3-asr-native",
                "model_dir": str(tmp_path),
                "device": "mps",
            }
        )
        + _frame(
            {
                "type": "transcribe",
                "request_id": 1,
                "sample_rate": 16_000,
                "channels": 1,
                "sample_width_bytes": 2,
                "language": "Chinese",
                "context": "术语",
                "pcm_b64": base64.b64encode(pcm).decode(),
            }
        )
        + _frame(
            {
                "type": "transcribe",
                "request_id": 2,
                "sample_rate": 16_000,
                "channels": 1,
                "sample_width_bytes": 2,
                "language": "English",
                "context": "",
                "pcm_b64": base64.b64encode(pcm).decode(),
            }
        )
    )
    responses = BytesIO()
    loads: list[Path] = []
    calls: list[tuple[bytes, str, str]] = []

    class FakeEngine:
        identity = WorkerIdentity(device="mps", dtype="float16")

        def transcribe(self, audio: bytes, *, language: str, context: str) -> tuple[str, str]:
            calls.append((audio, language, context))
            return f"result-{len(calls)}", language

    def engine_factory(model_dir: Path, device: str) -> FakeEngine:
        assert device == "mps"
        loads.append(model_dir)
        return FakeEngine()

    serve(
        requests,
        responses,
        model_dir=tmp_path,
        device="mps",
        engine_factory=engine_factory,
    )

    frames = _read_frames(responses.getvalue())
    assert len(loads) == 1
    assert [frame["type"] for frame in frames] == ["ready", "result", "result"]
    assert [frame.get("request_id") for frame in frames[1:]] == [1, 2]
    assert calls == [(pcm, "Chinese", "术语"), (pcm, "English", "")]


def test_worker_returns_redacted_error_for_invalid_pcm(tmp_path: Path) -> None:
    requests = BytesIO(
        _frame(
            {
                "type": "start",
                "backend_id": "qwen3-asr-native",
                "model_dir": str(tmp_path),
                "device": "cpu",
            }
        )
        + _frame(
            {
                "type": "transcribe",
                "request_id": 1,
                "sample_rate": 16_000,
                "channels": 1,
                "sample_width_bytes": 2,
                "language": "Chinese",
                "context": "",
                "pcm_b64": "not-base64!",
            }
        )
    )
    responses = BytesIO()

    class FakeEngine:
        identity = WorkerIdentity(device="cpu", dtype="float32")

        def transcribe(self, audio: bytes, *, language: str, context: str) -> tuple[str, str]:
            del audio, language, context
            raise AssertionError("must not run")

    serve(
        requests,
        responses,
        model_dir=tmp_path,
        device="cpu",
        engine_factory=lambda model_dir, device: FakeEngine(),
    )

    error = _read_frames(responses.getvalue())[-1]
    assert error == {
        "type": "error",
        "code": "QWEN3_WORKER_INVALID_REQUEST",
        "request_id": 1,
    }
