"""Pipecat STT adapter that commits one SpeechRail OpenAI Realtime session per VAD turn."""

from __future__ import annotations

import contextlib
import logging
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

from sona.asr.contracts import ASRCapabilities
from sona.speechrail.transcription_events import (
    Noop,
    SpeechRailTranscriptionError,
    TranscriptionCompleted,
    TranscriptionDelta,
    TranscriptionSegment,
    decode_transcription_event,
)
from sona.speechrail.transport import SpeechRailProtocolError, SpeechRailRealtimeClient

logger = logging.getLogger(__name__)

__all__ = [
    "ClientFactory",
    "SpeechRailConversationSTTFactory",
    "SpeechRailConversationSTTProcessor",
]


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
        super().__init__(name="speechrail-openai-realtime-stt")
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
        client = self._client_factory()
        try:
            await client.connect(language=self._language)
        except Exception as exc:
            with contextlib.suppress(Exception):
                await client.close()
            self._client = None
            logger.warning(
                "交互 STT: SpeechRail 实时会话连接失败 (language=%s): %s",
                self._language,
                exc,
            )
            raise
        self._client = client
        logger.debug("交互 STT: SpeechRail 实时会话已建立 (language=%s)", self._language)

    async def _append_audio(self, frame: InputAudioRawFrame) -> None:
        if frame.sample_rate != 16_000 or frame.num_channels != 1 or len(frame.audio) % 2:
            raise ValueError("SpeechRail conversation STT requires 16 kHz mono PCM16")
        if self._client is None:
            return
        try:
            await self._client.append_pcm(frame.audio)
        except Exception as exc:
            logger.warning("交互 STT: SpeechRail 发送音频失败，重置本回合会话: %s", exc)
            with contextlib.suppress(Exception):
                await self._client.close()
            self._client = None
            raise

    async def _commit_turn(self) -> None:
        if self._client is None:
            return
        client = self._client
        try:
            await client.commit()
            while True:
                event = await client.receive()
                try:
                    decoded = decode_transcription_event(event)
                except SpeechRailProtocolError:
                    logger.warning(
                        "交互 STT: SpeechRail 事件解析失败 (type=%s)",
                        event.get("type"),
                    )
                    raise RuntimeError("SPEECHRAIL_PROTOCOL_ERROR") from None
                if isinstance(decoded, Noop):
                    continue
                if isinstance(decoded, TranscriptionDelta):
                    if decoded.text.strip():
                        logger.debug("交互 STT: 中间转写: %s", decoded.text)
                    await self.push_frame(
                        InterimTranscriptionFrame(decoded.text, "user", _timestamp()),
                        FrameDirection.DOWNSTREAM,
                    )
                elif isinstance(decoded, TranscriptionCompleted):
                    if decoded.transcript.strip():
                        logger.info("交互 STT: 转写完成: %s", decoded.transcript)
                    await self.push_frame(
                        TranscriptionFrame(
                            decoded.transcript,
                            "user",
                            _timestamp(),
                            finalized=True,
                        ),
                        FrameDirection.DOWNSTREAM,
                    )
                    return
                elif isinstance(decoded, TranscriptionSegment):
                    continue
                elif isinstance(decoded, SpeechRailTranscriptionError):
                    logger.error(
                        "交互 STT: SpeechRail 转写失败 code=%s message=%s",
                        decoded.code,
                        decoded.message or "(no message)",
                    )
                    raise RuntimeError(
                        f"SPEECHRAIL_REQUEST_FAILED code={decoded.code} "
                        f"message={decoded.message}"
                    ) from None
                else:
                    logger.error("交互 STT: 未知转写事件 %r", type(decoded).__name__)
                    raise RuntimeError("SPEECHRAIL_PROTOCOL_ERROR")
        finally:
            await client.close()
            self._client = None
            logger.debug("交互 STT: SpeechRail 实时会话已关闭 (语言 %s)", self._language)

    async def _close_turn(self) -> None:
        if self._client is not None:
            logger.debug("交互 STT: SpeechRail 会话未完成即关闭")
            await self._client.close()
            self._client = None


class SpeechRailConversationSTTFactory:
    """Opt-in factory for the voice-assistant Pipecat STT boundary."""

    backend_id = "speechrail-openai-realtime"
    capabilities = ASRCapabilities(
        languages=frozenset({"zh", "en", "Chinese", "English"}),
        supports_partial=True,
        supports_segment_timestamps=False,
        supports_word_timestamps=False,
        supports_hotwords=False,
        supports_speaker_labels=False,
        supports_native_diarization=False,
        supports_eof_flush=True,
    )

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._client_factory = client_factory or (
            lambda: SpeechRailRealtimeClient(url=self._url, api_key=self._api_key)
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
