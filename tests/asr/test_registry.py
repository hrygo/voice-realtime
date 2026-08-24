"""ASR 后端注册、解析与用途能力门禁测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from voice_realtime.asr.contracts import (
    ASRCapabilities,
    ASREvent,
    ASRSessionContext,
)
from voice_realtime.asr.profiles import WLKSenseVoiceProfile
from voice_realtime.asr.registry import (
    ASRBackendRegistry,
    BackendCapabilityError,
    DuplicateBackendError,
    UnknownBackendError,
)
from voice_realtime.meeting.models import TranscriptWindow


class FakeTranscriber:
    backend_id = "wlk-sensevoice"

    def __init__(self, capabilities: ASRCapabilities) -> None:
        self.capabilities = capabilities

    @property
    def uri(self) -> str:
        return "ws://127.0.0.1/asr"

    async def connect(self) -> None:
        return

    async def send_audio(self, chunk: bytes) -> None:
        del chunk

    async def events(self) -> AsyncIterator[ASREvent]:
        if False:
            yield ASREvent(kind="ready")

    async def finish(self) -> TranscriptWindow:
        return TranscriptWindow(source_epoch=1)

    async def close(self) -> None:
        return


def _profile() -> WLKSenseVoiceProfile:
    return WLKSenseVoiceProfile(
        model_dir="runtime/sensevoice",
        language="Chinese",
        host="127.0.0.1",
        port=8001,
    )


def _capabilities(
    *,
    languages: frozenset[str] = frozenset({"Chinese"}),
    eof: bool = True,
    segment_timestamps: bool = True,
    speaker_labels: bool = True,
) -> ASRCapabilities:
    return ASRCapabilities(
        languages=languages,
        supports_partial=True,
        supports_segment_timestamps=segment_timestamps,
        supports_word_timestamps=True,
        supports_hotwords=False,
        supports_speaker_labels=speaker_labels,
        supports_native_diarization=False,
        supports_eof_flush=eof,
    )


def test_registry_rejects_duplicate_backend() -> None:
    registry = ASRBackendRegistry()

    def factory(_profile, _context) -> FakeTranscriber:
        return FakeTranscriber(_capabilities())

    registry.register_streaming("wlk-sensevoice", factory)

    with pytest.raises(DuplicateBackendError) as exc_info:
        registry.register_streaming("wlk-sensevoice", factory)

    assert exc_info.value.code == "DUPLICATE_ASR_BACKEND"


def test_registry_rejects_unknown_backend() -> None:
    registry = ASRBackendRegistry()

    with pytest.raises(UnknownBackendError) as exc_info:
        registry.create_streaming(
            _profile(),
            ASRSessionContext(source_epoch=1, offset_ms=0, purpose="subtitles"),
        )

    assert exc_info.value.code == "UNKNOWN_ASR_BACKEND"


def test_registry_rejects_language_capability_mismatch() -> None:
    registry = ASRBackendRegistry()
    registry.register_streaming(
        "wlk-sensevoice",
        lambda _profile, _context: FakeTranscriber(
            _capabilities(languages=frozenset({"English"}))
        ),
    )

    with pytest.raises(BackendCapabilityError, match="language") as exc_info:
        registry.create_streaming(
            _profile(),
            ASRSessionContext(source_epoch=1, offset_ms=0, purpose="subtitles"),
        )

    assert exc_info.value.code == "ASR_CAPABILITY_MISMATCH"


def test_registry_rejects_meeting_backend_without_eof_flush() -> None:
    registry = ASRBackendRegistry()
    registry.register_streaming(
        "wlk-sensevoice",
        lambda _profile, _context: FakeTranscriber(_capabilities(eof=False)),
    )

    with pytest.raises(BackendCapabilityError, match="EOF"):
        registry.create_streaming(
            _profile(),
            ASRSessionContext(source_epoch=1, offset_ms=0, purpose="meeting"),
        )


def test_registry_rejects_meeting_backend_without_segment_timestamps() -> None:
    registry = ASRBackendRegistry()
    registry.register_streaming(
        "wlk-sensevoice",
        lambda _profile, _context: FakeTranscriber(
            _capabilities(segment_timestamps=False)
        ),
    )

    with pytest.raises(BackendCapabilityError, match="segment timestamps"):
        registry.create_streaming(
            _profile(),
            ASRSessionContext(source_epoch=1, offset_ms=0, purpose="meeting"),
        )


def test_registry_rejects_factory_backend_identity_mismatch() -> None:
    registry = ASRBackendRegistry()
    backend = FakeTranscriber(_capabilities())
    backend.backend_id = "unexpected"
    registry.register_streaming("wlk-sensevoice", lambda _profile, _context: backend)

    with pytest.raises(BackendCapabilityError, match="identity"):
        registry.create_streaming(
            _profile(),
            ASRSessionContext(source_epoch=1, offset_ms=0, purpose="subtitles"),
        )


def test_registry_returns_capability_compatible_backend() -> None:
    registry = ASRBackendRegistry()
    expected = FakeTranscriber(_capabilities())
    registry.register_streaming("wlk-sensevoice", lambda _profile, _context: expected)

    actual = registry.create_streaming(
        _profile(),
        ASRSessionContext(source_epoch=1, offset_ms=0, purpose="meeting"),
    )

    assert actual is expected
