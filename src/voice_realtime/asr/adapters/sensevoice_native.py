"""SenseVoiceSmall 原生离线实验适配器。"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from itertools import islice
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from voice_realtime.asr.contracts import ASRCapabilities, ASREvent, ASRSessionContext
from voice_realtime.meeting.models import NormalizedSegment, TranscriptWindow

_SAMPLE_RATE = 16_000
_SAMPLE_WIDTH_BYTES = 2
_MAX_TRANSCRIPT_CHARS = 100_000
_MAX_RAW_EVENT_BYTES = 32 * 1024
_MAX_RAW_DEPTH = 6
_MAX_RAW_ITEMS = 256
_MAX_RAW_STRING_CHARS = 4_096
_LANGUAGES = {
    "auto": "auto",
    "zh": "zh",
    "中文": "zh",
    "chinese": "zh",
    "en": "en",
    "英文": "en",
    "english": "en",
    "yue": "yue",
    "粤语": "yue",
    "cantonese": "yue",
    "ja": "ja",
    "日文": "ja",
    "日本語": "ja",
    "japanese": "ja",
    "ko": "ko",
    "韩文": "ko",
    "한국어": "ko",
    "korean": "ko",
}


class SenseVoiceNativeInference(Protocol):
    def __call__(
        self,
        audio: Any,
        *,
        language: str,
        use_itn: bool,
    ) -> object: ...


SenseVoiceNativeRawEventSink = Callable[[Mapping[str, object]], None]


def _normalize_language(language: str) -> str:
    normalized = language.strip()
    mapped = _LANGUAGES.get(normalized.lower()) or _LANGUAGES.get(normalized)
    if mapped is None:
        raise ValueError(f"unsupported SenseVoice language: {language!r}")
    return mapped


def _json_safe(value: object, *, depth: int = 0) -> object:
    if depth > _MAX_RAW_DEPTH:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, str)):
        return value[:_MAX_RAW_STRING_CHARS] if isinstance(value, str) else value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{type(value).__name__}:{len(value)} bytes>"
    if isinstance(value, Mapping):
        return {
            str(key)[:256]: _json_safe(item, depth=depth + 1)
            for key, item in islice(value.items(), _MAX_RAW_ITEMS)
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in value[:_MAX_RAW_ITEMS]]
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return _json_safe(tolist(), depth=depth + 1)
        except Exception:
            pass
    return f"<{type(value).__name__}>"


def _bounded_audit_payload(result: object) -> dict[str, object]:
    payload: dict[str, object] = {"event": "inference", "result": _json_safe(result)}
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        return {"event": "inference", "result": "<unserializable>"}
    if len(encoded) <= _MAX_RAW_EVENT_BYTES:
        return payload
    return {
        "event": "inference",
        "result": "<truncated>",
        "result_type": type(result).__name__,
        "result_bytes": len(encoded),
    }


def _extract_raw_text(result: object) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, Mapping):
        text = result.get("text")
        if isinstance(text, str):
            return text
        raise ValueError("SenseVoice result mapping requires string text")
    if isinstance(result, (list, tuple)) and result:
        first = result[0]
        if isinstance(first, Mapping) and isinstance(first.get("text"), str):
            return str(first["text"])
    raise ValueError("SenseVoice result must contain text")


class SenseVoiceNativeEngine:
    """在一个 benchmark run 内懒加载并复用一个 CPU SenseVoice 模型。"""

    def __init__(
        self,
        model_dir: Path,
        device: Literal["cpu"] = "cpu",
        ncpu: int = 4,
    ) -> None:
        model_path = Path(model_dir).expanduser()
        if not model_path.is_absolute():
            raise ValueError("SenseVoice model_dir 必须是绝对路径")
        if device != "cpu":
            raise ValueError("SenseVoice native benchmark 仅允许 cpu")
        if ncpu < 1:
            raise ValueError("ncpu 必须大于 0")
        self.model_dir = model_path.resolve()
        self.device = device
        self.ncpu = ncpu
        self._model: Any | None = None
        self._load_lock = threading.Lock()

    def __call__(
        self,
        audio: Any,
        *,
        language: str,
        use_itn: bool,
    ) -> object:
        model = self._ensure_model()
        try:
            return model.generate(
                input=audio,
                cache={},
                batch_size=1,
                language=_normalize_language(language),
                use_itn=use_itn,
            )
        except Exception as exc:
            raise RuntimeError("SENSEVOICE_INFERENCE_ERROR: inference failed") from exc

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            self._validate_model_dir()
            from funasr import AutoModel  # type: ignore[import-untyped]

            try:
                model = AutoModel(
                    model=str(self.model_dir),
                    device="cpu",
                    ncpu=self.ncpu,
                    disable_update=True,
                    disable_pbar=True,
                    trust_remote_code=False,
                )
            except Exception as exc:
                raise RuntimeError("SENSEVOICE_MODEL_LOAD_ERROR: model load failed") from exc
            self._model = model
            return model

    def _validate_model_dir(self) -> None:
        required = (
            "config.yaml",
            "configuration.json",
            "model.pt",
            "am.mvn",
            "chn_jpn_yue_eng_ko_spectok.bpe.model",
            "tokens.json",
        )
        if not self.model_dir.is_dir():
            raise RuntimeError("SENSEVOICE_MODEL_NOT_FOUND: model_dir does not exist")
        if any(not (self.model_dir / name).is_file() for name in required):
            raise RuntimeError("SENSEVOICE_MODEL_INCOMPLETE: required files missing")


class SenseVoiceNativeAdapter:
    """把 SenseVoice 整段离线推理包装为统一转录契约。"""

    backend_id = "sensevoice-native"

    def __init__(
        self,
        *,
        engine: SenseVoiceNativeInference,
        language: str,
        context: ASRSessionContext,
        use_itn: bool = True,
        raw_event_sink: SenseVoiceNativeRawEventSink | None = None,
    ) -> None:
        self._engine = engine
        self._language = _normalize_language(language)
        self._context = context
        self._use_itn = use_itn
        self._raw_event_sink = raw_event_sink
        self._pcm = bytearray()
        self._connected = False
        self._closed = False
        self._events_active = False
        self._event_queue: asyncio.Queue[ASREvent | None] = asyncio.Queue()
        self._finish_task: asyncio.Task[TranscriptWindow] | None = None
        self.capabilities = ASRCapabilities(
            languages=frozenset({"auto", "zh", "en", "yue", "ja", "ko"}),
            supports_partial=False,
            supports_segment_timestamps=False,
            supports_word_timestamps=False,
            supports_hotwords=False,
            supports_speaker_labels=False,
            supports_native_diarization=False,
            supports_eof_flush=True,
        )

    @property
    def uri(self) -> str:
        return "offline://sensevoice-native"

    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("SENSEVOICE_CLOSED: adapter is closed")
        if self._connected:
            return
        self._connected = True
        self._event_queue.put_nowait(ASREvent(kind="ready"))

    async def send_audio(self, chunk: bytes) -> None:
        if self._closed:
            raise RuntimeError("SENSEVOICE_CLOSED: adapter is closed")
        if not self._connected:
            raise RuntimeError("SENSEVOICE_NOT_CONNECTED: adapter is not connected")
        if self._finish_task is not None:
            raise RuntimeError("SENSEVOICE_FINISHED: adapter has already finished")
        if not isinstance(chunk, bytes):
            raise TypeError("SenseVoice PCM chunk 必须是 bytes")
        if len(chunk) % _SAMPLE_WIDTH_BYTES:
            raise ValueError("SENSEVOICE_PCM_INVALID: PCM 必须是偶数字节")
        self._pcm.extend(chunk)

    def events(self) -> AsyncIterator[ASREvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[ASREvent]:
        if not self._connected:
            raise RuntimeError("SENSEVOICE_NOT_CONNECTED: adapter is not connected")
        if self._events_active:
            raise RuntimeError("SENSEVOICE_EVENTS_ALREADY_CONSUMED")
        self._events_active = True
        try:
            while True:
                event = await self._event_queue.get()
                if event is None:
                    return
                yield event
                if event.kind in {"final", "error"}:
                    return
        finally:
            self._events_active = False

    async def finish(self) -> TranscriptWindow:
        if self._closed:
            raise RuntimeError("SENSEVOICE_CLOSED: adapter is closed")
        if not self._connected:
            raise RuntimeError("SENSEVOICE_NOT_CONNECTED: adapter is not connected")
        if self._finish_task is None:
            self._finish_task = asyncio.create_task(self._finish_once())
        return await asyncio.shield(self._finish_task)

    async def _finish_once(self) -> TranscriptWindow:
        try:
            if not self._pcm:
                window = TranscriptWindow(source_epoch=self._context.source_epoch)
            else:
                audio = self._pcm_to_float32()
                result = await asyncio.to_thread(
                    self._engine,
                    audio,
                    language=self._language,
                    use_itn=self._use_itn,
                )
                if inspect.isawaitable(result):
                    result = await result
                self._audit(result)
                raw_text = _extract_raw_text(result)
                from funasr.utils.postprocess_utils import (  # type: ignore[import-untyped]
                    rich_transcription_postprocess,
                )

                text = rich_transcription_postprocess(raw_text).strip()
                if len(text) > _MAX_TRANSCRIPT_CHARS:
                    raise ValueError("transcript exceeds the size limit")
                window = self._build_window(text, len(audio))
            self._event_queue.put_nowait(
                ASREvent(
                    kind="final",
                    window=window,
                    metadata={"backend_id": self.backend_id, "is_final": True},
                )
            )
            return window
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._event_queue.put_nowait(
                ASREvent(
                    kind="error",
                    error_code="SENSEVOICE_ENGINE_ERROR",
                    error_message="SenseVoice offline inference failed",
                )
            )
            raise RuntimeError("SENSEVOICE_ENGINE_ERROR: offline inference failed") from exc

    def _pcm_to_float32(self) -> Any:
        import numpy as np

        samples = np.frombuffer(bytes(self._pcm), dtype="<i2")
        return samples.astype(np.float32) / np.float32(32768.0)

    def _build_window(self, text: str, sample_count: int) -> TranscriptWindow:
        if not text:
            return TranscriptWindow(source_epoch=self._context.source_epoch)
        duration_ms = round(sample_count * 1000 / _SAMPLE_RATE)
        start_ms = self._context.offset_ms
        end_ms = start_ms + duration_ms
        identity = f"{self.backend_id}|{self._context.source_epoch}|{start_ms}|{end_ms}|{text}"
        segment = NormalizedSegment(
            id=uuid5(NAMESPACE_URL, f"voice-realtime:segment:{identity}"),
            order=0,
            source_epoch=self._context.source_epoch,
            speaker_key=f"epoch:{self._context.source_epoch}:speaker:0",
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
            detected_language=self._language,
        )
        return TranscriptWindow(source_epoch=self._context.source_epoch, segments=(segment,))

    def _audit(self, result: object) -> None:
        if self._raw_event_sink is None:
            return
        try:
            self._raw_event_sink(_bounded_audit_payload(result))
        except Exception:
            return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected = False
        self._pcm.clear()
        self._event_queue.put_nowait(None)
