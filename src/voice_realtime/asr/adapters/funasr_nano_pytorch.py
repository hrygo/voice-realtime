"""Fun-ASR-Nano 原生 PyTorch 离线实验适配器。

这个模块刻意把模型生命周期和统一的流式转录端口分开：

* :class:`FunASRNanoPyTorchEngine` 只负责一次性懒加载模型，并在每次调用时
  接收已经归一化的 16 kHz ``float32`` 音频数组；
* :class:`FunASRNanoPyTorchAdapter` 只负责样本级 PCM 缓冲、事件顺序和领域
  ``TranscriptWindow`` 规范化。

因此 benchmark run 可以复用同一个 engine，而不会为每个样本重新加载
``model.pt``。模块级不导入 ``funasr``、``numpy`` 或 ``torch``，保证轻量的
协议/单元测试不会触发重型运行时。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import threading
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
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


class FunASRNanoPyTorchInference(Protocol):
    """共享离线推理 engine 的可调用端口。"""

    def __call__(
        self,
        audio: Any,
        *,
        language: str,
        hotwords: Sequence[str],
        itn: bool,
    ) -> object: ...


FunASRNanoPyTorchRawEventSink = Callable[[Mapping[str, object]], None]


def _json_safe(value: object, *, depth: int = 0) -> object:
    """把 vendor 结果转换为有限、可 JSON 序列化的值。

    ``numpy.ndarray`` 通过 ``tolist`` 变成普通列表；torch tensor、模型对象和
    其他未知对象只保留类型占位符，绝不把对象本身交给审计 sink。
    """

    if depth > _MAX_RAW_DEPTH:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            return value[:_MAX_RAW_STRING_CHARS]
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{type(value).__name__}:{len(value)} bytes>"
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_RAW_ITEMS:
                result["<truncated-items>"] = f"{len(value) - _MAX_RAW_ITEMS} more"
                break
            safe_key = str(key)[:256]
            result[safe_key] = _json_safe(item, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _json_safe(item, depth=depth + 1)
            for item in islice(value, _MAX_RAW_ITEMS)
        ]

    # numpy scalar values expose item(); ndarray exposes tolist().  Use these
    # optional protocols without importing numpy in this lightweight module.
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return _json_safe(tolist(), depth=depth + 1)
        except Exception:
            return f"<{type(value).__name__}>"
    item = getattr(value, "item", None)
    if callable(item):
        try:
            scalar = item()
        except Exception:
            return f"<{type(value).__name__}>"
        if scalar is not value:
            return _json_safe(scalar, depth=depth + 1)
    return f"<{type(value).__name__}>"


def _bounded_audit_payload(result: object) -> dict[str, object]:
    """构造长度受限的 JSON-safe vendor 审计记录。"""

    payload: dict[str, object] = {"event": "inference", "result": _json_safe(result)}
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError):
        return {
            "event": "inference",
            "result": "<unserializable>",
            "result_type": type(result).__name__,
        }
    if len(encoded) <= _MAX_RAW_EVENT_BYTES:
        return payload
    return {
        "event": "inference",
        "result": "<truncated>",
        "result_type": type(result).__name__,
        "result_bytes": len(encoded),
    }


def _result_text(value: object, *, depth: int = 0) -> str:
    """从 FunASR 常见 ``list[dict]``/dict/string 返回值提取文本。"""

    if depth > _MAX_RAW_DEPTH or value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("text", "transcript", "hypothesis"):
            if key in value:
                return _result_text(value[key], depth=depth + 1)
        for key in ("result", "output"):
            if key in value:
                text = _result_text(value[key], depth=depth + 1)
                if text:
                    return text
        return ""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        texts = [_result_text(item, depth=depth + 1) for item in value]
        return " ".join(text for text in texts if text).strip()
    return ""


class FunASRNanoPyTorchEngine:
    """Fun-ASR-Nano 的懒加载、离线 PyTorch engine。

    ``model_dir`` 必须是项目外模型缓存中的绝对路径。模型只在第一次调用
    ``__call__`` 时加载；同一 engine 后续样本复用相同的 FunASR ``AutoModel``。
    ``AutoModel`` 本身会在设备不可用时静默切到 CPU，因此这里在加载前后都做
    显式 MPS 检查，并将任何 CPU 参数视为硬错误。
    """

    def __init__(
        self,
        model_dir: Path,
        device: Literal["mps", "cpu"],
        ncpu: int = 4,
    ) -> None:
        model_path = Path(model_dir).expanduser()
        if not model_path.is_absolute():
            raise ValueError("Fun-ASR model_dir 必须是绝对路径")
        if device not in {"mps", "cpu"}:
            raise ValueError("Fun-ASR device 必须是 mps 或 cpu")
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
        hotwords: Sequence[str],
        itn: bool,
    ) -> object:
        """对单个 16 kHz float32 ndarray 执行离线推理。"""

        model = self._ensure_model()
        try:
            # FunASR 1.4.2 的公开 AutoModel 文档接受 ndarray，但
            # FunASRNano.generate_chatml 实际只处理 str 或 torch.Tensor。
            import torch

            tensor = torch.from_numpy(audio)
            return model.generate(
                input=[tensor],
                cache={},
                batch_size=1,
                hotwords=list(hotwords),
                language=None if language.strip().lower() == "auto" else language,
                itn=itn,
            )
        except Exception as exc:
            raise RuntimeError(
                "FUNASR_INFERENCE_ERROR: Fun-ASR-Nano inference failed"
            ) from exc

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            # Heavy imports intentionally remain inside the first load call.
            import torch
            from funasr import AutoModel  # type: ignore[import-untyped]

            if self.device == "mps":
                self._require_mps(torch)
            self._validate_model_dir()
            requested_device = "mps:0" if self.device == "mps" else "cpu"
            kwargs: dict[str, object] = {
                "model": str(self.model_dir),
                "trust_remote_code": False,
                "device": requested_device,
                "ncpu": self.ncpu,
                "disable_update": True,
                "disable_pbar": True,
            }
            remote_code = self.model_dir / "model.py"
            if remote_code.is_file():
                kwargs["remote_code"] = str(remote_code)
            try:
                model = AutoModel(**kwargs)
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(
                    "FUNASR_MODEL_LOAD_ERROR: Fun-ASR-Nano model load failed"
                ) from exc
            if self.device == "mps":
                self._require_model_parameters_on_mps(model)
            self._model = model
            return model

    def _validate_model_dir(self) -> None:
        if not self.model_dir.is_dir():
            raise RuntimeError("FUNASR_MODEL_NOT_FOUND: model_dir does not exist")
        required = (self.model_dir / "config.yaml", self.model_dir / "model.pt")
        if any(not path.is_file() for path in required):
            raise RuntimeError("FUNASR_MODEL_INCOMPLETE: config.yaml/model.pt missing")

    @staticmethod
    def _require_mps(torch_module: Any) -> None:
        backends = getattr(torch_module, "backends", None)
        mps = getattr(backends, "mps", None)
        if mps is None or not bool(mps.is_available()):
            raise RuntimeError("FUNASR_MPS_UNAVAILABLE: MPS device is unavailable")
        is_built = getattr(mps, "is_built", None)
        if callable(is_built) and not bool(is_built()):
            raise RuntimeError("FUNASR_MPS_UNAVAILABLE: MPS backend is not built")

    @staticmethod
    def _require_model_parameters_on_mps(model: Any) -> None:
        module = getattr(model, "model", model)
        parameters = getattr(module, "parameters", None)
        if not callable(parameters):
            raise RuntimeError(
                "FUNASR_MPS_DEVICE_MISMATCH: model parameters cannot be inspected"
            )
        try:
            devices = tuple(getattr(parameter, "device", None) for parameter in parameters())
        except Exception as exc:
            raise RuntimeError(
                "FUNASR_MPS_DEVICE_MISMATCH: model parameters cannot be inspected"
            ) from exc
        if not devices or any(
            getattr(device, "type", None) != "mps"
            or getattr(device, "index", None) not in {None, 0}
            for device in devices
        ):
            raise RuntimeError(
                "FUNASR_MPS_DEVICE_MISMATCH: model parameters are not all on mps:0"
            )


class FunASRNanoPyTorchAdapter:
    """把离线单次推理包装为 ``StreamingTranscriber``。"""

    backend_id = "funasr-nano-pytorch"
    _languages = frozenset(
        {
            "Chinese",
            "中文",
            "English",
            "英文",
            "Japanese",
            "日文",
            "日本語",
            "zh",
            "en",
            "ja",
        }
    )

    def __init__(
        self,
        *,
        engine: FunASRNanoPyTorchInference,
        language: str,
        context: ASRSessionContext,
        hotwords: Sequence[str] = (),
        itn: bool = True,
        raw_event_sink: FunASRNanoPyTorchRawEventSink | None = None,
    ) -> None:
        normalized_language = language.strip()
        if not normalized_language:
            raise ValueError("language 不能为空")
        self._engine = engine
        self._language = normalized_language
        self._context = context
        self._hotwords = tuple(word.strip() for word in hotwords if word.strip())
        self._itn = itn
        self._raw_event_sink = raw_event_sink
        self._pcm = bytearray()
        self._connected = False
        self._closed = False
        self._events_active = False
        self._event_queue: asyncio.Queue[ASREvent | None] = asyncio.Queue()
        self._finish_task: asyncio.Task[TranscriptWindow] | None = None
        self.capabilities = ASRCapabilities(
            languages=self._languages | {normalized_language},
            supports_partial=False,
            supports_segment_timestamps=False,
            supports_word_timestamps=False,
            supports_hotwords=True,
            supports_speaker_labels=False,
            supports_native_diarization=False,
            supports_eof_flush=True,
        )

    @property
    def uri(self) -> str:
        return "offline://funasr-nano-pytorch"

    async def connect(self) -> None:
        """初始化样本状态并排出唯一 ``ready`` 事件。"""

        if self._closed:
            raise RuntimeError("FUNASR_CLOSED: adapter is closed")
        if self._connected:
            return
        self._connected = True
        self._event_queue.put_nowait(ASREvent(kind="ready"))

    async def send_audio(self, chunk: bytes) -> None:
        """缓冲 16 kHz mono signed-16 little-endian PCM，不写临时音频文件。"""

        if self._closed:
            raise RuntimeError("FUNASR_CLOSED: adapter is closed")
        if not self._connected:
            raise RuntimeError("FUNASR_NOT_CONNECTED: adapter is not connected")
        if self._finish_task is not None:
            raise RuntimeError("FUNASR_FINISHED: adapter has already finished")
        if not isinstance(chunk, bytes):
            raise TypeError("Fun-ASR-Nano PCM chunk 必须是 bytes")
        if len(chunk) % _SAMPLE_WIDTH_BYTES:
            raise ValueError("FUNASR_PCM_INVALID: PCM 必须是偶数字节")
        self._pcm.extend(chunk)

    def events(self) -> AsyncIterator[ASREvent]:
        """返回单消费者事件迭代器。"""

        return self._events()

    async def _events(self) -> AsyncIterator[ASREvent]:
        if self._closed and not self._connected:
            raise RuntimeError("FUNASR_CLOSED: adapter is closed")
        if not self._connected:
            raise RuntimeError("FUNASR_NOT_CONNECTED: adapter is not connected")
        if self._events_active:
            raise RuntimeError("FUNASR_EVENTS_ALREADY_CONSUMED: only one event reader is allowed")
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
        """合并全部 PCM，调用共享 engine 一次并产生唯一 final。"""

        if self._closed:
            raise RuntimeError("FUNASR_CLOSED: adapter is closed")
        if not self._connected:
            raise RuntimeError("FUNASR_NOT_CONNECTED: adapter is not connected")
        if self._finish_task is None:
            self._finish_task = asyncio.create_task(self._finish_once())
        return await asyncio.shield(self._finish_task)

    async def _finish_once(self) -> TranscriptWindow:
        try:
            audio = self._pcm_to_float32()
            result = await asyncio.to_thread(
                self._engine,
                audio,
                language=self._language,
                hotwords=self._hotwords,
                itn=self._itn,
            )
            if inspect.isawaitable(result):
                result = await result
            self._audit(result)
            text = _result_text(result)
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
                    error_code="FUNASR_ENGINE_ERROR",
                    error_message="Fun-ASR-Nano offline inference failed",
                )
            )
            raise RuntimeError(
                "FUNASR_ENGINE_ERROR: Fun-ASR-Nano offline inference failed"
            ) from exc

    def _pcm_to_float32(self) -> Any:
        # Delayed import keeps adapter protocol tests independent of numpy import cost.
        import numpy as np

        if len(self._pcm) % _SAMPLE_WIDTH_BYTES:
            raise ValueError("FUNASR_PCM_INVALID: PCM 必须是偶数字节")
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
        payload = _bounded_audit_payload(result)
        try:
            self._raw_event_sink(payload)
        except Exception:
            # 诊断 sink 不能改变 ASR 的最终领域结果。
            return

    async def close(self) -> None:
        """释放样本缓冲；重复调用安全。"""

        if self._closed:
            return
        if self._finish_task is not None and not self._finish_task.done():
            try:
                # asyncio.to_thread 无法被取消；必须等底层推理退出，避免下一个
                # 样本与残留 MPS kernel 并发并触发进程级崩溃。
                await asyncio.shield(self._finish_task)
            except asyncio.CancelledError:
                raise
            except Exception:
                # finish() 已负责把稳定错误写入事件流；close 只做资源收敛。
                pass
        self._closed = True
        self._connected = False
        self._pcm.clear()
        self._event_queue.put_nowait(None)
