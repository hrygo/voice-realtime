"""物理输出 Helper client、supervisor 与 AudioSource 适配测试。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID

import pytest

from voice_realtime.audio.frame import AudioFrameFlag
from voice_realtime.audio.ipc import (
    ControlMessage,
    PCMMessage,
    WireDecoder,
    encode_control_message,
    encode_pcm_message,
)
from voice_realtime.audio.output_source import (
    AudioCaptureClient,
    AudioCaptureError,
    HelperSupervisor,
    PhysicalOutputSource,
    _validated_executable,
)
from voice_realtime.audio.source import AudioSourceState
from voice_realtime.config import AudioCaptureSettings

CAPTURE_ID = UUID("00000000-0000-0000-0000-000000000001")
SOURCE_ID = UUID("00000000-0000-0000-0000-000000000002")
TOKEN = "a" * 64


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="vrac-", dir="/tmp") as directory:
        yield Path(directory)


def _pcm(sequence: int, *, capture_id: UUID = CAPTURE_ID) -> PCMMessage:
    return PCMMessage(
        capture_id=capture_id,
        source_id=SOURCE_ID,
        device_generation=3,
        sequence=sequence,
        host_time_ns=sequence + 1,
        sample_rate=16_000,
        samples_per_channel=512,
        channels=1,
        sample_width=2,
        flags=AudioFrameFlag.NONE,
        pcm=sequence.to_bytes(2, "little", signed=True) * 512,
    )


async def _read_message(
    reader: asyncio.StreamReader,
    decoder: WireDecoder,
) -> ControlMessage | PCMMessage:
    while True:
        data = await reader.read(65_536)
        if not data:
            raise EOFError("client disconnected")
        messages = decoder.feed(data)
        if messages:
            return messages[0]


async def _ack_hello(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    observed: list[dict[str, object]],
) -> WireDecoder:
    decoder = WireDecoder()
    message = await _read_message(reader, decoder)
    assert isinstance(message, ControlMessage)
    observed.append(message.payload)
    writer.write(
        encode_control_message(
            {
                "type": "hello_ack",
                "request_id": message.payload["request_id"],
                "helper_version": "test-helper",
                "protocol_major": 1,
                "protocol_minor": 0,
                "capabilities": ["device_scoped_tap"],
            }
        )
    )
    await writer.drain()
    return decoder


async def _serve(
    socket_path: Path,
    handler,
) -> asyncio.AbstractServer:
    server = await asyncio.start_unix_server(handler, path=str(socket_path))
    await asyncio.to_thread(socket_path.chmod, 0o600)
    return server


async def test_client_authenticates_and_correlates_control_request(
    socket_dir: Path,
) -> None:
    socket_path = socket_dir / "capture.sock"
    observed: list[dict[str, object]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = await _ack_hello(reader, writer, observed)
        message = await _read_message(reader, decoder)
        assert isinstance(message, ControlMessage)
        observed.append(message.payload)
        writer.write(
            encode_control_message(
                {
                    "type": "devices",
                    "request_id": message.payload["request_id"],
                    "devices": [],
                }
            )
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await _serve(socket_path, handler)
    client = AudioCaptureClient(socket_path, TOKEN, command_timeout_secs=1.0)
    try:
        await client.connect()
        response = await client.request(
            {"type": "list_devices"},
            expected_type="devices",
        )
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert observed[0]["type"] == "hello"
    assert observed[0]["capture_token"] == TOKEN
    assert observed[0]["client_pid"] == os.getpid()
    assert observed[1]["type"] == "list_devices"
    assert response["devices"] == []


async def test_client_rejects_socket_with_group_access(socket_dir: Path) -> None:
    socket_path = socket_dir / "capture.sock"

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    server = await _serve(socket_path, handler)
    socket_path.chmod(0o660)
    client = AudioCaptureClient(socket_path, TOKEN)
    try:
        with pytest.raises(AudioCaptureError) as exc_info:
            await client.connect()
    finally:
        server.close()
        await server.wait_closed()

    assert exc_info.value.code == "insecure_socket"


async def test_client_maps_safe_helper_error(socket_dir: Path) -> None:
    socket_path = socket_dir / "capture.sock"
    observed: list[dict[str, object]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = await _ack_hello(reader, writer, observed)
        message = await _read_message(reader, decoder)
        assert isinstance(message, ControlMessage)
        writer.write(
            encode_control_message(
                {
                    "type": "error",
                    "request_id": message.payload["request_id"],
                    "code": "permission_denied",
                    "message": "系统音频权限未授予",
                    "retryable": False,
                }
            )
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await _serve(socket_path, handler)
    client = AudioCaptureClient(socket_path, TOKEN, command_timeout_secs=1.0)
    try:
        await client.connect()
        with pytest.raises(AudioCaptureError) as exc_info:
            await client.request({"type": "prepare_capture"}, expected_type="ready")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert exc_info.value.code == "permission_denied"
    assert exc_info.value.retryable is False
    assert "permission_denied" not in str(exc_info.value)


async def test_client_pcm_queue_drops_oldest(socket_dir: Path) -> None:
    socket_path = socket_dir / "capture.sock"
    sent = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _ack_hello(reader, writer, [])
        writer.write(encode_pcm_message(_pcm(1)))
        writer.write(encode_pcm_message(_pcm(2)))
        await writer.drain()
        sent.set()
        await reader.read()
        writer.close()
        await writer.wait_closed()

    server = await _serve(socket_path, handler)
    client = AudioCaptureClient(socket_path, TOKEN, queue_size=1)
    try:
        await client.connect()
        await asyncio.wait_for(sent.wait(), timeout=1.0)
        await asyncio.sleep(0)
        async with asyncio.timeout(1.0):
            message = await anext(client.pcm_messages())
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert message.sequence == 2
    assert client.dropped_frames == 1


async def test_client_reports_unexpected_disconnect(socket_dir: Path) -> None:
    socket_path = socket_dir / "capture.sock"

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _ack_hello(reader, writer, [])
        writer.close()
        await writer.wait_closed()

    server = await _serve(socket_path, handler)
    client = AudioCaptureClient(socket_path, TOKEN)
    try:
        await client.connect()
        async with asyncio.timeout(1.0):
            with pytest.raises(AudioCaptureError) as exc_info:
                await anext(client.pcm_messages())
        assert client.connected is False
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert exc_info.value.code == "helper_disconnected"


async def test_client_rejects_new_request_immediately_after_disconnect(
    socket_dir: Path,
) -> None:
    socket_path = socket_dir / "capture.sock"

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _ack_hello(reader, writer, [])
        writer.close()
        await writer.wait_closed()

    server = await _serve(socket_path, handler)
    client = AudioCaptureClient(socket_path, TOKEN, command_timeout_secs=1.0)
    try:
        await client.connect()
        async with asyncio.timeout(1.0):
            with pytest.raises(AudioCaptureError):
                await anext(client.pcm_messages())
        async with asyncio.timeout(0.1):
            with pytest.raises(AudioCaptureError) as exc_info:
                await client.request({"type": "list_devices"}, expected_type="devices")
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert exc_info.value.code == "helper_disconnected"


class FakeCaptureClient:
    def __init__(self, *, prepare_error: AudioCaptureError | None = None) -> None:
        self.prepare_error = prepare_error
        self.commands: list[str] = []
        self.queue: asyncio.Queue[PCMMessage] = asyncio.Queue()
        self.dropped_frames = 0
        self.queued_frames = 0

    async def prepare_capture(self, capture_id: UUID, **kwargs) -> dict[str, object]:
        self.commands.append("prepare_capture")
        if self.prepare_error:
            raise self.prepare_error
        return {
            "type": "ready",
            "capture_id": str(capture_id),
            "source_id": str(SOURCE_ID),
            "device_generation": 3,
        }

    async def commit_capture(self, capture_id: UUID) -> None:
        self.commands.append("commit_capture")

    async def abort_capture(self, capture_id: UUID) -> None:
        self.commands.append("abort_capture")

    async def stop_capture(self, capture_id: UUID) -> None:
        self.commands.append("stop_capture")

    async def pcm_messages(self) -> AsyncIterator[PCMMessage]:
        while True:
            yield await self.queue.get()


class FakeSupervisor:
    def __init__(self, client: FakeCaptureClient) -> None:
        self.client = client
        self.start_calls = 0
        self.stop_calls = 0

    async def start_client(self) -> FakeCaptureClient:
        self.start_calls += 1
        return self.client

    async def stop(self) -> None:
        self.stop_calls += 1


async def test_physical_output_source_lifecycle_and_stale_capture_filter() -> None:
    client = FakeCaptureClient()
    supervisor = FakeSupervisor(client)
    source = PhysicalOutputSource(supervisor)

    await source.prepare(CAPTURE_ID)
    assert source.state is AudioSourceState.READY
    await source.commit()
    await client.queue.put(_pcm(0, capture_id=UUID(int=99)))
    await client.queue.put(_pcm(1))
    async with asyncio.timeout(1.0):
        frame = await anext(source.frames())

    assert source.state is AudioSourceState.ACTIVE
    assert frame.capture_id == CAPTURE_ID
    assert frame.source_id == str(SOURCE_ID)
    assert frame.device_generation == 3
    await source.stop()
    await source.stop()
    assert client.commands == ["prepare_capture", "commit_capture", "stop_capture"]
    assert supervisor.stop_calls == 1
    assert source.state is AudioSourceState.STOPPED


async def test_physical_output_source_prepare_failure_is_abortable() -> None:
    client = FakeCaptureClient(
        prepare_error=AudioCaptureError(
            "permission_denied",
            "系统音频权限未授予",
            retryable=False,
        )
    )
    supervisor = FakeSupervisor(client)
    source = PhysicalOutputSource(supervisor)

    with pytest.raises(AudioCaptureError, match="系统音频权限未授予"):
        await source.prepare(CAPTURE_ID)

    assert source.state is AudioSourceState.FAILED
    assert supervisor.stop_calls == 1
    await source.abort()
    assert source.state is AudioSourceState.STOPPED


async def test_supervisor_retries_only_bounded_retryable_failures(tmp_path: Path) -> None:
    settings = AudioCaptureSettings(
        enabled=True,
        helper_executable=tmp_path / "helper",
        runtime_dir=tmp_path / "runtime",
        restart_attempts=2,
        restart_backoff_secs=0.001,
        max_restart_backoff_secs=0.002,
    )

    class FailingSupervisor(HelperSupervisor):
        calls = 0

        async def _start_once(self) -> AudioCaptureClient:
            self.calls += 1
            raise AudioCaptureError("helper_start_failed", "Helper 启动失败", retryable=True)

    supervisor = FailingSupervisor(settings)
    with pytest.raises(AudioCaptureError, match="Helper 启动失败"):
        await supervisor.start_client()

    assert supervisor.calls == 3


async def test_supervisor_cleans_stale_client_before_restarting(tmp_path: Path) -> None:
    settings = AudioCaptureSettings(
        enabled=True,
        helper_executable=tmp_path / "helper",
        runtime_dir=tmp_path / "runtime",
        restart_attempts=0,
    )
    stale_client = AudioCaptureClient(tmp_path / "stale.sock", TOKEN)
    replacement = AudioCaptureClient(tmp_path / "replacement.sock", TOKEN)

    class RecoveringSupervisor(HelperSupervisor):
        stop_calls = 0

        async def _stop_current(self) -> None:
            self.stop_calls += 1
            self._client = None

        async def _start_once(self) -> AudioCaptureClient:
            return replacement

    supervisor = RecoveringSupervisor(settings)
    supervisor._client = stale_client

    assert await supervisor.start_client() is replacement
    assert supervisor.stop_calls == 1


def test_helper_executable_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "real-helper"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    link = tmp_path / "helper"
    link.symlink_to(target)

    with pytest.raises(AudioCaptureError) as exc_info:
        _validated_executable(link)

    assert exc_info.value.code == "insecure_helper"


def test_audio_capture_settings_are_disabled_and_bounded_by_default() -> None:
    settings = AudioCaptureSettings(_env_file=None)

    assert settings.enabled is False
    assert settings.helper_executable is None
    assert settings.command_timeout_secs == 30.0
    assert settings.queue_size == 8
    assert settings.restart_attempts == 3
    assert settings.runtime_dir == Path("runtime/audio-capture")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"queue_size": 0},
        {"startup_timeout_secs": 0},
        {"command_timeout_secs": 31},
        {"restart_attempts": 11},
        {"restart_backoff_secs": 2, "max_restart_backoff_secs": 1},
    ],
)
def test_audio_capture_settings_reject_invalid_limits(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AudioCaptureSettings(_env_file=None, **kwargs)
