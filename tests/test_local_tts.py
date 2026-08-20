"""本机 TTS 客户端的代理隔离与生命周期。"""

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
