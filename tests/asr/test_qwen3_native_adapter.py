"""Qwen3-ASR 原生离线 adapter/隔离 worker 的 RED 协议测试。

这些测试只使用 fake process，不要求主项目环境安装 ``qwen-asr``，也不会
加载模型。worker 的唯一职责是通过一个长度前缀 JSON 帧协议，把主项目的
内存 PCM 请求转发到 WhisperLiveKit 独立 Python 环境；adapter 再把 worker
结果归一化为项目的 ``StreamingTranscriber`` 领域事件。
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import sys
from collections.abc import AsyncIterator, Iterable, Mapping
from pathlib import Path

import numpy as np
import pytest

import voice_realtime.asr.adapters.qwen3_native as qwen3_native_module
from voice_realtime.asr.adapters.qwen3_native import (
    Qwen3NativeOfflineAdapter,
    Qwen3NativeProtocolError,
    Qwen3NativeWorker,
    Qwen3NativeWorkerConfig,
    build_qwen3_worker_command,
)
from voice_realtime.asr.contracts import ASREvent, ASRSessionContext

_BACKEND_ID = "qwen3-asr-native"
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


def _frame(payload: Mapping[str, object]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def _frames(payload: bytes) -> list[dict[str, object]]:
    """解析 fake stdin，确保测试确实验证了长度前缀协议。"""
    result: list[dict[str, object]] = []
    offset = 0
    while offset < len(payload):
        assert len(payload) - offset >= 4
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        offset += 4
        assert length > 0
        assert len(payload) - offset >= length
        body = payload[offset : offset + length]
        offset += length
        value = json.loads(body.decode("utf-8"))
        assert isinstance(value, dict)
        result.append(value)
    return result


class FakePipe:
    """最小异步 stdin/stdout/stderr fake；不创建真实子进程。"""

    def __init__(self, chunks: Iterable[bytes] = ()) -> None:
        self._buffer = bytearray(b"".join(chunks))
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, payload: bytes) -> None:
        self.writes.append(bytes(payload))

    async def drain(self) -> None:
        return None

    async def readexactly(self, size: int) -> bytes:
        if len(self._buffer) < size:
            partial = bytes(self._buffer)
            self._buffer.clear()
            raise asyncio.IncompleteReadError(partial=partial, expected=size)
        payload = bytes(self._buffer[:size])
        del self._buffer[:size]
        return payload

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            payload = bytes(self._buffer)
            self._buffer.clear()
            return payload
        payload = bytes(self._buffer[:size])
        del self._buffer[:size]
        return payload

    def close(self) -> None:
        self.closed = True


class HangingPipe(FakePipe):
    """让 worker 的超时路径可确定地触发。"""

    async def readexactly(self, _size: int) -> bytes:
        await asyncio.Future()
        raise AssertionError("unreachable")


class FakeProcess:
    def __init__(
        self,
        responses: Iterable[bytes] = (),
        *,
        stderr: bytes = b"",
        hang_stdout: bool = False,
    ) -> None:
        pipe_type: type[FakePipe] = HangingPipe if hang_stdout else FakePipe
        self.stdin = FakePipe()
        self.stdout = pipe_type(responses)
        self.stderr = FakePipe((stderr,))
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self.pid = 4242

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class FakeProcessFactory:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> FakeProcess:
        self.calls.append((tuple(command), dict(kwargs)))
        return self.process


def _ready(*, device: str = "mps", dtype: str = "float16") -> bytes:
    return _frame(
        {
            "type": "ready",
            "backend_id": _BACKEND_ID,
            "device": device,
            "dtype": dtype,
            "model_loaded": True,
        }
    )


def _result(
    *,
    text: str = "你好",
    language: str = "Chinese",
    request_id: int = 1,
    device: str = "mps",
    dtype: str = "float16",
) -> bytes:
    return _frame(
        {
            "type": "result",
            "request_id": request_id,
            "backend_id": _BACKEND_ID,
            "device": device,
            "dtype": dtype,
            "language": language,
            "text": text,
        }
    )


def _context(*, offset_ms: int = 250) -> ASRSessionContext:
    return ASRSessionContext(source_epoch=7, offset_ms=offset_ms, purpose="subtitles")


def _snapshot(tmp_path: Path) -> tuple[Path, Path, Path]:
    """创建外部、不可加载的假 snapshot 和隔离解释器路径。"""
    repo_root = tmp_path / "repo"
    model_dir = tmp_path / "external-model-cache" / "Qwen3-ASR-1.7B"
    python = tmp_path / "whisperlivekit" / ".venv" / "bin" / "python"
    repo_root.mkdir()
    model_dir.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fake isolated interpreter")
    python.chmod(0o755)
    for name in _MODEL_FILES:
        (model_dir / name).write_bytes(b"not loaded by unit tests")
    return repo_root, model_dir, python


def test_worker_rejects_non_executable_python(tmp_path: Path) -> None:
    repo_root, model_dir, python = _snapshot(tmp_path)
    python.chmod(0o644)

    with pytest.raises(ValueError, match="executable"):
        Qwen3NativeWorkerConfig(
            repo_root=repo_root,
            python_executable=python,
            model_dir=model_dir,
            device="mps",
        )


def test_worker_preserves_virtualenv_python_symlink(tmp_path: Path) -> None:
    repo_root, model_dir, python = _snapshot(tmp_path)
    base_python = tmp_path / "base-python"
    base_python.write_bytes(b"base interpreter")
    base_python.chmod(0o755)
    python.unlink()
    python.symlink_to(base_python)

    config = Qwen3NativeWorkerConfig(
        repo_root=repo_root,
        python_executable=python,
        model_dir=model_dir,
        device="mps",
    )

    assert config.python_executable == python.absolute()
    assert config.python_executable != base_python.resolve()
    assert build_qwen3_worker_command(config)[0] == str(python.absolute())


def _worker(
    tmp_path: Path,
    process: FakeProcess,
    *,
    device: str = "mps",
    timeout_secs: float = 0.05,
) -> tuple[Qwen3NativeWorker, FakeProcessFactory, Qwen3NativeWorkerConfig]:
    repo_root, model_dir, python = _snapshot(tmp_path)
    config = Qwen3NativeWorkerConfig(
        repo_root=repo_root,
        python_executable=python,
        model_dir=model_dir,
        device=device,
        worker_module="whisperlivekit.qwen3_native_worker",
        timeout_secs=timeout_secs,
    )
    factory = FakeProcessFactory(process)
    return Qwen3NativeWorker(config, process_factory=factory), factory, config


def _adapter(
    worker: Qwen3NativeWorker,
    *,
    language: str = "zh",
    context: str = "术语：量子之声、Qwen3-ASR",
    session_context: ASRSessionContext | None = None,
) -> Qwen3NativeOfflineAdapter:
    return Qwen3NativeOfflineAdapter(
        worker=worker,
        language=language,
        context=context,
        session_context=session_context or _context(),
    )


@pytest.mark.asyncio
async def test_adapter_exposes_started_worker_pid_for_resource_sampling(
    tmp_path: Path,
) -> None:
    worker, _, _ = _worker(tmp_path, FakeProcess((_ready(),)))
    adapter = _adapter(worker)

    assert adapter.resource_process_ids == ()
    await adapter.connect()
    assert adapter.resource_process_ids == (4242,)

    await adapter.close()


async def _next_event(events: AsyncIterator[ASREvent]) -> ASREvent:
    return await anext(events)


def test_worker_command_uses_isolated_interpreter_and_external_snapshot(
    tmp_path: Path,
) -> None:
    repo_root, model_dir, python = _snapshot(tmp_path)
    config = Qwen3NativeWorkerConfig(
        repo_root=repo_root,
        python_executable=python,
        model_dir=model_dir,
        device="mps",
        worker_module="whisperlivekit.qwen3_native_worker",
        timeout_secs=1.0,
    )

    command = build_qwen3_worker_command(config)

    assert command[0] == str(python)
    assert Path(command[0]).is_absolute()
    assert command[0] != sys.executable
    assert command[command.index("-m") + 1] == config.worker_module
    assert command[command.index("--model-dir") + 1] == str(model_dir)
    assert command[command.index("--device") + 1] == "mps"
    assert command[command.index("--max-new-tokens") + 1] == "512"
    assert "Qwen/Qwen3-ASR-1.7B" not in command
    assert not any(value.startswith(("http://", "https://")) for value in command)
    assert model_dir.is_absolute()
    assert not model_dir.is_relative_to(repo_root)


@pytest.mark.parametrize("model_ref", ["Qwen/Qwen3-ASR-1.7B", "https://example.invalid/qwen"])
def test_worker_rejects_repo_id_or_url_model_reference(tmp_path: Path, model_ref: str) -> None:
    repo_root, _model_dir, python = _snapshot(tmp_path)

    with pytest.raises(ValueError, match=r"local|absolute|snapshot"):
        Qwen3NativeWorkerConfig(
            repo_root=repo_root,
            python_executable=python,
            model_dir=model_ref,
            device="mps",
            worker_module="whisperlivekit.qwen3_native_worker",
            timeout_secs=1.0,
        )


def test_worker_rejects_snapshot_inside_repository(tmp_path: Path) -> None:
    repo_root, _model_dir, python = _snapshot(tmp_path)
    inside = repo_root / "runtime" / "qwen3-asr-1.7b"
    inside.mkdir(parents=True)

    with pytest.raises(ValueError, match=r"outside|project|repository"):
        Qwen3NativeWorkerConfig(
            repo_root=repo_root,
            python_executable=python,
            model_dir=inside,
            device="mps",
            worker_module="whisperlivekit.qwen3_native_worker",
            timeout_secs=1.0,
        )


@pytest.mark.asyncio
async def test_adapter_buffers_16k_s16le_in_memory_and_forwards_context_and_language(
    tmp_path: Path,
) -> None:
    pcm = np.asarray([-32768, -1, 0, 1, 32767], dtype=np.int16).tobytes()
    process = FakeProcess([_ready(), _result(text="内存输入")])
    worker, factory, _config = _worker(tmp_path, process)
    adapter = _adapter(worker, language="zh")

    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    await adapter.send_audio(pcm[:4])
    await adapter.send_audio(pcm[4:])
    final = await adapter.finish()

    sent_frames = _frames(b"".join(process.stdin.writes))
    transcribe = next(frame for frame in sent_frames if frame["type"] == "transcribe")
    assert transcribe["sample_rate"] == 16_000
    assert transcribe["channels"] == 1
    assert transcribe["sample_width_bytes"] == 2
    assert transcribe["language"] == "Chinese"
    assert transcribe["context"] == "术语：量子之声、Qwen3-ASR"
    assert base64.b64decode(str(transcribe["pcm_b64"])) == pcm
    assert factory.calls
    process_env = factory.calls[0][1]["env"]
    assert isinstance(process_env, dict)
    assert process_env["HF_HUB_OFFLINE"] == "1"
    assert process_env["TRANSFORMERS_OFFLINE"] == "1"
    assert process_env["PYTORCH_ENABLE_MPS_FALLBACK"] == "0"
    assert process_env["PYTHONPATH"] == str(_config.repo_root / "src")
    assert not await asyncio.to_thread(lambda: list(tmp_path.rglob("*.wav")))
    assert final.segments[0].text == "内存输入"


@pytest.mark.parametrize(
    ("input_language", "expected_language"),
    [("zh", "Chinese"), ("en", "English"), ("auto", "auto")],
)
@pytest.mark.asyncio
async def test_language_codes_are_mapped_to_qwen_canonical_names(
    tmp_path: Path,
    input_language: str,
    expected_language: str,
) -> None:
    process = FakeProcess([_ready(), _result(language=expected_language)])
    worker, _factory, _config = _worker(tmp_path, process)
    adapter = _adapter(worker, language=input_language, context="")

    await adapter.connect()
    await adapter.send_audio(b"\x00\x00")
    await adapter.finish()

    frames = _frames(b"".join(process.stdin.writes))
    request = next(frame for frame in frames if frame["type"] == "transcribe")
    assert request["language"] == expected_language


@pytest.mark.asyncio
async def test_one_worker_process_is_reused_and_model_loads_once_per_run(tmp_path: Path) -> None:
    process = FakeProcess(
        [
            _ready(),
            _result(text="第一句", request_id=1),
            _result(text="第二句", request_id=2),
        ]
    )
    worker, factory, _config = _worker(tmp_path, process)

    await worker.start()
    await worker.transcribe(np.zeros(2, dtype=np.float32), language="Chinese", context="")
    await worker.transcribe(np.zeros(2, dtype=np.float32), language="Chinese", context="")

    assert len(factory.calls) == 1
    frames = _frames(b"".join(process.stdin.writes))
    assert [frame["type"] for frame in frames].count("start") == 1
    assert [frame["type"] for frame in frames].count("transcribe") == 2


@pytest.mark.asyncio
async def test_concurrent_start_creates_only_one_worker_process(tmp_path: Path) -> None:
    process = FakeProcess([_ready()])
    worker, factory, _config = _worker(tmp_path, process)

    first, second = await asyncio.gather(worker.start(), worker.start())

    assert first == second
    assert len(factory.calls) == 1
    frames = _frames(b"".join(process.stdin.writes))
    assert [frame["type"] for frame in frames].count("start") == 1


@pytest.mark.asyncio
async def test_request_and_response_frames_validate_length_prefix_and_request_id(
    tmp_path: Path,
) -> None:
    process = FakeProcess([_ready(), _result(request_id=1)])
    worker, _factory, _config = _worker(tmp_path, process)

    await worker.start()
    result = await worker.transcribe(
        np.zeros(2, dtype=np.float32),
        language="Chinese",
        context="",
    )

    assert result.text == "你好"
    frames = _frames(b"".join(process.stdin.writes))
    request = next(frame for frame in frames if frame["type"] == "transcribe")
    assert request["request_id"] == 1
    assert request["sample_rate"] == 16_000


@pytest.mark.asyncio
async def test_truncated_worker_frame_is_a_stable_protocol_error(tmp_path: Path) -> None:
    process = FakeProcess([struct.pack(">I", 100) + b'{"type":"ready"}'])
    worker, _factory, _config = _worker(tmp_path, process)

    with pytest.raises(Qwen3NativeProtocolError, match=r"QWEN3_WORKER_EOF|truncated"):
        await worker.start()


@pytest.mark.asyncio
async def test_invalid_json_worker_frame_is_a_stable_protocol_error(tmp_path: Path) -> None:
    process = FakeProcess([struct.pack(">I", 9) + b"not-json!"])
    worker, _factory, _config = _worker(tmp_path, process)

    with pytest.raises(Qwen3NativeProtocolError, match="QWEN3_WORKER_INVALID_JSON"):
        await worker.start()


@pytest.mark.asyncio
async def test_failed_start_terminates_spawned_worker(tmp_path: Path) -> None:
    process = FakeProcess([struct.pack(">I", 9) + b"not-json!"])
    worker, _factory, _config = _worker(tmp_path, process)

    with pytest.raises(Qwen3NativeProtocolError, match="QWEN3_WORKER_INVALID_JSON"):
        await worker.start()

    assert process.terminated or process.killed
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_worker_stderr_is_mapped_without_exposing_traceback(tmp_path: Path) -> None:
    process = FakeProcess(
        [],
        stderr=b"Traceback (most recent call last):\nsecret model path\n",
    )
    worker, _factory, _config = _worker(tmp_path, process)

    with pytest.raises(Qwen3NativeProtocolError, match="QWEN3_WORKER_STDERR") as error:
        await worker.start()
    assert "Traceback" not in str(error.value)
    assert "secret model path" not in str(error.value)


@pytest.mark.asyncio
async def test_worker_timeout_is_mapped_to_stable_error(tmp_path: Path) -> None:
    process = FakeProcess([_ready()], hang_stdout=True)
    worker, _factory, _config = _worker(tmp_path, process, timeout_secs=0.001)

    with pytest.raises(TimeoutError, match="QWEN3_WORKER_TIMEOUT"):
        await worker.start()


@pytest.mark.asyncio
async def test_ready_then_final_event_carries_worker_mps_identity(tmp_path: Path) -> None:
    process = FakeProcess([_ready(), _result(text="最终句")])
    worker, _factory, _config = _worker(tmp_path, process)
    adapter = _adapter(worker, session_context=_context(offset_ms=1_000))

    await adapter.connect()
    events = adapter.events()
    ready = await _next_event(events)
    assert ready.kind == "ready"
    assert ready.metadata["device"] == "mps"
    assert ready.metadata["dtype"] == "float16"

    await adapter.send_audio(np.zeros(16_000, dtype=np.int16).tobytes())
    final = await adapter.finish()
    final_event = await _next_event(events)

    assert final_event.kind == "final"
    assert final_event.window == final
    assert final.segments[0].text == "最终句"
    assert final.segments[0].start_ms == 1_000
    assert final.segments[0].end_ms == 2_000


@pytest.mark.asyncio
async def test_mps_request_rejects_worker_cpu_fallback(tmp_path: Path) -> None:
    process = FakeProcess([_ready(device="cpu", dtype="float32")])
    worker, _factory, _config = _worker(tmp_path, process, device="mps")

    with pytest.raises(Qwen3NativeProtocolError, match=r"QWEN3_DEVICE_MISMATCH|MPS"):
        await worker.start()


@pytest.mark.asyncio
async def test_mps_request_rejects_worker_dtype_mismatch(tmp_path: Path) -> None:
    process = FakeProcess([_ready(device="mps", dtype="float32")])
    worker, _factory, _config = _worker(tmp_path, process, device="mps")

    with pytest.raises(Qwen3NativeProtocolError, match="QWEN3_DTYPE_MISMATCH"):
        await worker.start()


@pytest.mark.asyncio
async def test_finish_is_idempotent_and_worker_receives_one_request(tmp_path: Path) -> None:
    process = FakeProcess([_ready(), _result(text="只推理一次")])
    worker, _factory, _config = _worker(tmp_path, process)
    adapter = _adapter(worker)

    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    await adapter.send_audio(b"\x00\x00")
    first, second = await asyncio.gather(adapter.finish(), adapter.finish())

    assert first == second
    frames = _frames(b"".join(process.stdin.writes))
    assert [frame["type"] for frame in frames].count("transcribe") == 1


@pytest.mark.asyncio
async def test_worker_failure_emits_redacted_stable_error_event(tmp_path: Path) -> None:
    process = FakeProcess(
        [
            _ready(),
            _frame(
                {
                    "type": "error",
                    "code": "QWEN3_WORKER_INFERENCE_ERROR",
                    "request_id": 1,
                }
            ),
        ]
    )
    worker, _factory, _config = _worker(tmp_path, process)
    adapter = _adapter(worker)
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    await adapter.send_audio(b"\x00\x00")

    with pytest.raises(RuntimeError, match="QWEN3_ENGINE_ERROR"):
        await adapter.finish()
    error = await _next_event(events)

    assert error.kind == "error"
    assert error.error_code == "QWEN3_ENGINE_ERROR"
    assert "Traceback" not in (error.error_message or "")


@pytest.mark.asyncio
async def test_empty_pcm_finishes_empty_without_invoking_model(tmp_path: Path) -> None:
    process = FakeProcess([_ready()])
    worker, _factory, _config = _worker(tmp_path, process)
    adapter = _adapter(worker)

    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    final = await adapter.finish()
    final_event = await _next_event(events)

    assert final_event.kind == "final"
    assert final.segments == ()
    frames = _frames(b"".join(process.stdin.writes))
    assert not any(frame["type"] == "transcribe" for frame in frames)


@pytest.mark.asyncio
async def test_adapter_rejects_pcm_over_limit_before_buffer_growth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(qwen3_native_module, "_MAX_PCM_BYTES", 4)
    process = FakeProcess([_ready()])
    worker, _factory, _config = _worker(tmp_path, process)
    adapter = _adapter(worker)
    await adapter.connect()

    with pytest.raises(ValueError, match="TOO_LARGE"):
        await adapter.send_audio(b"\x00\x00\x00\x00\x00\x00")

    assert adapter._pcm == bytearray()


@pytest.mark.asyncio
async def test_final_segment_uses_worker_detected_language(tmp_path: Path) -> None:
    process = FakeProcess([_ready(), _result(text="hello", language="English")])
    worker, _factory, _config = _worker(tmp_path, process)
    adapter = _adapter(worker, language="zh")
    await adapter.connect()
    await adapter.send_audio(b"\x00\x00")

    window = await adapter.finish()

    assert window.segments[0].detected_language == "English"


@pytest.mark.asyncio
async def test_close_is_idempotent_and_terminates_child_process(tmp_path: Path) -> None:
    process = FakeProcess([_ready()])
    worker, _factory, _config = _worker(tmp_path, process)
    adapter = _adapter(worker)

    await adapter.connect()
    await adapter.close()
    await adapter.close()

    assert process.terminated or process.killed
    assert process.wait_calls == 1
    with pytest.raises(RuntimeError, match="QWEN3_CLOSED"):
        await adapter.finish()
