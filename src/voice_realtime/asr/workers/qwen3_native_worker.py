"""在 WhisperLiveKit 隔离环境中运行 Qwen3-ASR 原生整段推理。"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import struct
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

_BACKEND_ID = "qwen3-asr-native"
_MAX_FRAME_BYTES = 64 * 1024 * 1024
_MAX_PCM_BYTES = 40 * 1024 * 1024
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


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    device: str
    dtype: str


class WorkerEngine(Protocol):
    identity: WorkerIdentity

    def transcribe(
        self,
        audio: bytes,
        *,
        language: str,
        context: str,
    ) -> tuple[str, str]: ...


EngineFactory = Callable[[Path, str], WorkerEngine]


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise EOFError("truncated worker frame")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_frame(stream: BinaryIO) -> dict[str, object] | None:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise EOFError("truncated worker header")
    length = struct.unpack(">I", header)[0]
    if length < 1 or length > _MAX_FRAME_BYTES:
        raise ValueError("invalid worker frame size")
    body = _read_exact(stream, length)
    decoded = json.loads(body.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("worker frame must be an object")
    return {str(key): value for key, value in decoded.items()}


def _write_frame(stream: BinaryIO, payload: Mapping[str, object]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not body or len(body) > _MAX_FRAME_BYTES:
        raise ValueError("invalid worker response size")
    stream.write(struct.pack(">I", len(body)))
    stream.write(body)
    stream.flush()


def _valid_request_id(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _decode_transcribe_request(frame: Mapping[str, object]) -> tuple[int, bytes, str, str]:
    request_id = _valid_request_id(frame.get("request_id"))
    if request_id is None:
        raise ValueError("invalid request_id")
    if (
        frame.get("sample_rate") != 16_000
        or frame.get("channels") != 1
        or frame.get("sample_width_bytes") != 2
    ):
        raise ValueError("invalid PCM format")
    language = frame.get("language")
    context = frame.get("context")
    encoded = frame.get("pcm_b64")
    if not isinstance(language, str) or not language or not isinstance(context, str):
        raise ValueError("invalid language/context")
    if not isinstance(encoded, str):
        raise ValueError("invalid PCM payload")
    try:
        pcm = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid PCM payload") from exc
    if not pcm or len(pcm) % 2 or len(pcm) > _MAX_PCM_BYTES:
        raise ValueError("invalid PCM length")
    return request_id, pcm, language, context


def serve(
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    *,
    model_dir: Path,
    device: str,
    engine_factory: EngineFactory,
) -> None:
    """读取一个 start 后复用同一模型顺序处理全部 transcribe 请求。"""
    start = _read_frame(input_stream)
    if (
        start is None
        or start.get("type") != "start"
        or start.get("backend_id") != _BACKEND_ID
        or start.get("model_dir") != str(model_dir)
        or start.get("device") != device
    ):
        _write_frame(output_stream, {"type": "error", "code": "QWEN3_WORKER_INVALID_START"})
        return
    try:
        engine = engine_factory(model_dir, device)
    except Exception:
        _write_frame(output_stream, {"type": "error", "code": "QWEN3_WORKER_LOAD_ERROR"})
        return
    identity = engine.identity
    expected_dtype = "float16" if device == "mps" else "float32"
    if identity.device != device or identity.dtype != expected_dtype:
        _write_frame(
            output_stream,
            {"type": "error", "code": "QWEN3_WORKER_IDENTITY_MISMATCH"},
        )
        return
    _write_frame(
        output_stream,
        {
            "type": "ready",
            "backend_id": _BACKEND_ID,
            "device": identity.device,
            "dtype": identity.dtype,
            "model_loaded": True,
        },
    )
    while True:
        try:
            frame = _read_frame(input_stream)
        except Exception:
            _write_frame(output_stream, {"type": "error", "code": "QWEN3_WORKER_INVALID_FRAME"})
            return
        if frame is None:
            return
        request_id = _valid_request_id(frame.get("request_id"))
        if frame.get("type") != "transcribe":
            _write_frame(
                output_stream,
                {
                    "type": "error",
                    "code": "QWEN3_WORKER_INVALID_REQUEST",
                    "request_id": request_id,
                },
            )
            continue
        try:
            request_id, pcm, language, context = _decode_transcribe_request(frame)
        except Exception:
            _write_frame(
                output_stream,
                {
                    "type": "error",
                    "code": "QWEN3_WORKER_INVALID_REQUEST",
                    "request_id": request_id,
                },
            )
            continue
        try:
            text, detected_language = engine.transcribe(
                pcm,
                language=language,
                context=context,
            )
        except Exception:
            _write_frame(
                output_stream,
                {
                    "type": "error",
                    "code": "QWEN3_WORKER_INFERENCE_ERROR",
                    "request_id": request_id,
                },
            )
            continue
        _write_frame(
            output_stream,
            {
                "type": "result",
                "request_id": request_id,
                "backend_id": _BACKEND_ID,
                "device": identity.device,
                "dtype": identity.dtype,
                "language": detected_language,
                "text": text,
            },
        )


class Qwen3ASREngine:
    """仅在隔离环境导入 qwen-asr/Transformers 的真实 engine。"""

    def __init__(
        self,
        model_dir: Path,
        device: Literal["mps", "cpu"],
        *,
        max_new_tokens: int = 512,
    ) -> None:
        if not model_dir.is_absolute() or not model_dir.is_dir():
            raise ValueError("model_dir must be an absolute local directory")
        if any(not (model_dir / name).is_file() for name in _MODEL_FILES):
            raise ValueError("model snapshot is incomplete")
        import torch
        from qwen_asr import Qwen3ASRModel  # type: ignore[import-not-found]

        if device == "mps":
            if not torch.backends.mps.is_available() or not torch.backends.mps.is_built():
                raise RuntimeError("MPS unavailable")
            dtype = torch.float16
        elif device == "cpu":
            dtype = torch.float32
        else:
            raise ValueError("device must be mps or cpu")
        model = Qwen3ASRModel.from_pretrained(
            str(model_dir),
            dtype=dtype,
            max_inference_batch_size=1,
            max_new_tokens=max_new_tokens,
            local_files_only=True,
        )
        model.model.to(torch.device(device), dtype=dtype).eval()
        parameters = tuple(model.model.parameters())
        if not parameters or any(parameter.device.type != device for parameter in parameters):
            raise RuntimeError("model device mismatch")
        self._model = model
        self.identity = WorkerIdentity(
            device=device,
            dtype=str(parameters[0].dtype).removeprefix("torch."),
        )

    def transcribe(
        self,
        audio: bytes,
        *,
        language: str,
        context: str,
    ) -> tuple[str, str]:
        import numpy as np

        waveform = np.frombuffer(audio, dtype="<i2").astype(np.float32)
        waveform /= np.float32(32768.0)
        results = self._model.transcribe(
            audio=(waveform, 16_000),
            context=context,
            language=language,
            return_time_stamps=False,
        )
        if not results:
            return "", language
        first = results[0]
        text = getattr(first, "text", None)
        detected_language = getattr(first, "language", None)
        if not isinstance(text, str) or not isinstance(detected_language, str):
            raise RuntimeError("invalid qwen-asr result")
        return text.strip(), detected_language


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qwen3-ASR isolated benchmark worker")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--device", choices=("mps", "cpu"), required=True)
    parser.add_argument("--max-new-tokens", type=int, default=512)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "0"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    model_dir = Path(str(args.model_dir)).expanduser().resolve(strict=True)
    protocol_fd = os.dup(sys.stdout.fileno())
    protocol_output = os.fdopen(protocol_fd, "wb", buffering=0)
    sys.stdout = sys.stderr
    try:
        serve(
            sys.stdin.buffer,
            protocol_output,
            model_dir=model_dir,
            device=str(args.device),
            engine_factory=lambda path, device: Qwen3ASREngine(
                path,
                device=device,  # type: ignore[arg-type]
                max_new_tokens=int(args.max_new_tokens),
            ),
        )
    finally:
        protocol_output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
