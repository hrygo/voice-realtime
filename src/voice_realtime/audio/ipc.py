"""物理输出采集 Helper 的严格 UDS v1 wire codec。"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Final
from uuid import UUID

from voice_realtime.audio.frame import (
    AudioFrame,
    AudioFrameFlag,
    AudioSourceKind,
    AudioSourceRole,
)

PROTOCOL_MAGIC: Final = b"VRAC"
PROTOCOL_MAJOR: Final = 1
PROTOCOL_MINOR: Final = 0
COMMON_HEADER_SIZE: Final = 16
PCM_HEADER_SIZE: Final = 84
MAX_HEADER_SIZE: Final = 256
MAX_CONTROL_BODY_BYTES: Final = 65_536
MAX_FRAME_BYTES: Final = 1_048_576
PCM_PAYLOAD_BYTES: Final = 1_024

_COMMON_HEADER = struct.Struct(">4sHBBBBHI")
_PCM_EXTENSION = struct.Struct(">16s16sIQQIHBBII")
_KNOWN_FRAME_FLAGS = int(
    AudioFrameFlag.DISCONTINUITY
    | AudioFrameFlag.SILENCE_FILL
    | AudioFrameFlag.END_OF_STREAM
)


class WireMessageType(IntEnum):
    """UDS v1 wire message discriminator。"""

    JSON = 1
    PCM = 2


class WireProtocolError(ValueError):
    """只携带稳定错误码和脱敏消息的协议异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ControlMessage:
    """已完成基础边界校验的 JSON object。"""

    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class PCMMessage:
    """UDS v1 固定格式 PCM 消息。"""

    capture_id: UUID
    source_id: UUID
    device_generation: int
    sequence: int
    host_time_ns: int
    sample_rate: int
    samples_per_channel: int
    channels: int
    sample_width: int
    flags: AudioFrameFlag
    pcm: bytes

    def __post_init__(self) -> None:
        if self.device_generation < 0:
            raise ValueError("device_generation must be non-negative")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if self.host_time_ns < 0:
            raise ValueError("host_time_ns must be non-negative")
        if (
            self.sample_rate != 16_000
            or self.samples_per_channel != 512
            or self.channels != 1
            or self.sample_width != 2
        ):
            raise ValueError("PCM message must use the normalized v1 format")
        if len(self.pcm) != PCM_PAYLOAD_BYTES:
            raise ValueError(f"PCM message payload must be {PCM_PAYLOAD_BYTES} bytes")
        if int(self.flags) & ~_KNOWN_FRAME_FLAGS:
            raise ValueError("PCM message contains unknown frame flags")

    def to_audio_frame(self) -> AudioFrame:
        """投影为音频域的 far-end 物理输出帧。"""
        return AudioFrame(
            capture_id=self.capture_id,
            source_id=str(self.source_id),
            source_kind=AudioSourceKind.PHYSICAL_OUTPUT,
            source_role=AudioSourceRole.FAR_END,
            device_generation=self.device_generation,
            sequence=self.sequence,
            host_time_ns=self.host_time_ns,
            pcm=self.pcm,
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_width=self.sample_width,
            samples_per_channel=self.samples_per_channel,
            flags=self.flags,
        )


WireMessage = ControlMessage | PCMMessage


def encode_control_message(payload: dict[str, object]) -> bytes:
    """编码一个经过基础字段校验的 JSON 控制帧。"""
    validated = _validate_control_payload(payload)
    try:
        body = json.dumps(
            validated,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise WireProtocolError(
            "invalid_control",
            "control message is not valid JSON",
        ) from exc
    if len(body) > MAX_CONTROL_BODY_BYTES:
        raise WireProtocolError("frame_too_large", "control frame too large")
    return _COMMON_HEADER.pack(
        PROTOCOL_MAGIC,
        COMMON_HEADER_SIZE,
        PROTOCOL_MAJOR,
        PROTOCOL_MINOR,
        WireMessageType.JSON,
        0,
        0,
        len(body),
    ) + body


def encode_pcm_message(message: PCMMessage) -> bytes:
    """编码一个固定 84-byte header 的 PCM 帧。"""
    body_length = len(message.pcm)
    common = _COMMON_HEADER.pack(
        PROTOCOL_MAGIC,
        PCM_HEADER_SIZE,
        PROTOCOL_MAJOR,
        PROTOCOL_MINOR,
        WireMessageType.PCM,
        0,
        0,
        body_length,
    )
    extension = _PCM_EXTENSION.pack(
        message.capture_id.bytes,
        message.source_id.bytes,
        message.device_generation,
        message.sequence,
        message.host_time_ns,
        message.sample_rate,
        message.samples_per_channel,
        message.channels,
        message.sample_width,
        int(message.flags),
        body_length,
    )
    return common + extension + message.pcm


class WireDecoder:
    """可接受任意 socket 分片的有界增量解码器。"""

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes | bytearray | memoryview) -> list[WireMessage]:
        """追加字节并返回本次形成的全部完整消息。"""
        self._buffer.extend(data)
        messages: list[WireMessage] = []
        while len(self._buffer) >= COMMON_HEADER_SIZE:
            (
                magic,
                header_length,
                major,
                minor,
                raw_message_type,
                prefix_flags,
                reserved,
                body_length,
            ) = _COMMON_HEADER.unpack_from(self._buffer)
            message_type = _validate_common_header(
                magic=magic,
                header_length=header_length,
                major=major,
                minor=minor,
                raw_message_type=raw_message_type,
                prefix_flags=prefix_flags,
                reserved=reserved,
                body_length=body_length,
            )
            frame_length = header_length + body_length
            if frame_length > MAX_FRAME_BYTES:
                raise WireProtocolError("frame_too_large", "wire frame too large")
            if len(self._buffer) < frame_length:
                break

            frame = bytes(self._buffer[:frame_length])
            message = (
                _decode_control(frame, header_length, body_length)
                if message_type is WireMessageType.JSON
                else _decode_pcm(frame, header_length, body_length)
            )
            del self._buffer[:frame_length]
            messages.append(message)
        return messages


