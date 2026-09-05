"""普通字幕发送失败时的 PCM 缺口行为。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from sona.asr.contracts import ASRCapabilities, ASREvent, ASRSessionContext
from sona.config import SubtitleSettings
from sona.subtitles import SubtitleProxy


class ControlledTranscriber:
    backend_id = "controlled"
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

    def __init__(self, context: ASRSessionContext, behavior: str) -> None:
        self.context = context
        self.behavior = behavior
        self.closed = False
        self.sent_audio: list[bytes] = []
        self.send_started = asyncio.Event()
        self._events: asyncio.Queue[ASREvent | None] = asyncio.Queue()
        self._send_gate = asyncio.Event()

    @property
    def uri(self) -> str:
        return "ws://controlled/asr"

    async def connect(self) -> None:
        self._events.put_nowait(ASREvent(kind="ready"))

    async def send_audio(self, chunk: bytes) -> None:
        self.sent_audio.append(chunk)
        self.send_started.set()
        if self.behavior == "raise":
            raise ConnectionError("send failed")
        if self.behavior == "cancel":
            raise asyncio.CancelledError
        if self.behavior == "block":
            await self._send_gate.wait()

    async def events(self) -> AsyncIterator[ASREvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._events.put_nowait(None)


def _settings(tmp_path: Path) -> SubtitleSettings:
    return SubtitleSettings(model_dir=tmp_path, output_dir=tmp_path / "subtitles")


async def _wait_until(predicate: Callable[[], bool]) -> None:
    try:
        async with asyncio.timeout(1.0):
            for _ in range(200):
                if predicate():
                    return
                await asyncio.sleep(0.005)
    except TimeoutError:
        pytest.fail("等待普通字幕发送状态超时")
    pytest.fail("等待普通字幕发送状态超时")


async def _start_proxy(
    tmp_path: Path, behaviors: list[str]
) -> tuple[SubtitleProxy, list[ASRSessionContext], list[ControlledTranscriber]]:
    contexts: list[ASRSessionContext] = []
    streams: list[ControlledTranscriber] = []

    def factory(context: ASRSessionContext) -> ControlledTranscriber:
        contexts.append(context)
        behavior = behaviors[min(len(streams), len(behaviors) - 1)]
        stream = ControlledTranscriber(context, behavior)
        streams.append(stream)
        return stream

    proxy = SubtitleProxy(
        _settings(tmp_path),
        transcriber_factory=factory,
        backoff_delays=(0.01,),
    )
    await proxy.start()
    preparation = await proxy.prepare_browser_capture(timeout_secs=1.0)
    proxy.commit_browser_capture(preparation)
    return proxy, contexts, streams


def _gap_collector(proxy: SubtitleProxy) -> list[dict[str, object]]:
    gaps: list[dict[str, object]] = []

    async def sender(text: str) -> None:
        payload = json.loads(text)
        if payload.get("type") == "gap":
            gaps.append(payload)

    proxy.add_client(sender)
    return gaps


@pytest.mark.asyncio
async def test_send_error_counts_failed_chunk_as_one_reconnect_gap(
    tmp_path: Path,
) -> None:
    proxy, contexts, streams = await _start_proxy(tmp_path, ["raise", "ok"])
    gaps = _gap_collector(proxy)
    chunk = b"\x00" * 1024
    try:
        await proxy.push_audio(chunk)
        await _wait_until(lambda: streams[0].send_started.is_set())
        await _wait_until(lambda: len(contexts) >= 2)
        await _wait_until(lambda: len(gaps) == 1)

        assert streams[0].sent_audio == [chunk]
        assert contexts[1].offset_ms == 32
        assert gaps == [{"type": "gap", "dropped_ms": 32}]
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_cancelled_send_and_drained_queue_count_each_chunk_once(
    tmp_path: Path,
) -> None:
    proxy, contexts, streams = await _start_proxy(tmp_path, ["cancel", "ok"])
    gaps = _gap_collector(proxy)
    chunks = [b"\x00" * 1024 for _ in range(3)]
    try:
        for chunk in chunks:
            await proxy.push_audio(chunk)
        await _wait_until(lambda: streams[0].send_started.is_set())
        await _wait_until(lambda: len(contexts) >= 2)
        await _wait_until(lambda: len(gaps) == 1)

        assert streams[0].sent_audio == [chunks[0]]
        assert contexts[1].offset_ms == 96
        assert gaps == [{"type": "gap", "dropped_ms": 96}]
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_normal_stop_does_not_report_cancelled_audio_as_gap(
    tmp_path: Path,
) -> None:
    proxy, contexts, streams = await _start_proxy(tmp_path, ["block"])
    gaps = _gap_collector(proxy)
    try:
        await proxy.push_audio(b"\x00" * 1024)
        await _wait_until(lambda: streams[0].send_started.is_set())
        await proxy.stop()
        await asyncio.sleep(0.02)

        assert len(contexts) == 1
        assert gaps == []
    finally:
        if proxy.browser_capture_active:
            await proxy.stop()


@pytest.mark.asyncio
async def test_user_clear_does_not_report_cancelled_audio_as_gap(
    tmp_path: Path,
) -> None:
    proxy, contexts, streams = await _start_proxy(tmp_path, ["block", "ok"])
    gaps = _gap_collector(proxy)
    try:
        await proxy.push_audio(b"\x00" * 1024)
        await _wait_until(lambda: streams[0].send_started.is_set())
        await proxy.clear_subtitles()
        await _wait_until(lambda: len(contexts) >= 2)
        await asyncio.sleep(0.02)

        assert gaps == []
    finally:
        await proxy.stop()
