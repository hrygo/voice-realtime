"""Pipecat 到本机 TTS 桥的客户端适配。"""

from __future__ import annotations

from typing import Any

from pipecat.services.openai.tts import OpenAITTSService

from voice_realtime.interaction.fast_clause_aggregator import ChineseClauseTextAggregator
from voice_realtime.network import local_async_client


class LocalBridgeTTSService(OpenAITTSService):
    """不走系统代理，支持中文首句极速分词弱标点加速，并在管道清理时释放自有 HTTP 客户端。"""

    def __init__(
        self,
        *,
        fast_first_clause: bool = True,
        first_clause_min_chars: int = 8,
        **kwargs: Any,
    ) -> None:
        self._local_http_client = local_async_client()
        super().__init__(http_client=self._local_http_client, **kwargs)
        if fast_first_clause:
            self._text_aggregator = ChineseClauseTextAggregator(
                aggregation_type=self._text_aggregation_mode,
                fast_first_clause=fast_first_clause,
                first_clause_min_chars=first_clause_min_chars,
            )

    async def cleanup(self) -> None:
        try:
            await super().cleanup()  # type: ignore[no-untyped-call]
        finally:
            await self._local_http_client.aclose()

    async def on_turn_context_completed(self) -> None:
        """本地 HTTP 请求结束后立即封闭音频上下文，不等待 Pipecat idle timeout。"""
        context_id = self._turn_context_id
        if context_id is not None and self.audio_context_available(context_id):
            # Pipecat 1.7.0 会用最后一次 TTS 请求的结果覆盖这个状态：若前序已有
            # 音频、末尾请求却只返回 ErrorFrame/零音频，状态会变回 False，导致
            # 已播放完的轮次额外等待默认 3s。LocalBridge 是同步 HTTP 流；只要
            # 上下文已经创建，LLM turn 完成时即可安全排入唯一停止帧和结束哨兵。
            self._is_yielding_frames_synchronously = True
        await super().on_turn_context_completed()  # type: ignore[no-untyped-call]
