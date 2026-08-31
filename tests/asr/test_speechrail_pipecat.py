from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from pipecat.frames.frames import (
    InputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_realtime.asr.adapters.speechrail_pipecat import SpeechRailConversationSTTProcessor


class FakeSpeechRailClient:
    def __init__(self) -> None:
        self.appended: list[bytes] = []
        self.commits = 0
        self.closed = False
        self._events = iter(
            (
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
            isinstance(frame, TranscriptionFrame) and frame.text == "你好世界"
            for frame in emitted
        )

    asyncio.run(scenario())
