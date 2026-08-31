"""Pipecat STT adapter that commits one SpeechRail v2 session per VAD turn."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_realtime.asr.adapters.speechrail_realtime import SpeechRailRealtimeClient
from voice_realtime.asr.contracts import ASRCapabilities


class _SpeechRailClient(Protocol):
    async def connect(self, *, language: str) -> None: ...

    async def append_pcm(self, chunk: bytes) -> None: ...

    async def commit(self) -> None: ...

    async def receive(self) -> dict[str, object]: ...

    async def close(self) -> None: ...


ClientFactory = Callable[[], _SpeechRailClient]


class SpeechRailConversationSTTProcessor(FrameProcessor):
    """Converts VAD-bounded PCM turns into Pipecat transcription frames."""

    def __init__(self, *, language: str, client_factory: ClientFactory) -> None:
        super().__init__(name="speechrail-realtime-v2-stt")
        self._language = language
        self._client_factory = client_factory
        self._client: _SpeechRailClient | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            await self._open_turn()
        elif isinstance(frame, InputAudioRawFrame):
            await self._append_audio(frame)
            return
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            await self._commit_turn()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self._close_turn()
        await self.push_frame(frame, direction)

    async def _open_turn(self) -> None:
        if self._client is not None:
            return
        self._client = self._client_factory()
        await self._client.connect(language=self._language)

    async def _append_audio(self, frame: InputAudioRawFrame) -> None:
        if frame.sample_rate != 16_000 or frame.num_channels != 1 or len(frame.audio) % 2:
            raise ValueError("SpeechRail conversation STT requires 16 kHz mono PCM16")
        if self._client is not None:
            await self._client.append_pcm(frame.audio)

    async def _commit_turn(self) -> None:
        if self._client is None:
            return
        client = self._client
        await client.commit()
        try:
            while True:
                event = await client.receive()
                event_type = event.get("type")
                text = event.get("text")
                if event_type == "input_audio_buffer.ack":
                    continue
                if event_type == "transcription.delta" and isinstance(text, str):
                    await self.push_frame(
                        InterimTranscriptionFrame(text, "user", _timestamp()),
                        FrameDirection.DOWNSTREAM,
                    )
                elif event_type == "transcription.completed" and isinstance(text, str):
                    await self.push_frame(
                        TranscriptionFrame(text, "user", _timestamp(), finalized=True),
                        FrameDirection.DOWNSTREAM,
                    )
                    return
                elif event_type == "error":
                    raise RuntimeError("SPEECHRAIL_REQUEST_FAILED")
                elif event_type == "session.completed":
                    raise RuntimeError("SPEECHRAIL_FINAL_MISSING")
                else:
                    raise RuntimeError("SPEECHRAIL_PROTOCOL_ERROR")
        finally:
            await client.close()
            self._client = None

    async def _close_turn(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


class SpeechRailConversationSTTFactory:
    """Opt-in factory for the voice-assistant Pipecat STT boundary."""

    backend_id = "speechrail-realtime-v2"
    capabilities = ASRCapabilities(
        languages=frozenset({"zh", "en", "Chinese", "English"}),
        supports_partial=True,
        supports_segment_timestamps=True,
        supports_word_timestamps=False,
        supports_hotwords=False,
        supports_speaker_labels=False,
        supports_native_diarization=False,
        supports_eof_flush=True,
    )

    def __init__(self, *, url: str, client_factory: ClientFactory | None = None) -> None:
        self._url = url
        self._client_factory = client_factory or (
            lambda: SpeechRailRealtimeClient(url=self._url)
        )

    def create_processor(self, *, sample_rate: int, language: str) -> FrameProcessor:
        if sample_rate != 16_000:
            raise ValueError("SpeechRail conversation STT requires 16 kHz PCM")
        return SpeechRailConversationSTTProcessor(
            language=language,
            client_factory=self._client_factory,
        )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()
