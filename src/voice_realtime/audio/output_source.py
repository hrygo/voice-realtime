"""物理输出 Helper 的 UDS client、进程监管与 AudioSource 适配。"""

from __future__ import annotations

import asyncio
import os
import secrets
import stat
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from voice_realtime.audio.frame import AudioFrame, AudioSourceKind, AudioSourceRole
from voice_realtime.audio.ipc import (
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    PCMMessage,
    WireDecoder,
    WireProtocolError,
    encode_control_message,
)
from voice_realtime.audio.source import AudioSourceHealth, AudioSourceState
from voice_realtime.config import AudioCaptureSettings

_READ_SIZE = 65_536
_EVENT_QUEUE_SIZE = 64
_SOCKET_PATH_MAX_BYTES = 100
_TOKEN_ENV = "VR_AUDIO_CAPTURE_TOKEN"
_HEX_DIGITS = frozenset("0123456789abcdef")

_SAFE_ERROR_MESSAGES = {
    "invalid_message": "Helper 返回了无效消息",
    "unsupported_protocol": "Helper 协议版本不兼容",
    "authentication_failed": "Helper 身份校验失败",
    "permission_denied": "系统音频权限未授予",
    "unsupported_os": "当前 macOS 版本不支持系统音频采集",
    "unsupported_device_scope": "该输出设备无法安全限定采集范围",
    "device_unavailable": "目标输出设备不可用",
    "invalid_state": "Helper 当前状态不允许该操作",
    "capture_conflict": "Helper 已有其他采集事务",
    "callback_timeout": "未收到输出设备音频回调",
    "conversion_failed": "系统输出音频格式转换失败",
    "io_failed": "系统输出音频 I/O 失败",
    "internal_error": "系统音频 Helper 内部错误",
}


class AudioCaptureError(RuntimeError):
    """物理输出采集边界的稳定、脱敏错误。"""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class _TerminalMessage:
    error: AudioCaptureError


