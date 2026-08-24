"""通过隔离 Python worker 运行 Qwen3-ASR 原生离线推理。"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import struct
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from voice_realtime.asr.contracts import ASRCapabilities, ASREvent, ASRSessionContext
from voice_realtime.meeting.models import NormalizedSegment, TranscriptWindow

_BACKEND_ID = "qwen3-asr-native"
_SAMPLE_RATE = 16_000
_SAMPLE_WIDTH_BYTES = 2
_MAX_FRAME_BYTES = 64 * 1024 * 1024
_MAX_PCM_BYTES = 40 * 1024 * 1024
_MAX_TRANSCRIPT_CHARS = 100_000
_MODEL_FILES = (
    "config.json",
    "generation_config.json",
    "preprocessor_config.json",
    "tokenizer_config.json",
    "chat_template.json",
    "merges.txt",
    "vocab.json",
    "model.safetensors.index.json",
    "model-00001-of-00002.safetensors",
    "model-00002-of-00002.safetensors",
)
_LANGUAGES = {
    "zh": "Chinese",
    "中文": "Chinese",
    "chinese": "Chinese",
    "en": "English",
    "英文": "English",
    "english": "English",
    "yue": "Cantonese",
    "粤语": "Cantonese",
    "cantonese": "Cantonese",
    "ja": "Japanese",
    "日文": "Japanese",
    "日本語": "Japanese",
    "japanese": "Japanese",
    "ko": "Korean",
    "韩文": "Korean",
    "한국어": "Korean",
    "korean": "Korean",
}


class _PipeReader(Protocol):
    async def readexactly(self, size: int) -> bytes: ...

    async def read(self, size: int = -1) -> bytes: ...


class _PipeWriter(Protocol):
    def write(self, payload: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...


class _WorkerProcess(Protocol):
    stdin: _PipeWriter
    stdout: _PipeReader
    stderr: _PipeReader
    returncode: int | None

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


ProcessFactory = Callable[..., _WorkerProcess | Awaitable[_WorkerProcess]]
Qwen3NativeRawEventSink = Callable[[Mapping[str, object]], None]


class Qwen3NativeProtocolError(RuntimeError):
    """隔离 worker 返回了不可信或不一致的协议数据。"""


@dataclass(frozen=True, slots=True)
class Qwen3NativeWorkerConfig:
    repo_root: Path
    python_executable: Path
    model_dir: Path
    device: Literal["mps", "cpu"]
    worker_module: str = "voice_realtime.asr.workers.qwen3_native_worker"
    max_new_tokens: int = 512
    timeout_secs: float = 120.0

    def __post_init__(self) -> None:
        repo_root = Path(self.repo_root).expanduser()
        python_executable = Path(self.python_executable).expanduser()
        model_dir = Path(self.model_dir).expanduser()
        if not repo_root.is_absolute():
            raise ValueError("repo_root must be absolute")
        if (
            not python_executable.is_absolute()
            or not python_executable.is_file()
            or not os.access(python_executable, os.X_OK)
        ):
            raise ValueError(
                "isolated Python executable must be an executable absolute local file"
            )
        if not model_dir.is_absolute():
            raise ValueError("Qwen3 model snapshot must be an absolute local path")
        resolved_repo = repo_root.resolve(strict=True)
        resolved_model = model_dir.resolve(strict=True)
        if resolved_model.is_relative_to(resolved_repo):
            raise ValueError("Qwen3 model snapshot must be outside the repository")
        if any(not (resolved_model / name).is_file() for name in _MODEL_FILES):
            raise ValueError("Qwen3 model snapshot is incomplete")
        if not self.worker_module.strip():
            raise ValueError("worker_module cannot be empty")
        if not 32 <= self.max_new_tokens <= 2_048:
            raise ValueError("max_new_tokens must be between 32 and 2048")
        if self.timeout_secs <= 0:
            raise ValueError("timeout_secs must be positive")
        object.__setattr__(self, "repo_root", resolved_repo)
        # venv/bin/python 通常是 symlink；解析到基础解释器会绕过 pyvenv.cfg，
        # 从而丢失隔离环境的 site-packages。入口已在上方校验为本机可执行文件。
        object.__setattr__(self, "python_executable", python_executable.absolute())
        object.__setattr__(self, "model_dir", resolved_model)


@dataclass(frozen=True, slots=True)
class Qwen3WorkerIdentity:
    device: str
    dtype: str


@dataclass(frozen=True, slots=True)
class Qwen3WorkerResult:
    text: str
    language: str
    device: str
    dtype: str


def build_qwen3_worker_command(config: Qwen3NativeWorkerConfig) -> list[str]:
    """构造不含 repo ID、URL 或网络参数的隔离 worker 命令。"""
    return [
        str(config.python_executable),
        "-m",
        config.worker_module,
        "--model-dir",
        str(config.model_dir),
        "--device",
        config.device,
        "--max-new-tokens",
        str(config.max_new_tokens),
    ]


async def _default_process_factory(
    command: list[str],
    **kwargs: Any,
) -> _WorkerProcess:
    return await asyncio.create_subprocess_exec(  # type: ignore[return-value]
        *command,
        **kwargs,
    )


def _encode_frame(payload: Mapping[str, object]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not body or len(body) > _MAX_FRAME_BYTES:
        raise Qwen3NativeProtocolError("QWEN3_WORKER_FRAME_SIZE: invalid frame size")
    return struct.pack(">I", len(body)) + body


class Qwen3NativeWorker:
    """管理一个持久隔离进程，并逐请求验证模型运行身份。"""

    def __init__(
        self,
        config: Qwen3NativeWorkerConfig,
        *,
        process_factory: ProcessFactory = _default_process_factory,
    ) -> None:
        self.config = config
        self._process_factory = process_factory
        self._process: _WorkerProcess | None = None
        self._identity: Qwen3WorkerIdentity | None = None
        self._request_id = 0
        self._start_lock = asyncio.Lock()
        self._io_lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail = bytearray()
        self._closed = False

    @property
    def identity(self) -> Qwen3WorkerIdentity:
        if self._identity is None:
            raise RuntimeError("QWEN3_NOT_STARTED: worker has not started")
        return self._identity

    async def start(self) -> Qwen3WorkerIdentity:
        async with self._start_lock:
            if self._closed:
                raise RuntimeError("QWEN3_CLOSED: worker is closed")
            if self._identity is not None:
                return self._identity
            command = build_qwen3_worker_command(self.config)
            worker_env = {
                name: value
                for name in ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL")
                if (value := os.environ.get(name)) is not None
            }
            worker_env.update(
                {
                    "PYTHONPATH": str(self.config.repo_root / "src"),
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "HF_DATASETS_OFFLINE": "1",
                    "PYTORCH_ENABLE_MPS_FALLBACK": "0",
                    "TOKENIZERS_PARALLELISM": "false",
                }
            )
            created = self._process_factory(
                command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.config.repo_root),
                env=worker_env,
            )
            process = await created if inspect.isawaitable(created) else created
            self._process = process
            self._stderr_tail.clear()
            self._stderr_task = asyncio.create_task(self._drain_stderr(process))
            try:
                await self._write(
                    {
                        "type": "start",
                        "backend_id": _BACKEND_ID,
                        "model_dir": str(self.config.model_dir),
                        "device": self.config.device,
                    }
                )
                ready = await self._read_frame()
                if ready.get("type") == "error":
                    code = ready.get("code")
                    safe_code = code if isinstance(code, str) else "QWEN3_WORKER_ERROR"
                    raise Qwen3NativeProtocolError(safe_code)
                if ready.get("type") != "ready" or ready.get("backend_id") != _BACKEND_ID:
                    raise Qwen3NativeProtocolError(
                        "QWEN3_WORKER_PROTOCOL: invalid ready frame"
                    )
                if ready.get("model_loaded") is not True:
                    raise Qwen3NativeProtocolError("QWEN3_WORKER_MODEL_NOT_LOADED")
                device = ready.get("device")
                dtype = ready.get("dtype")
                if not isinstance(device, str) or not isinstance(dtype, str):
                    raise Qwen3NativeProtocolError(
                        "QWEN3_WORKER_IDENTITY: device/dtype missing"
                    )
                if device != self.config.device:
                    raise Qwen3NativeProtocolError(
                        "QWEN3_DEVICE_MISMATCH: MPS request must not fall back to CPU"
                    )
                expected_dtype = "float16" if device == "mps" else "float32"
                if dtype != expected_dtype:
                    raise Qwen3NativeProtocolError(
                        "QWEN3_DTYPE_MISMATCH: worker dtype does not match profile"
                    )
                self._identity = Qwen3WorkerIdentity(device=device, dtype=dtype)
                return self._identity
            except BaseException:
                await self._abort_process()
                raise

    async def transcribe(
        self,
        audio: object,
        *,
        language: str,
        context: str,
    ) -> Qwen3WorkerResult:
        await self.start()
        pcm = self._audio_to_pcm(audio)
        async with self._io_lock:
            self._request_id += 1
            request_id = self._request_id
            try:
                await self._write(
                    {
                        "type": "transcribe",
                        "request_id": request_id,
                        "sample_rate": _SAMPLE_RATE,
                        "channels": 1,
                        "sample_width_bytes": _SAMPLE_WIDTH_BYTES,
                        "language": language,
                        "context": context,
                        "pcm_b64": base64.b64encode(pcm).decode("ascii"),
                    }
                )
                response = await self._read_frame()
            except BaseException:
                await self._abort_process()
                raise
        if response.get("type") == "error":
            code = response.get("code")
            safe_code = code if isinstance(code, str) else "QWEN3_WORKER_ERROR"
            raise Qwen3NativeProtocolError(safe_code)
        if response.get("type") != "result" or response.get("request_id") != request_id:
            raise Qwen3NativeProtocolError("QWEN3_WORKER_REQUEST_MISMATCH")
        if response.get("backend_id") != _BACKEND_ID:
            raise Qwen3NativeProtocolError("QWEN3_WORKER_BACKEND_MISMATCH")
        text = response.get("text")
        result_language = response.get("language")
        device = response.get("device")
        dtype = response.get("dtype")
        if not all(isinstance(value, str) for value in (text, result_language, device, dtype)):
            raise Qwen3NativeProtocolError("QWEN3_WORKER_RESULT_INVALID")
        if len(str(text)) > _MAX_TRANSCRIPT_CHARS:
            raise Qwen3NativeProtocolError("QWEN3_WORKER_TRANSCRIPT_TOO_LARGE")
        identity = self.identity
        if device != identity.device or dtype != identity.dtype:
            raise Qwen3NativeProtocolError("QWEN3_DEVICE_MISMATCH: result identity changed")
        return Qwen3WorkerResult(
            text=str(text).strip(),
            language=str(result_language),
            device=str(device),
            dtype=str(dtype),
        )

    @staticmethod
    def _audio_to_pcm(audio: object) -> bytes:
        if isinstance(audio, bytes):
            pcm = audio
        else:
            import numpy as np

            values = np.asarray(audio)
            if values.dtype == np.float32 or values.dtype == np.float64:
                clipped = np.clip(values, -1.0, 32767 / 32768)
                pcm = np.rint(clipped * 32768).astype("<i2").tobytes()
            elif values.dtype == np.int16:
                pcm = values.astype("<i2", copy=False).tobytes()
            else:
                raise TypeError("Qwen3 audio must be s16le bytes or float/int16 ndarray")
        if len(pcm) % _SAMPLE_WIDTH_BYTES:
            raise ValueError("QWEN3_PCM_INVALID: PCM 必须是偶数字节")
        if len(pcm) > _MAX_PCM_BYTES:
            raise ValueError("QWEN3_PCM_TOO_LARGE: audio exceeds worker limit")
        return pcm

    async def _write(self, payload: Mapping[str, object]) -> None:
        if self._process is None:
            raise RuntimeError("QWEN3_NOT_STARTED")
        self._process.stdin.write(_encode_frame(payload))
        try:
            await asyncio.wait_for(
                self._process.stdin.drain(),
                timeout=self.config.timeout_secs,
            )
        except TimeoutError as exc:
            raise TimeoutError("QWEN3_WORKER_TIMEOUT: stdin drain timed out") from exc

    async def _read_frame(self) -> dict[str, object]:
        if self._process is None:
            raise RuntimeError("QWEN3_NOT_STARTED")
        try:
            header = await asyncio.wait_for(
                self._process.stdout.readexactly(4),
                timeout=self.config.timeout_secs,
            )
            length = struct.unpack(">I", header)[0]
            if length < 1 or length > _MAX_FRAME_BYTES:
                raise Qwen3NativeProtocolError("QWEN3_WORKER_FRAME_SIZE")
            body = await asyncio.wait_for(
                self._process.stdout.readexactly(length),
                timeout=self.config.timeout_secs,
            )
        except TimeoutError as exc:
            raise TimeoutError("QWEN3_WORKER_TIMEOUT") from exc
        except asyncio.IncompleteReadError as exc:
            await asyncio.sleep(0)
            if self._stderr_tail:
                raise Qwen3NativeProtocolError(
                    "QWEN3_WORKER_STDERR: worker failed without exposing diagnostics"
                ) from exc
            raise Qwen3NativeProtocolError("QWEN3_WORKER_EOF: truncated frame") from exc
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Qwen3NativeProtocolError("QWEN3_WORKER_INVALID_JSON") from exc
        if not isinstance(decoded, dict):
            raise Qwen3NativeProtocolError("QWEN3_WORKER_INVALID_JSON")
        return {str(key): value for key, value in decoded.items()}

    async def _drain_stderr(self, process: _WorkerProcess) -> None:
        try:
            while chunk := await process.stderr.read(4096):
                self._stderr_tail.extend(chunk)
                if len(self._stderr_tail) > 32 * 1024:
                    del self._stderr_tail[: len(self._stderr_tail) - 32 * 1024]
        except (OSError, RuntimeError):
            return

    async def _abort_process(self) -> None:
        process = self._process
        stderr_task = self._stderr_task
        self._process = None
        self._identity = None
        self._stderr_task = None
        if process is not None:
            process.stdin.close()
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
        if stderr_task is not None:
            await asyncio.gather(stderr_task, return_exceptions=True)

    async def close(self) -> None:
        async with self._start_lock, self._io_lock:
            if self._closed:
                return
            self._closed = True
            await self._abort_process()


class Qwen3NativeOfflineAdapter:
    """把隔离 worker 的整段推理包装为统一转录事件。"""

    backend_id = _BACKEND_ID

    def __init__(
        self,
        *,
        worker: Qwen3NativeWorker,
        language: str,
        context: str,
        session_context: ASRSessionContext,
        owns_worker: bool = True,
        raw_event_sink: Qwen3NativeRawEventSink | None = None,
    ) -> None:
        normalized = language.strip()
        canonical = _LANGUAGES.get(normalized.lower()) or _LANGUAGES.get(normalized)
        if canonical is None:
            raise ValueError(f"unsupported Qwen3 language: {language!r}")
        self._worker = worker
        self._language = canonical
        self._prompt_context = context.strip()
        self._context = session_context
        self._owns_worker = owns_worker
        self._raw_event_sink = raw_event_sink
        self._pcm = bytearray()
        self._connected = False
        self._closed = False
        self._events_active = False
        self._event_queue: asyncio.Queue[ASREvent | None] = asyncio.Queue()
        self._finish_task: asyncio.Task[TranscriptWindow] | None = None
        self.capabilities = ASRCapabilities(
            languages=frozenset(_LANGUAGES) | frozenset(_LANGUAGES.values()),
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
        return "offline://qwen3-asr-native"

    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("QWEN3_CLOSED: adapter is closed")
        if self._connected:
            return
        identity = await self._worker.start()
        self._connected = True
        self._event_queue.put_nowait(
            ASREvent(
                kind="ready",
                metadata={
                    "backend_id": self.backend_id,
                    "device": identity.device,
                    "dtype": identity.dtype,
                },
            )
        )

    async def send_audio(self, chunk: bytes) -> None:
        if self._closed:
            raise RuntimeError("QWEN3_CLOSED: adapter is closed")
        if not self._connected:
            raise RuntimeError("QWEN3_NOT_CONNECTED")
        if self._finish_task is not None:
            raise RuntimeError("QWEN3_FINISHED")
        if not isinstance(chunk, bytes):
            raise TypeError("Qwen3 PCM chunk 必须是 bytes")
        if len(chunk) % _SAMPLE_WIDTH_BYTES:
            raise ValueError("QWEN3_PCM_INVALID: PCM 必须是偶数字节")
        if len(self._pcm) + len(chunk) > _MAX_PCM_BYTES:
            raise ValueError("QWEN3_PCM_TOO_LARGE: audio exceeds worker limit")
        self._pcm.extend(chunk)

    def events(self) -> AsyncIterator[ASREvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[ASREvent]:
        if not self._connected:
            raise RuntimeError("QWEN3_NOT_CONNECTED")
        if self._events_active:
            raise RuntimeError("QWEN3_EVENTS_ALREADY_CONSUMED")
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
            raise RuntimeError("QWEN3_CLOSED: adapter is closed")
        if not self._connected:
            raise RuntimeError("QWEN3_NOT_CONNECTED")
        if self._finish_task is None:
            self._finish_task = asyncio.create_task(self._finish_once())
        return await asyncio.shield(self._finish_task)

    async def _finish_once(self) -> TranscriptWindow:
        try:
            if not self._pcm:
                window = TranscriptWindow(source_epoch=self._context.source_epoch)
            else:
                result = await self._worker.transcribe(
                    bytes(self._pcm),
                    language=self._language,
                    context=self._prompt_context,
                )
                self._audit(result)
                window = self._build_window(
                    result.text,
                    len(self._pcm) // _SAMPLE_WIDTH_BYTES,
                    result.language,
                )
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
                    error_code="QWEN3_ENGINE_ERROR",
                    error_message="Qwen3-ASR offline inference failed",
                )
            )
            raise RuntimeError("QWEN3_ENGINE_ERROR: offline inference failed") from exc

    def _audit(self, result: Qwen3WorkerResult) -> None:
        if self._raw_event_sink is None:
            return
        text = result.text[:4_096]
        payload: dict[str, object] = {
            "event": "inference",
            "language": result.language,
            "device": result.device,
            "dtype": result.dtype,
            "text": text,
            "text_truncated": len(result.text) > len(text),
        }
        try:
            self._raw_event_sink(payload)
        except Exception:
            return

    def _build_window(
        self,
        text: str,
        sample_count: int,
        detected_language: str,
    ) -> TranscriptWindow:
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
            detected_language=detected_language,
        )
        return TranscriptWindow(source_epoch=self._context.source_epoch, segments=(segment,))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected = False
        self._pcm.clear()
        self._event_queue.put_nowait(None)
        if self._owns_worker:
            await self._worker.close()
