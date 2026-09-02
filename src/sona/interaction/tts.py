"""Pipecat 到 SpeechRail TTS 的客户端适配。"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any

from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings, assert_given
from pipecat.services.tts_service import TTSService
from pipecat.utils.tracing.service_decorators import traced_tts

from sona.config import normalize_speechrail_tts_voice
from sona.interaction.fast_clause_aggregator import ChineseClauseTextAggregator
from sona.speechrail.transport import SpeechRailProtocolError
from sona.speechrail.tts import SpeechRailTTSClient


@dataclass
class SpeechRailTTSSettings(TTSSettings):
    """SpeechRail public TTS profile selected by the interaction application."""

    speed: float = 1.0


class SpeechRailTTSService(TTSService):
    """Convert SpeechRail OpenAI Realtime PCM responses into Pipecat audio frames.

    The historical ``alloy`` alias is normalized locally until 2026-10-31;
    SpeechRail itself only receives public preset IDs.
    """

    Settings = SpeechRailTTSSettings
    _settings: SpeechRailTTSSettings

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None = None,
        client_factory: Callable[..., SpeechRailTTSClient] | None = None,
        fast_first_clause: bool = True,
        first_clause_min_chars: int = 8,
        settings: SpeechRailTTSSettings | None = None,
        **kwargs: Any,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._client_factory = client_factory or SpeechRailTTSClient
        configured = settings or self.Settings(
            model="speechrail/qwen3-tts", voice="default", language="auto"
        )
        super().__init__(
            sample_rate=24_000,
            push_start_frame=True,
            push_stop_frames=True,
            settings=configured,
            **kwargs,
        )
        if fast_first_clause:
            self._text_aggregator = ChineseClauseTextAggregator(
                aggregation_type=self._text_aggregation_mode,
                fast_first_clause=fast_first_clause,
                first_clause_min_chars=first_clause_min_chars,
            )

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        """Open one bounded speech session and emit its 24 kHz PCM chunks."""
        try:
            model = _required_string(assert_given(self._settings.model), "model")
            voice = normalize_speechrail_tts_voice(
                _required_string(assert_given(self._settings.voice), "voice")
            )
            language = _required_string(assert_given(self._settings.language), "language")
            speed = self._settings.speed
            if not 0.25 <= speed <= 4.0:
                raise ValueError("TTS speed must be between 0.25 and 4.0")
        except ValueError as exc:
            yield ErrorFrame(error=str(exc))
            return

        client = self._client_factory(
            url=self._url,
            model=model,
            voice=voice,
            language=language,
            api_key=self._api_key,
        )
        try:
            await self.start_tts_usage_metrics(text)
            async for audio in client.synthesize(text, speed=speed):
                await self.stop_ttfb_metrics()
                yield TTSAudioRawFrame(
                    audio=audio,
                    sample_rate=24_000,
                    num_channels=1,
                    context_id=context_id,
                )
        except SpeechRailProtocolError as exc:
            yield ErrorFrame(error=f"SpeechRail TTS error: {exc}")

    async def on_turn_context_completed(self) -> None:
        """Close completed SpeechRail audio contexts without the default idle wait."""
        context_id = self._turn_context_id
        if context_id is not None and self.audio_context_available(context_id):
            self._is_yielding_frames_synchronously = True
        await super().on_turn_context_completed()  # type: ignore[no-untyped-call]


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"SpeechRail TTS {field} must be configured")
    return value
