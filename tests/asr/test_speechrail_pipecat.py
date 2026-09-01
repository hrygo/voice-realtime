from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_realtime.asr.adapters.speechrail_pipecat import SpeechRailConversationSTTProcessor
from voice_realtime.speechrail.transcription_events import (
    TranscriptionCompleted,
    TranscriptionDelta,
    decode_transcription_event,
)


class FakeSpeechRailClient:
    def __init__(self) -> None:
        self.appended: list[bytes] = []
        self.commits = 0
        self.closed = False
        self._events = iter(
            (
                {"type": "input_audio_buffer.ack"},
                {"type": "transcription.delta", "text": "你好"},
                {
                    "type": "transcription.completed",
                    "text": "你好世界",
                    "segments": [],
                },
            )
        )

    async def connect(self, *, language: str) -> None:
        assert language == "zh"

    async def append_pcm(self, chunk: bytes) -> None:
        self.appended.append(chunk)

    async def commit(self) -> None:
        self.commits += 1

    async def receive(self) -> dict[str, object]:
        return next(self._events)

    async def close(self) -> None:
        self.closed = True


def test_pipecat_processor_commits_one_v2_session_per_vad_turn() -> None:
    async def scenario() -> None:
        client = FakeSpeechRailClient()
        processor = SpeechRailConversationSTTProcessor(
            language="zh",
            client_factory=lambda: client,
        )
        emitted: list[object] = []

        async def capture(frame: object, _direction: object) -> None:
            emitted.append(frame)

        processor.push_frame = capture  # type: ignore[method-assign]
        with patch.object(FrameProcessor, "process_frame", new=AsyncMock()):
            await processor.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
            await processor.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
            await processor.process_frame(
                InputAudioRawFrame(b"\x00\x00", sample_rate=16_000, num_channels=1),
                FrameDirection.DOWNSTREAM,
            )
            await processor.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

        assert client.appended == [b"\x00\x00"]
        assert client.commits == 1
        assert any(
            isinstance(frame, TranscriptionFrame) and frame.text == "你好世界" for frame in emitted
        )

    asyncio.run(scenario())


def test_pipecat_processor_fails_and_closes_the_turn_on_speechrail_error() -> None:
    class ErrorClient(FakeSpeechRailClient):
        def __init__(self) -> None:
            super().__init__()
            self._events = iter(({"type": "error", "error": {"code": "speechrail_error"}},))

    async def scenario() -> None:
        client = ErrorClient()
        processor = SpeechRailConversationSTTProcessor(
            language="zh",
            client_factory=lambda: client,
        )

        await processor._open_turn()
        with pytest.raises(RuntimeError, match="SPEECHRAIL_REQUEST_FAILED"):
            await processor._commit_turn()

        assert client.closed is True

    asyncio.run(scenario())


def test_pipecat_processor_matches_decoder_for_delta_and_completed() -> None:
    async def scenario() -> None:
        delta_raw = {"type": "transcription.delta", "text": "你好"}
        completed_raw = {"type": "transcription.completed", "text": "你好世界", "segments": []}
        decoded_delta = decode_transcription_event(delta_raw)
        decoded_completed = decode_transcription_event(completed_raw)
        assert isinstance(decoded_delta, TranscriptionDelta)
        assert isinstance(decoded_completed, TranscriptionCompleted)

        client = FakeSpeechRailClient()
        client._events = iter((delta_raw, completed_raw))
        processor = SpeechRailConversationSTTProcessor(
            language="zh",
            client_factory=lambda: client,
        )
        emitted: list[object] = []

        async def capture(frame: object, _direction: object) -> None:
            emitted.append(frame)

        processor.push_frame = capture  # type: ignore[method-assign]
        with patch.object(FrameProcessor, "process_frame", new=AsyncMock()):
            await processor.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
            await processor.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
            await processor.process_frame(
                InputAudioRawFrame(b"\x00\x00", sample_rate=16_000, num_channels=1),
                FrameDirection.DOWNSTREAM,
            )
            await processor.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

        interim = next(frame for frame in emitted if isinstance(frame, InterimTranscriptionFrame))
        final = next(frame for frame in emitted if isinstance(frame, TranscriptionFrame))
        assert interim.text == decoded_delta.text
        assert final.text == decoded_completed.text
        assert final.finalized is True

    asyncio.run(scenario())


def test_pipecat_processor_validates_segments_via_shared_decoder() -> None:
    class BadSegmentsClient(FakeSpeechRailClient):
        def __init__(self) -> None:
            super().__init__()
            self._events = iter(
                (
                    {
                        "type": "transcription.completed",
                        "text": "你好世界",
                        "segments": [{"start_ms": 0, "end_ms": 100, "text": ""}],
                    },
                )
            )

    async def scenario() -> None:
        client = BadSegmentsClient()
        processor = SpeechRailConversationSTTProcessor(
            language="zh",
            client_factory=lambda: client,
        )

        await processor._open_turn()
        with pytest.raises(RuntimeError, match="SPEECHRAIL_PROTOCOL_ERROR"):
            await processor._commit_turn()

        assert client.closed is True

    asyncio.run(scenario())


def test_pipecat_processor_raises_on_session_completed() -> None:
    class SessionEndedClient(FakeSpeechRailClient):
        def __init__(self) -> None:
            super().__init__()
            self._events = iter(({"type": "session.completed"},))

    async def scenario() -> None:
        client = SessionEndedClient()
        processor = SpeechRailConversationSTTProcessor(
            language="zh",
            client_factory=lambda: client,
        )

        await processor._open_turn()
        with pytest.raises(RuntimeError, match="SPEECHRAIL_FINAL_MISSING"):
            await processor._commit_turn()

        assert client.closed is True

    asyncio.run(scenario())


def test_pipecat_processor_raises_on_unknown_event_type() -> None:
    class UnknownClient(FakeSpeechRailClient):
        def __init__(self) -> None:
            super().__init__()
            self._events = iter(({"type": "some.future.event"},))

    async def scenario() -> None:
        client = UnknownClient()
        processor = SpeechRailConversationSTTProcessor(
            language="zh",
            client_factory=lambda: client,
        )

        await processor._open_turn()
        with pytest.raises(RuntimeError, match="SPEECHRAIL_PROTOCOL_ERROR"):
            await processor._commit_turn()

        assert client.closed is True

    asyncio.run(scenario())


def test_pipecat_processor_raises_on_diarization_event() -> None:
    class DiarizationClient(FakeSpeechRailClient):
        def __init__(self) -> None:
            super().__init__()
            self._events = iter(({"type": "transcription.diarization.completed", "mapping": {}},))

    async def scenario() -> None:
        client = DiarizationClient()
        processor = SpeechRailConversationSTTProcessor(
            language="zh",
            client_factory=lambda: client,
        )

        await processor._open_turn()
        with pytest.raises(RuntimeError, match="SPEECHRAIL_PROTOCOL_ERROR"):
            await processor._commit_turn()

        assert client.closed is True

    asyncio.run(scenario())
