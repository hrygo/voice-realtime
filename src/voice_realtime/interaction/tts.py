"""Pipecat 到本机 TTS 桥的客户端适配。"""

from __future__ import annotations

from typing import Any

from pipecat.services.openai.tts import OpenAITTSService

from voice_realtime.network import local_async_client


class LocalBridgeTTSService(OpenAITTSService):
    """不走系统代理，并在管道清理时释放自有 HTTP 客户端。"""

    def __init__(self, **kwargs: Any) -> None:
        self._local_http_client = local_async_client()
        super().__init__(http_client=self._local_http_client, **kwargs)

    async def cleanup(self) -> None:
        try:
            await super().cleanup()  # type: ignore[no-untyped-call]
        finally:
            await self._local_http_client.aclose()
