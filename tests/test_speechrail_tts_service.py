"""SpeechRail Realtime v2 到 Pipecat TTS 的边界适配。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from pipecat.frames.frames import TTSAudioRawFrame

from voice_realtime.interaction.tts import SpeechRailTTSService


class FakeSpeechRailTTSClient:
    def __init__(self, *, block_after_audio: bool = False) -> None:
        self.requests: list[tuple[str, float]] = []
        self.cancelled = False
        self._block_after_audio = block_after_audio

    async def synthesize(self, text: str, *, speed: float) -> AsyncIterator[bytes]:
        self.requests.append((text, speed))
        yield b"\x01\x00"
        if not self._block_after_audio:
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_speechrail_tts_service_normalizes_legacy_voice_and_yields_pcm() -> None:
    client = FakeSpeechRailTTSClient()
    factory_calls: list[dict[str, str]] = []

    def client_factory(**kwargs: str) -> FakeSpeechRailTTSClient:
        factory_calls.append(kwargs)
        return client

    service = SpeechRailTTSService(
        url="ws://speechrail.test/v2/realtime",
        client_factory=client_factory,
        settings=SpeechRailTTSService.Settings(
            model="speechrail/qwen3-tts", voice="alloy", language="zh"
        ),
    )

    frames = [frame async for frame in service.run_tts("你好", "turn-1")]

    assert factory_calls == [
        {
            "url": "ws://speechrail.test/v2/realtime",
            "model": "speechrail/qwen3-tts",
            "voice": "default",
            "language": "zh",
            "api_key": None,
        }
    ]
    assert client.requests == [("你好", 1.0)]
    assert len(frames) == 1
    assert isinstance(frames[0], TTSAudioRawFrame)
    assert frames[0].audio == b"\x01\x00"
    assert frames[0].sample_rate == 24_000
    assert frames[0].num_channels == 1
    assert frames[0].context_id == "turn-1"
    await service.cleanup()


async def test_speechrail_tts_service_propagates_pipeline_task_cancellation_to_client() -> None:
    client = FakeSpeechRailTTSClient(block_after_audio=True)
    service = SpeechRailTTSService(
        url="ws://speechrail.test/v2/realtime",
        client_factory=lambda **_: client,
        settings=SpeechRailTTSService.Settings(
            model="speechrail/qwen3-tts", voice="warm", language="auto"
        ),
    )

    async def consume() -> None:
        async for _frame in service.run_tts("请停止", "turn-2"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.cancelled
    await service.cleanup()