class AudioCaptureClient:
    """单连接 UDS client；控制响应关联与 PCM 背压在此收口。"""

    def __init__(
        self,
        socket_path: Path,
        capture_token: str,
        *,
        queue_size: int = 8,
        command_timeout_secs: float = 5.0,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        if command_timeout_secs <= 0:
            raise ValueError("command_timeout_secs must be positive")
        if not _is_capture_token(capture_token):
            raise ValueError("capture_token must be 64 lowercase hex characters")
        self._socket_path = socket_path
        self._capture_token = capture_token
        self._command_timeout_secs = command_timeout_secs
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, object]]] = {}
        self._pcm_queue: asyncio.Queue[PCMMessage | _TerminalMessage] = asyncio.Queue(
            maxsize=queue_size
        )
        self._event_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(
            maxsize=_EVENT_QUEUE_SIZE
        )
        self._closing = False
        self._failure: AudioCaptureError | None = None
        self._dropped_frames = 0

    @property
    def connected(self) -> bool:
        reader_task = self._reader_task
        return (
            self._writer is not None
            and reader_task is not None
            and not reader_task.done()
            and not self._closing
            and self._failure is None
        )

    @property
    def queued_frames(self) -> int:
        return self._pcm_queue.qsize()

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    async def connect(self) -> None:
        if self.connected:
            return
        _assert_private_socket(self._socket_path)
        self._closing = False
        self._failure = None
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                str(self._socket_path)
            )
        except OSError as exc:
            raise AudioCaptureError(
                "socket_unavailable",
                "无法连接系统音频 Helper",
                retryable=True,
            ) from exc
        self._reader_task = asyncio.create_task(
            self._reader_loop(),
            name="audio-capture-client-reader",
        )
        try:
            response = await self.request(
                {
                    "type": "hello",
                    "protocol_major": PROTOCOL_MAJOR,
                    "protocol_minor": PROTOCOL_MINOR,
                    "capture_token": self._capture_token,
                    "client_pid": os.getpid(),
                },
                expected_type="hello_ack",
            )
            if (
                _strict_int(response.get("protocol_major")) != PROTOCOL_MAJOR
                or _strict_int(response.get("protocol_minor")) < PROTOCOL_MINOR
                or not isinstance(response.get("helper_version"), str)
                or not isinstance(response.get("capabilities"), list)
            ):
                raise _invalid_helper_response()
        except BaseException:
            await self.close()
            raise

    async def request(
        self,
        payload: dict[str, object],
        *,
        expected_type: str,
    ) -> dict[str, object]:
        writer = self._writer
        failure = self._failure
        if failure is not None:
            raise AudioCaptureError(
                failure.code,
                str(failure),
                retryable=failure.retryable,
            )
        if writer is None or self._closing:
            raise AudioCaptureError(
                "helper_disconnected",
                "系统音频 Helper 未连接",
                retryable=True,
            )
        if "request_id" in payload:
            raise ValueError("request_id is generated by AudioCaptureClient")
        request_id = uuid4().hex
        outbound = {**payload, "request_id": request_id}
        frame = encode_control_message(outbound)
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(self._command_timeout_secs):
                async with self._write_lock:
                    writer.write(frame)
                    await writer.drain()
                response = await future
        except TimeoutError as exc:
            raise AudioCaptureError(
                "helper_timeout",
                "系统音频 Helper 响应超时",
                retryable=True,
            ) from exc
        except OSError as exc:
            raise AudioCaptureError(
                "helper_disconnected",
                "系统音频 Helper 连接已断开",
                retryable=True,
            ) from exc
        finally:
            self._pending.pop(request_id, None)
        if response.get("type") != expected_type:
            raise _invalid_helper_response()
        return response

    async def list_devices(self) -> list[dict[str, object]]:
        response = await self.request(
            {"type": "list_devices"},
            expected_type="devices",
        )
        devices = response.get("devices")
        if not isinstance(devices, list) or not all(
            isinstance(device, dict) for device in devices
        ):
            raise _invalid_helper_response()
        return [dict(device) for device in devices]

    async def prepare_capture(
        self,
        capture_id: UUID,
        *,
        follow_default_output: bool = True,
        device_ref: str | None = None,
        exclude_pids: Sequence[int] = (),
    ) -> dict[str, object]:
        if not follow_default_output and not device_ref:
            raise ValueError("device_ref is required when default output is not followed")
        response = await self.request(
            {
                "type": "prepare_capture",
                "capture_id": str(capture_id),
                "follow_default_output": follow_default_output,
                "device_ref": device_ref,
                "exclude_pids": list(exclude_pids),
            },
            expected_type="ready",
        )
        if response.get("capture_id") != str(capture_id):
            raise _invalid_helper_response()
        try:
            UUID(str(response.get("source_id")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise _invalid_helper_response() from exc
        if _strict_int(response.get("device_generation")) < 0:
            raise _invalid_helper_response()
        return response

    async def commit_capture(self, capture_id: UUID) -> None:
        await self._capture_command("commit_capture", capture_id)

    async def abort_capture(self, capture_id: UUID) -> None:
        await self._capture_command("abort_capture", capture_id)

    async def stop_capture(self, capture_id: UUID) -> None:
        await self._capture_command("stop_capture", capture_id)

    async def pcm_messages(self) -> AsyncIterator[PCMMessage]:
        while True:
            item = await self._pcm_queue.get()
            if isinstance(item, _TerminalMessage):
                raise item.error
            yield item

    async def events(self) -> AsyncIterator[dict[str, object]]:
        while True:
            yield await self._event_queue.get()

    async def close(self) -> None:
        if self._closing and self._writer is None:
            return
        self._closing = True
        task = self._reader_task
        self._reader_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is not None:
            writer.close()
            with suppress(ConnectionError, OSError, TimeoutError):
                async with asyncio.timeout(self._command_timeout_secs):
                    await writer.wait_closed()
        self._fail_pending(
            AudioCaptureError(
                "helper_disconnected",
                "系统音频 Helper 连接已关闭",
                retryable=True,
            )
        )

    async def _capture_command(self, command: str, capture_id: UUID) -> None:
        response = await self.request(
            {"type": command, "capture_id": str(capture_id)},
            expected_type="ack",
        )
        if (
            response.get("command") != command
            or response.get("capture_id") != str(capture_id)
        ):
            raise _invalid_helper_response()

    async def _reader_loop(self) -> None:
        decoder = WireDecoder()
        failure: AudioCaptureError | None = None
        try:
            reader = self._reader
            if reader is None:
                raise RuntimeError("reader is not initialized")
            while True:
                data = await reader.read(_READ_SIZE)
                if not data:
                    failure = AudioCaptureError(
                        "helper_disconnected",
                        "系统音频 Helper 连接已断开",
                        retryable=True,
                    )
                    break
                for message in decoder.feed(data):
                    if isinstance(message, PCMMessage):
                        self._enqueue_pcm(message)
                    else:
                        self._handle_control(message.payload)
        except asyncio.CancelledError:
            raise
        except WireProtocolError:
            failure = _invalid_helper_response()
        except (OSError, RuntimeError):
            failure = AudioCaptureError(
                "helper_disconnected",
                "系统音频 Helper 连接已断开",
                retryable=True,
            )
        finally:
            if failure is not None and not self._closing:
                self._failure = failure
                self._fail_pending(failure)
                self._enqueue_terminal(failure)

    def _handle_control(self, payload: dict[str, object]) -> None:
        message_type = payload.get("type")
        request_id = payload.get("request_id")
        if message_type == "error":
            error = _error_from_payload(payload)
            if isinstance(request_id, str):
                future = self._pending.get(request_id)
                if future is not None and not future.done():
                    future.set_exception(error)
                    return
            self._failure = error
            self._fail_pending(error)
            self._enqueue_terminal(error)
            return
        if isinstance(request_id, str):
            future = self._pending.get(request_id)
            if future is not None and not future.done():
                future.set_result(payload)
                return
        if self._event_queue.full():
            with suppress(asyncio.QueueEmpty):
                self._event_queue.get_nowait()
        self._event_queue.put_nowait(payload)

    def _enqueue_pcm(self, message: PCMMessage) -> None:
        if self._pcm_queue.full():
            with suppress(asyncio.QueueEmpty):
                self._pcm_queue.get_nowait()
            self._dropped_frames += 1
        self._pcm_queue.put_nowait(message)

    def _enqueue_terminal(self, error: AudioCaptureError) -> None:
        if self._pcm_queue.full():
            with suppress(asyncio.QueueEmpty):
                self._pcm_queue.get_nowait()
        self._pcm_queue.put_nowait(_TerminalMessage(error))

    def _fail_pending(self, error: AudioCaptureError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)


class CaptureClient(Protocol):
    """`PhysicalOutputSource` 所需的最小 client 接口。"""

    @property
    def queued_frames(self) -> int: ...

    @property
    def dropped_frames(self) -> int: ...

    async def prepare_capture(
        self,
        capture_id: UUID,
        *,
        follow_default_output: bool = True,
        device_ref: str | None = None,
        exclude_pids: Sequence[int] = (),
    ) -> dict[str, object]: ...

    async def commit_capture(self, capture_id: UUID) -> None: ...

    async def abort_capture(self, capture_id: UUID) -> None: ...

    async def stop_capture(self, capture_id: UUID) -> None: ...

    def pcm_messages(self) -> AsyncIterator[PCMMessage]: ...


class CaptureSupervisor(Protocol):
    """来源层可替换的 Helper 监管接口。"""

    async def start_client(self) -> CaptureClient: ...

    async def stop(self) -> None: ...


class HelperSupervisor:
    """按固定配置启动 Helper，并对启动期失败实施有限退避。"""

    def __init__(self, settings: AudioCaptureSettings) -> None:
        self._settings = settings
        self._process: asyncio.subprocess.Process | None = None
        self._client: AudioCaptureClient | None = None
        self._socket_path: Path | None = None

    async def start_client(self) -> AudioCaptureClient:
        if self._client is not None and self._client.connected:
            return self._client
        if (
            self._client is not None
            or self._process is not None
            or self._socket_path is not None
        ):
            await self._stop_current()
        attempts = self._settings.restart_attempts + 1
        delay = self._settings.restart_backoff_secs
        last_error: AudioCaptureError | None = None
        for attempt in range(attempts):
            try:
                client = await self._start_once()
                self._client = client
                return client
            except AudioCaptureError as exc:
                last_error = exc
                await self._stop_current()
                if not exc.retryable or attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._settings.max_restart_backoff_secs)
        if last_error is not None:
            raise last_error
        raise AudioCaptureError(
            "helper_start_failed",
            "Helper 启动失败",
            retryable=True,
        )

    async def stop(self) -> None:
        await self._stop_current()

    async def _start_once(self) -> AudioCaptureClient:
        if not self._settings.enabled:
            raise AudioCaptureError(
                "feature_disabled",
                "系统音频采集功能尚未启用",
                retryable=False,
            )
        executable = _validated_executable(self._settings.helper_executable)
        runtime_dir = _prepare_runtime_dir(self._settings.runtime_dir)
        socket_path = runtime_dir / f"capture-{os.getpid()}-{secrets.token_hex(6)}.sock"
        if len(os.fsencode(socket_path)) >= _SOCKET_PATH_MAX_BYTES:
            raise AudioCaptureError(
                "socket_path_too_long",
                "系统音频运行目录路径过长",
                retryable=False,
            )
        token = secrets.token_hex(32)
        environment = {
            _TOKEN_ENV: token,
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        }
        try:
            process = await asyncio.create_subprocess_exec(
                str(executable),
                "--socket",
                str(socket_path),
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise AudioCaptureError(
                "helper_start_failed",
                "Helper 启动失败",
                retryable=True,
            ) from exc
        self._process = process
        self._socket_path = socket_path
        deadline = (
            asyncio.get_running_loop().time()
            + self._settings.startup_timeout_secs
        )
        last_connect_error: AudioCaptureError | None = None
        while asyncio.get_running_loop().time() < deadline:
            if process.returncode is not None:
                raise AudioCaptureError(
                    "helper_start_failed",
                    "Helper 在建立连接前退出",
                    retryable=True,
                )
            if socket_path.exists():
                client = AudioCaptureClient(
                    socket_path,
                    token,
                    queue_size=self._settings.queue_size,
                    command_timeout_secs=self._settings.command_timeout_secs,
                )
                try:
                    await client.connect()
                    return client
                except AudioCaptureError as exc:
                    await client.close()
                    last_connect_error = exc
                    if not exc.retryable:
                        raise
            await asyncio.sleep(0.02)
        if last_connect_error is not None and not last_connect_error.retryable:
            raise last_connect_error
        raise AudioCaptureError(
            "helper_start_timeout",
            "Helper 启动超时",
            retryable=True,
        )

    async def _stop_current(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.close()
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                async with asyncio.timeout(self._settings.command_timeout_secs):
                    await process.wait()
            except TimeoutError:
                process.kill()
                await process.wait()
        socket_path = self._socket_path
        self._socket_path = None
        if socket_path is not None:
            with suppress(FileNotFoundError):
                socket_path.unlink()


class PhysicalOutputSource:
    """把 Helper PCM 流适配为 far-end `AudioSource`。"""

    def __init__(
        self,
        supervisor: CaptureSupervisor,
        *,
        follow_default_output: bool = True,
        device_ref: str | None = None,
        exclude_pids: Sequence[int] = (),
    ) -> None:
        if not follow_default_output and not device_ref:
            raise ValueError("device_ref is required when default output is not followed")
        self._supervisor = supervisor
        self._follow_default_output = follow_default_output
        self._device_ref = device_ref
        self._exclude_pids = tuple(exclude_pids)
        self._state = AudioSourceState.STOPPED
        self._client: CaptureClient | None = None
        self._capture_id: UUID | None = None
        self._source_id: UUID | None = None
        self._last_sequence: int | None = None
        self._last_host_time_ns: int | None = None

    @property
    def kind(self) -> AudioSourceKind:
        return AudioSourceKind.PHYSICAL_OUTPUT

    @property
    def role(self) -> AudioSourceRole:
        return AudioSourceRole.FAR_END

    @property
    def state(self) -> AudioSourceState:
        return self._state

    async def prepare(self, capture_id: UUID) -> None:
        if self._state is not AudioSourceState.STOPPED:
            raise RuntimeError("physical output source must be stopped before prepare")
        self._state = AudioSourceState.PREPARING
        self._capture_id = capture_id
        self._last_sequence = None
        self._last_host_time_ns = None
        try:
            client = await self._supervisor.start_client()
            self._client = client
            response = await client.prepare_capture(
                capture_id,
                follow_default_output=self._follow_default_output,
                device_ref=self._device_ref,
                exclude_pids=self._exclude_pids,
            )
            if response.get("capture_id") != str(capture_id):
                raise _invalid_helper_response()
            source_id = UUID(str(response.get("source_id")))
            generation = _strict_int(response.get("device_generation"))
            if generation < 0:
                raise _invalid_helper_response()
            self._source_id = source_id
        except BaseException:
            self._state = AudioSourceState.FAILED
            self._client = None
            with suppress(Exception):
                await self._supervisor.stop()
            raise
        self._state = AudioSourceState.READY

    async def commit(self) -> None:
        client = self._client
        capture_id = self._capture_id
        if (
            self._state is not AudioSourceState.READY
            or client is None
            or capture_id is None
        ):
            raise RuntimeError("physical output source is not ready")
        try:
            await client.commit_capture(capture_id)
        except BaseException:
            self._state = AudioSourceState.FAILED
            raise
        self._state = AudioSourceState.ACTIVE

    async def abort(self) -> None:
        await self._release(abort=True)

    async def stop(self) -> None:
        await self._release(abort=False)

    async def frames(self) -> AsyncIterator[AudioFrame]:
        client = self._client
        capture_id = self._capture_id
        source_id = self._source_id
        if (
            self._state is not AudioSourceState.ACTIVE
            or client is None
            or capture_id is None
            or source_id is None
        ):
            raise RuntimeError("physical output source is not active")
        try:
            async for message in client.pcm_messages():
                if (
                    message.capture_id != capture_id
                    or message.source_id != source_id
                ):
                    continue
                self._last_sequence = message.sequence
                self._last_host_time_ns = message.host_time_ns
                yield message.to_audio_frame()
        except AudioCaptureError:
            self._state = AudioSourceState.FAILED
            raise

    def health(self) -> AudioSourceHealth:
        client = self._client
        return AudioSourceHealth(
            state=self._state,
            queued_frames=client.queued_frames if client is not None else 0,
            dropped_frames=client.dropped_frames if client is not None else 0,
            last_sequence=self._last_sequence,
            last_host_time_ns=self._last_host_time_ns,
        )

    async def _release(self, *, abort: bool) -> None:
        if self._state is AudioSourceState.STOPPED:
            return
        client = self._client
        capture_id = self._capture_id
        error: AudioCaptureError | None = None
        try:
            if client is not None and capture_id is not None:
                try:
                    if abort:
                        await client.abort_capture(capture_id)
                    else:
                        await client.stop_capture(capture_id)
                except AudioCaptureError as exc:
                    error = exc
        finally:
            try:
                await self._supervisor.stop()
            finally:
                self._state = AudioSourceState.STOPPED
                self._client = None
                self._capture_id = None
                self._source_id = None
        if error is not None:
            raise error


def _assert_private_socket(socket_path: Path) -> None:
    try:
        parent_stat = os.lstat(socket_path.parent)
        socket_stat = os.lstat(socket_path)
    except OSError as exc:
        raise AudioCaptureError(
            "socket_unavailable",
            "系统音频 Helper socket 不可用",
            retryable=True,
        ) from exc
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or stat.S_ISLNK(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or stat.S_IMODE(parent_stat.st_mode) & 0o077
    ):
        raise AudioCaptureError(
            "insecure_runtime_dir",
            "系统音频运行目录权限不安全",
            retryable=False,
        )
    if (
        not stat.S_ISSOCK(socket_stat.st_mode)
        or socket_stat.st_uid != os.geteuid()
        or stat.S_IMODE(socket_stat.st_mode) & 0o077
    ):
        raise AudioCaptureError(
            "insecure_socket",
            "系统音频 Helper socket 权限不安全",
            retryable=False,
        )


def _prepare_runtime_dir(configured_path: Path) -> Path:
    path = configured_path.absolute()
    try:
        if not path.exists():
            path.mkdir(mode=0o700, parents=True)
        info = os.lstat(path)
    except OSError as exc:
        raise AudioCaptureError(
            "runtime_dir_unavailable",
            "无法创建系统音频运行目录",
            retryable=False,
        ) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise AudioCaptureError(
            "insecure_runtime_dir",
            "系统音频运行目录权限不安全",
            retryable=False,
        )
    return path


def _validated_executable(configured_path: Path | None) -> Path:
    if configured_path is None:
        raise AudioCaptureError(
            "helper_unavailable",
            "未配置系统音频 Helper",
            retryable=False,
        )
    try:
        configured_info = os.lstat(configured_path)
        if stat.S_ISLNK(configured_info.st_mode):
            raise AudioCaptureError(
                "insecure_helper",
                "系统音频 Helper 文件权限不安全",
                retryable=False,
            )
        path = configured_path.resolve(strict=True)
        info = os.lstat(path)
    except OSError as exc:
        raise AudioCaptureError(
            "helper_unavailable",
            "系统音频 Helper 不可用",
            retryable=False,
        ) from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(info.st_mode) & 0o022
        or not os.access(path, os.X_OK)
    ):
        raise AudioCaptureError(
            "insecure_helper",
            "系统音频 Helper 文件权限不安全",
            retryable=False,
        )
    return path


def _strict_int(value: object) -> int:
    if type(value) is not int:
        raise _invalid_helper_response()
    return value


def _error_from_payload(payload: dict[str, object]) -> AudioCaptureError:
    code = payload.get("code")
    retryable = payload.get("retryable")
    if not isinstance(code, str) or type(retryable) is not bool:
        return _invalid_helper_response()
    safe_message = _SAFE_ERROR_MESSAGES.get(code)
    if safe_message is None:
        return _invalid_helper_response()
    return AudioCaptureError(code, safe_message, retryable=retryable)


def _invalid_helper_response() -> AudioCaptureError:
    return AudioCaptureError(
        "invalid_helper_response",
        "系统音频 Helper 返回了无效响应",
        retryable=False,
    )


def _is_capture_token(value: str) -> bool:
    return len(value) == 64 and all(character in _HEX_DIGITS for character in value)
