"""物理输出 Helper UDS v1 wire contract 测试。"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from voice_realtime.audio.frame import AudioFrameFlag, AudioSourceKind, AudioSourceRole
from voice_realtime.audio.ipc import (
    COMMON_HEADER_SIZE,
    MAX_CONTROL_BODY_BYTES,
    PCM_HEADER_SIZE,
    ControlMessage,
    PCMMessage,
    WireDecoder,
    WireMessageType,
    WireProtocolError,
    encode_control_message,
    encode_pcm_message,
)

ROOT = Path(__file__).parents[1]
CONTRACT_DIR = ROOT / "contracts" / "audio-capture" / "v1"
CAPTURE_ID = UUID("00000000-0000-0000-0000-000000000001")
SOURCE_ID = UUID("00000000-0000-0000-0000-000000000002")


def _pcm_message() -> PCMMessage:
    return PCMMessage(
        capture_id=CAPTURE_ID,
        source_id=SOURCE_ID,
        device_generation=3,
        sequence=4,
        host_time_ns=5,
        sample_rate=16_000,
        samples_per_channel=512,
        channels=1,
        sample_width=2,
        flags=AudioFrameFlag.DISCONTINUITY,
        pcm=bytes(1_024),
    )


def _decode_one(data: bytes) -> ControlMessage | PCMMessage:
    messages = WireDecoder().feed(data)
    assert len(messages) == 1
    return messages[0]


def test_control_fixture_matches_schema_and_round_trips_fragmented() -> None:
    schema = json.loads((CONTRACT_DIR / "control-message.schema.json").read_text())
    payload = json.loads((CONTRACT_DIR / "fixtures" / "hello.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    encoded = encode_control_message(payload)
    decoder = WireDecoder()

    decoded: list[ControlMessage | PCMMessage] = []
    for byte in encoded:
        decoded.extend(decoder.feed(bytes([byte])))

    assert decoded == [ControlMessage(payload=payload)]


def test_pcm_header_fixture_round_trips_with_synthetic_silence() -> None:
    expected_header = bytes.fromhex(
        (CONTRACT_DIR / "fixtures" / "pcm-header.hex").read_text().strip()
    )
    encoded = encode_pcm_message(_pcm_message())

    assert len(expected_header) == PCM_HEADER_SIZE
    assert encoded[:PCM_HEADER_SIZE] == expected_header
    decoded = _decode_one(expected_header + bytes(1_024))
    assert decoded == _pcm_message()


def test_pcm_message_projects_to_physical_output_audio_frame() -> None:
    frame = _pcm_message().to_audio_frame()

    assert frame.capture_id == CAPTURE_ID
    assert frame.source_id == str(SOURCE_ID)
    assert frame.source_kind is AudioSourceKind.PHYSICAL_OUTPUT
    assert frame.source_role is AudioSourceRole.FAR_END
    assert frame.flags is AudioFrameFlag.DISCONTINUITY


def test_decoder_handles_multiple_messages_in_one_read() -> None:
    first = encode_control_message({"type": "list_devices", "request_id": "one"})
    second = encode_pcm_message(_pcm_message())

    assert WireDecoder().feed(first + second) == [
        ControlMessage(payload={"type": "list_devices", "request_id": "one"}),
        _pcm_message(),
    ]


@pytest.mark.parametrize(
    ("offset", "replacement", "code"),
    [
        (0, b"FAIL", "invalid_magic"),
        (6, b"\x02", "unsupported_protocol"),
        (8, b"\xff", "unsupported_message_type"),
        (9, b"\x01", "invalid_header"),
        (10, b"\x00\x01", "invalid_header"),
    ],
)
def test_decoder_rejects_invalid_common_header(
    offset: int,
    replacement: bytes,
    code: str,
) -> None:
    encoded = bytearray(
        encode_control_message({"type": "list_devices", "request_id": "request"})
    )
    encoded[offset : offset + len(replacement)] = replacement

    with pytest.raises(WireProtocolError) as exc_info:
        WireDecoder().feed(encoded)

    assert exc_info.value.code == code


def test_decoder_rejects_oversized_control_before_body_arrives() -> None:
    header = struct.pack(
        ">4sHBBBBHI",
        b"VRAC",
        COMMON_HEADER_SIZE,
        1,
        0,
        WireMessageType.JSON,
        0,
        0,
        MAX_CONTROL_BODY_BYTES + 1,
    )

    with pytest.raises(WireProtocolError, match="control frame too large") as exc_info:
        WireDecoder().feed(header)

    assert exc_info.value.code == "frame_too_large"


def test_decoder_skips_known_major_future_minor_header_extension() -> None:
    payload = b'{"type":"list_devices","request_id":"future"}'
    extension = b"future!!"
    header = struct.pack(
        ">4sHBBBBHI",
        b"VRAC",
        COMMON_HEADER_SIZE + len(extension),
        1,
        1,
        WireMessageType.JSON,
        0,
        0,
        len(payload),
    )

    assert _decode_one(header + extension + payload) == ControlMessage(
        payload={"type": "list_devices", "request_id": "future"}
    )


@pytest.mark.parametrize(
    ("field_offset", "replacement", "code"),
    [
        (12, struct.pack(">I", 1_023), "invalid_pcm"),
        (68, struct.pack(">I", 48_000), "invalid_pcm_format"),
        (80, struct.pack(">I", 1_023), "invalid_pcm"),
        (76, struct.pack(">I", 1 << 16), "invalid_frame_flags"),
    ],
)
def test_decoder_rejects_invalid_pcm_header(
    field_offset: int,
    replacement: bytes,
    code: str,
) -> None:
    encoded = bytearray(encode_pcm_message(_pcm_message()))
    encoded[field_offset : field_offset + len(replacement)] = replacement

    with pytest.raises(WireProtocolError) as exc_info:
        WireDecoder().feed(encoded)

    assert exc_info.value.code == code


def test_control_validation_rejects_non_object_and_unsafe_request_id() -> None:
    non_object = b"[]"
    header = struct.pack(
        ">4sHBBBBHI",
        b"VRAC",
        COMMON_HEADER_SIZE,
        1,
        0,
        WireMessageType.JSON,
        0,
        0,
        len(non_object),
    )

    with pytest.raises(WireProtocolError, match="JSON object"):
        WireDecoder().feed(header + non_object)
    with pytest.raises(WireProtocolError, match="request_id"):
        encode_control_message({"type": "list_devices", "request_id": "x" * 65})


def test_protocol_errors_do_not_echo_untrusted_payload() -> None:
    secret = "sensitive-capture-token"
    body = ("{\"type\":\"hello\",\"capture_token\":\"" + secret).encode()
    header = struct.pack(
        ">4sHBBBBHI",
        b"VRAC",
        COMMON_HEADER_SIZE,
        1,
        0,
        WireMessageType.JSON,
        0,
        0,
        len(body),
    )

    with pytest.raises(WireProtocolError) as exc_info:
        WireDecoder().feed(header + body)

    assert secret not in str(exc_info.value)