def _validate_common_header(
    *,
    magic: bytes,
    header_length: int,
    major: int,
    minor: int,
    raw_message_type: int,
    prefix_flags: int,
    reserved: int,
    body_length: int,
) -> WireMessageType:
    if magic != PROTOCOL_MAGIC:
        raise WireProtocolError("invalid_magic", "invalid wire magic")
    if major != PROTOCOL_MAJOR:
        raise WireProtocolError("unsupported_protocol", "unsupported protocol major")
    try:
        message_type = WireMessageType(raw_message_type)
    except ValueError as exc:
        raise WireProtocolError(
            "unsupported_message_type",
            "unsupported wire message type",
        ) from exc
    if prefix_flags != 0 or reserved != 0:
        raise WireProtocolError("invalid_header", "reserved header fields must be zero")
    minimum_header = (
        COMMON_HEADER_SIZE
        if message_type is WireMessageType.JSON
        else PCM_HEADER_SIZE
    )
    if header_length < minimum_header or header_length > MAX_HEADER_SIZE:
        raise WireProtocolError("invalid_header", "invalid wire header length")
    if minor == PROTOCOL_MINOR and header_length != minimum_header:
        raise WireProtocolError("invalid_header", "unexpected v1.0 header extension")
    if message_type is WireMessageType.JSON and body_length > MAX_CONTROL_BODY_BYTES:
        raise WireProtocolError("frame_too_large", "control frame too large")
    if message_type is WireMessageType.PCM and body_length != PCM_PAYLOAD_BYTES:
        raise WireProtocolError("invalid_pcm", "invalid PCM payload length")
    return message_type


def _decode_control(
    frame: bytes,
    header_length: int,
    body_length: int,
) -> ControlMessage:
    body = frame[header_length : header_length + body_length]
    try:
        decoded: object = json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise WireProtocolError(
            "invalid_control",
            "control body is not valid JSON",
        ) from exc
    return ControlMessage(payload=_validate_control_payload(decoded))


def _decode_pcm(
    frame: bytes,
    header_length: int,
    body_length: int,
) -> PCMMessage:
    (
        capture_bytes,
        source_bytes,
        device_generation,
        sequence,
        host_time_ns,
        sample_rate,
        samples_per_channel,
        channels,
        sample_width,
        raw_flags,
        payload_length,
    ) = _PCM_EXTENSION.unpack_from(frame, COMMON_HEADER_SIZE)
    if payload_length != body_length:
        raise WireProtocolError("invalid_pcm", "PCM length fields do not match")
    if (
        sample_rate != 16_000
        or samples_per_channel != 512
        or channels != 1
        or sample_width != 2
    ):
        raise WireProtocolError("invalid_pcm_format", "unsupported PCM format")
    if raw_flags & ~_KNOWN_FRAME_FLAGS:
        raise WireProtocolError("invalid_frame_flags", "unknown PCM frame flags")
    pcm = frame[header_length : header_length + body_length]
    try:
        return PCMMessage(
            capture_id=UUID(bytes=capture_bytes),
            source_id=UUID(bytes=source_bytes),
            device_generation=device_generation,
            sequence=sequence,
            host_time_ns=host_time_ns,
            sample_rate=sample_rate,
            samples_per_channel=samples_per_channel,
            channels=channels,
            sample_width=sample_width,
            flags=AudioFrameFlag(raw_flags),
            pcm=pcm,
        )
    except (TypeError, ValueError) as exc:
        raise WireProtocolError("invalid_pcm", "invalid PCM metadata") from exc


def _validate_control_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise WireProtocolError(
            "invalid_control",
            "control body must be a JSON object",
        )
    if not all(isinstance(key, str) for key in payload):
        raise WireProtocolError("invalid_control", "control keys must be strings")
    message_type = payload.get("type")
    if not isinstance(message_type, str) or not 1 <= len(message_type) <= 64:
        raise WireProtocolError("invalid_control", "control type is invalid")
    request_id = payload.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or not 1 <= len(request_id) <= 64
    ):
        raise WireProtocolError("invalid_control", "request_id is invalid")
    return dict(payload)


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")
