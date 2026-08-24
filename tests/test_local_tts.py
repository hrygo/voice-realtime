"""本机 TTS 客户端的代理隔离与生命周期。"""

import asyncio

import pytest
from pipecat.frames.frames import TTSStoppedFrame

from voice_realtime.interaction.tts import LocalBridgeTTSService


async def test_local_tts_client_is_direct_and_closed_on_cleanup() -> None:
    service = LocalBridgeTTSService(
        api_key="local",
        base_url="http://127.0.0.1:8765/v1",
        settings=LocalBridgeTTSService.Settings(voice="alloy"),
    )

    assert service._local_http_client._mounts == {}
    await service.cleanup()
    assert service._local_http_client.is_closed


@pytest.mark.parametrize("last_request_had_audio", [True, False])
async def test_local_http_turn_closes_context_without_idle_timeout(
    last_request_had_audio: bool,
) -> None:
    """末尾请求零音频时，也必须显式结束此前已经开始的本地 HTTP TTS 轮次。"""
    service = LocalBridgeTTSService(
        api_key="local",
        base_url="http://127.0.0.1:8765/v1",
        settings=LocalBridgeTTSService.Settings(voice="alloy"),
    )
    context_id = "local-http-turn"
    queue: asyncio.Queue[object] = asyncio.Queue()
    service._turn_context_id = context_id
    service._audio_contexts[context_id] = queue  # type: ignore[assignment]
    service._is_yielding_frames_synchronously = last_request_had_audio

    await service.on_turn_context_completed()

    stop_frame = queue.get_nowait()
    end_of_context = queue.get_nowait()
    assert isinstance(stop_frame, TTSStoppedFrame)
    assert stop_frame.context_id == context_id
    assert end_of_context is None
    assert queue.empty()
    assert service._turn_context_id is None
    await service.cleanup()
