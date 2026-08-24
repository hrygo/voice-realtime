"""SubtitleProxy 通过统一 StreamingTranscriber 契约工作。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

from voice_realtime.asr.contracts import (
    ASRCapabilities,
    ASREvent,
    ASRSessionContext,
)
from voice_realtime.config import SubtitleSettings
from voice_realtime.meeting.models import NormalizedSegment, TranscriptWindow
from voice_realtime.ui.subtitle_proxy import SubtitleProxy


class FakeTranscriber:
    backend_id = "fake"
    capabilities = ASRCapabilities(
        languages=frozenset({"Chinese"}),
        supports_partial=True,
        supports_segment_timestamps=True,
        supports_word_timestamps=True,
        supports_hotwords=False,
        supports_speaker_labels=True,
        supports_native_diarization=False,
        supports_eof_flush=True,
    )

    def __init__(self, window: TranscriptWindow) -> None:
        self.window = window
        self.closed = False
        self._events: asyncio.Queue[ASREvent] = asyncio.Queue()

    @property
    def uri(self) -> str:
        return "ws://fake/asr"

    async def connect(self) -> None:
        self._events.put_nowait(ASREvent(kind="ready"))
        self._events.put_nowait(ASREvent(kind="snapshot", window=self.window))

    async def send_audio(self, chunk: bytes) -> None:
        del chunk

    async def events(self) -> AsyncIterator[ASREvent]:
        while not self.closed:
            yield await self._events.get()

    async def finish(self) -> TranscriptWindow:
        self._events.put_nowait(ASREvent(kind="final", window=self.window))
        return self.window

    async def close(self) -> None:
        self.closed = True


async def test_proxy_broadcasts_domain_snapshot_through_legacy_presenter(
    tmp_path: Path,
) -> None:
    settings = SubtitleSettings(
        model_dir=tmp_path,
        output_dir=tmp_path / "subtitles",
    )
    window = TranscriptWindow(
        source_epoch=0,
        partial="下一句",
        segments=(
            NormalizedSegment(
                order=0,
                source_epoch=0,
                speaker_key="epoch:0:speaker:2",
                start_ms=1000,
                end_ms=2500,
                text="第一句",
                detected_language="zh",
            ),
        ),
    )
    contexts: list[ASRSessionContext] = []

    def factory(context: ASRSessionContext) -> FakeTranscriber:
        contexts.append(context)
        return FakeTranscriber(window)

    proxy = SubtitleProxy(settings, transcriber_factory=factory)
    client = AsyncMock()
    proxy.add_client(client)

    await proxy.start()
    await asyncio.sleep(0.02)

    payload = json.loads(client.await_args.args[0])
    assert payload["type"] == "full_update"
    assert payload["buffer_transcription"] == "下一句"
    assert payload["lines"][0]["speaker"] == 2
    assert contexts == [ASRSessionContext(source_epoch=0, offset_ms=0, purpose="subtitles")]
    await proxy.stop()
