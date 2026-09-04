"""SubtitleProxy 职责拆分前的行为保护测试。

这些测试锁定 façade 当前的可观察行为，供后续组件提取时逐项保持。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from sona.asr.contracts import ASRCapabilities, ASREvent, ASRSessionContext
from sona.asr.models import ASRSegment, ASRWindow
from sona.asr.presenters import legacy_subtitle_payload
from sona.config import SubtitleSettings
from sona.subtitles import (
    FinalizationTimeoutError,
    SubtitleProxy,
    TranscriptionGap,
)


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

    def __init__(self, *, source_epoch: int) -> None:
        self.source_epoch = source_epoch
        self.closed = False
        self.connected = False
        self.sent_audio: list[bytes] = []
        self.commits = 0
        self._events: asyncio.Queue[ASREvent] = asyncio.Queue()
        self._finish_gate = asyncio.Event()
        self._finish_result: ASRWindow | None = None
        self._end_stream = False
        self._send_gate: asyncio.Event | None = None

    @property
    def uri(self) -> str:
        return "ws://fake/asr"

    async def connect(self) -> None:
        self.connected = True
        self._events.put_nowait(ASREvent(kind="ready"))

    async def send_audio(self, chunk: bytes) -> None:
        self.sent_audio.append(chunk)
        if self._send_gate is not None:
            await self._send_gate.wait()

    async def events(self) -> AsyncIterator[ASREvent]:
        while not self.closed:
            try:
                yield await asyncio.wait_for(self._events.get(), timeout=0.05)
            except TimeoutError:
                if self._end_stream:
                    return

    async def finish(self) -> ASRWindow:
        self.commits += 1
        if self._finish_result is not None:
            return self._finish_result
        await self._finish_gate.wait()
        return self._finish_result or ASRWindow(source_epoch=self.source_epoch)

    async def close(self) -> None:
        self.closed = True

    def emit(self, event: ASREvent) -> None:
        self._events.put_nowait(event)


def _settings(tmp_path: Path) -> SubtitleSettings:
    return SubtitleSettings(
        model_dir=tmp_path,
        output_dir=tmp_path / "subtitles",
    )


class ShortLivedTranscriber(FakeTranscriber):
    """连接成功后立即结束流，模拟 worker 未就绪时的秒断握手。"""

    def __init__(self, *, source_epoch: int) -> None:
        super().__init__(source_epoch=source_epoch)
        self._end_stream = True


def _flapping_proxy(
    tmp_path: Path,
    *,
    probes: list[bool] | None = None,
    backoff: Sequence[float] = (0.01, 0.02),
    stable_reset_after_secs: float | None = None,
) -> SubtitleProxy:
    created: list[FakeTranscriber] = []

    def factory(_ctx: ASRSessionContext) -> FakeTranscriber:
        transcriber = ShortLivedTranscriber(source_epoch=1 + len(created))
        created.append(transcriber)
        return transcriber

    probe_results = list(probes) if probes is not None else []

    async def probe() -> bool:
        return probe_results.pop(0) if probe_results else True

    proxy = SubtitleProxy(
        _settings(tmp_path),
        transcriber_factory=factory,
        backoff_delays=backoff,
        readiness_probe=probe,
        stable_reset_after_secs=stable_reset_after_secs,
    )
    proxy._created = created  # type: ignore[attr-defined]
    return proxy


def _proxy(tmp_path: Path) -> SubtitleProxy:
    return SubtitleProxy(
        _settings(tmp_path),
        transcriber_factory=lambda _ctx: FakeTranscriber(source_epoch=1),
    )


def _window(*, partial: str = "", with_segment: bool = False) -> ASRWindow:
    segments = (
        (
            ASRSegment(
                order=0,
                source_epoch=1,
                speaker_key="epoch:1:speaker:1",
                start_ms=0,
                end_ms=1000,
                text="你好世界",
            ),
        )
        if with_segment
        else ()
    )
    return ASRWindow(source_epoch=1, partial=partial, segments=segments)


async def test_slow_client_only_suffers_its_own_bounded_queue(
    tmp_path: Path,
) -> None:
    proxy = _proxy(tmp_path)
    await proxy.start()
    fast_calls: list[str] = []
    blocked = asyncio.Event()

    async def fast_sender(text: str) -> None:
        fast_calls.append(text)

    async def slow_sender(text: str) -> None:
        await blocked.wait()

    proxy.add_client(fast_sender)
    proxy.add_client(slow_sender)
    for index in range(12):
        await proxy._broadcast_untracked({"type": "tick", "i": index})

    await asyncio.sleep(0.05)
    assert len(fast_calls) == 12
    slow_channel = proxy._client_hub._clients[slow_sender]
    assert slow_channel.queue.maxsize == 8
    assert slow_channel.queue.qsize() == 8

    proxy.remove_client(slow_sender)
    await asyncio.sleep(0.02)
    assert slow_sender not in proxy._client_hub._clients
    assert proxy.has_clients
    await proxy.stop()


async def test_late_subscriber_immediately_receives_current_snapshot(
    tmp_path: Path,
) -> None:
    proxy = _proxy(tmp_path)
    await proxy.start()
    payload = legacy_subtitle_payload(_window(partial="快照", with_segment=True))
    await proxy._broadcast_payload(payload)

    received: list[str] = []

    async def sender(text: str) -> None:
        received.append(text)

    proxy.add_client(sender)
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert json.loads(received[0]) == payload
    await proxy.stop()


async def test_browser_disconnect_does_not_close_meeting_capture(
    tmp_path: Path,
) -> None:
    proxy = _proxy(tmp_path)
    await proxy.start()
    preparation = await proxy.prepare_capture("meeting-1", timeout_secs=1.0)
    proxy.commit_capture(preparation)
    assert proxy.capture_owner == "meeting-1"

    async def sender(text: str) -> None:
        return None

    proxy.add_client(sender)
    proxy.remove_client(sender)

    assert proxy.capture_owner == "meeting-1"
    assert proxy.is_paused is False
    await proxy.abort_capture()
    await proxy.stop()


async def test_browser_and_meeting_preparation_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    proxy = _proxy(tmp_path)
    await proxy.start()
    preparation = await proxy.prepare_capture("meeting-1", timeout_secs=1.0)

    with pytest.raises(RuntimeError, match="会议采集租约"):
        await proxy.prepare_browser_capture(timeout_secs=1.0)

    proxy.commit_capture(preparation)
    with pytest.raises(RuntimeError, match="会议采集租约"):
        await proxy.prepare_browser_capture(timeout_secs=1.0)
    await proxy.abort_capture()

    browser_preparation = await proxy.prepare_browser_capture(timeout_secs=1.0)
    proxy.commit_browser_capture(browser_preparation)
    capture_preparation = await proxy.prepare_capture("meeting-2", timeout_secs=1.0)
    with pytest.raises(RuntimeError, match="普通字幕仍处于活动状态"):
        proxy.commit_capture(capture_preparation)
    await proxy.abort_prepared_capture(capture_preparation)
    await proxy.deactivate_browser_capture()
    await proxy.stop()


async def test_capture_reconnect_increments_epoch_and_reports_gap(
    tmp_path: Path,
) -> None:
    first = FakeTranscriber(source_epoch=1)
    second = FakeTranscriber(source_epoch=2)
    first._send_gate = asyncio.Event()
    created: list[ASRSessionContext] = []

    def factory(context: ASRSessionContext) -> FakeTranscriber:
        created.append(context)
        return first if context.source_epoch == 1 else second

    gaps: list[TranscriptionGap] = []
    proxy = SubtitleProxy(
        _settings(tmp_path),
        transcriber_factory=factory,
        backoff_delays=(0.01,),
    )
    await proxy.start()
    proxy.add_gap_listener(gaps.append)
    preparation = await proxy.prepare_capture("meeting-1", timeout_secs=1.0)
    proxy.commit_capture(preparation)
    first._end_stream = True
    await proxy.push_audio(b"\x00\x00" * 1600)

    await asyncio.sleep(0.4)
    assert proxy.capture_epoch == 2
    assert gaps == [TranscriptionGap(source_epoch=2, start_ms=0, end_ms=100)]
    assert created[1].source_epoch == 2
    await proxy.stop()


async def test_finish_timeout_carries_last_window(tmp_path: Path) -> None:
    transcriber = FakeTranscriber(source_epoch=1)
    proxy = SubtitleProxy(_settings(tmp_path), transcriber_factory=lambda _ctx: transcriber)
    await proxy.start()
    preparation = await proxy.prepare_capture("meeting-1", timeout_secs=1.0)
    proxy.commit_capture(preparation)

    await proxy._capture_session._handle_event(
        ASREvent(kind="snapshot", window=_window(partial="最后一句", with_segment=True))
    )

    with pytest.raises(FinalizationTimeoutError) as excinfo:
        await proxy.finish_capture(timeout_secs=0.05)
    assert excinfo.value.last_window is not None
    assert excinfo.value.last_window.partial == "最后一句"
    await proxy.stop()


async def test_meeting_final_event_notifies_listeners(tmp_path: Path) -> None:
    """会议采集期间每个 server_vad 回合的 final 窗口必须转发监听器增量入库。"""
    transcriber = FakeTranscriber(source_epoch=1)
    proxy = SubtitleProxy(_settings(tmp_path), transcriber_factory=lambda _ctx: transcriber)
    await proxy.start()
    preparation = await proxy.prepare_capture("meeting-1", timeout_secs=1.0)
    proxy.commit_capture(preparation)

    received: list[ASRWindow] = []

    async def listener(window: ASRWindow) -> None:
        received.append(window)

    proxy.add_event_listener(listener)
    await proxy._capture_session._handle_event(
        ASREvent(kind="final", window=_window(partial="", with_segment=True))
    )

    assert len(received) == 1
    assert received[0].segments[0].text == "你好世界"
    await proxy.abort_capture()
    await proxy.stop()


async def test_stop_is_idempotent(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path)
    await proxy.start()
    await proxy.stop()
    await proxy.stop()
    await proxy.start()
    await proxy.stop()


async def test_abort_capture_is_idempotent_without_lease(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path)
    await proxy.start()
    await proxy.abort_capture()
    await proxy.abort_capture()
    await proxy.stop()


async def test_partial_only_does_not_write_srt(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path)
    await proxy.start()
    await proxy._broadcast_payload(
        legacy_subtitle_payload(_window(partial="识别中")), persist=True
    )

    assert not (tmp_path / "subtitles" / "current.srt").exists()
    await proxy.stop()


async def test_duplicate_confirmed_snapshot_does_not_rewrite_srt(
    tmp_path: Path,
) -> None:
    proxy = _proxy(tmp_path)
    await proxy.start()
    payload = legacy_subtitle_payload(_window(with_segment=True))
    await proxy._broadcast_payload(payload, persist=True)
    current = tmp_path / "subtitles" / "current.srt"
    first_ns = current.stat().st_mtime_ns

    await proxy._broadcast_payload(payload, persist=True)
    assert current.stat().st_mtime_ns == first_ns
    await proxy.stop()


async def test_srt_persist_uses_atomic_replace(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path)
    await proxy.start()
    await proxy._broadcast_payload(
        legacy_subtitle_payload(_window(with_segment=True)), persist=True
    )
    current = tmp_path / "subtitles" / "current.srt"

    assert current.is_file()
    assert not (tmp_path / "subtitles" / "current.srt.tmp").exists()
    assert "你好世界" in current.read_text(encoding="utf-8")
    await proxy.stop()


async def test_close_epoch_archives_srt_only_once(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path)
    await proxy.start()
    await proxy._subtitle_session._open_epoch()
    await proxy._broadcast_payload(
        legacy_subtitle_payload(_window(with_segment=True)), persist=True
    )

    await proxy._subtitle_session._close_epoch()
    archives = sorted((tmp_path / "subtitles").glob("session-*.srt"))
    assert len(archives) == 1

    await proxy._subtitle_session._close_epoch()
    assert len(sorted((tmp_path / "subtitles").glob("session-*.srt"))) == 1
    await proxy.stop()


async def test_archive_filename_conflict_uses_numeric_suffix(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path)
    await proxy.start()
    payload = legacy_subtitle_payload(_window(with_segment=True))
    await proxy._subtitle_session._open_epoch()
    await proxy._broadcast_payload(payload, persist=True)
    await proxy._subtitle_session._close_epoch()
    await proxy._subtitle_session._open_epoch()
    await proxy._broadcast_payload(payload, persist=True)
    await proxy._subtitle_session._close_epoch()

    archives = sorted((tmp_path / "subtitles").glob("session-*.srt"))
    assert len(archives) == 2
    assert archives[0].name != archives[1].name
    assert "-2" in archives[1].name
    await proxy.stop()


async def test_epoch_close_broadcasts_reset_with_source_epoch(
    tmp_path: Path,
) -> None:
    proxy = _proxy(tmp_path)
    await proxy.start()
    received: list[str] = []

    async def sender(text: str) -> None:
        received.append(text)

    proxy.add_client(sender)
    await proxy._subtitle_session._open_epoch()
    await proxy._subtitle_session._close_epoch()
    await asyncio.sleep(0.05)

    assert received == [json.dumps({"type": "reset", "source_epoch": 1}, ensure_ascii=False)]
    await proxy.stop()


async def test_push_audio_requires_committed_owner(tmp_path: Path) -> None:
    transcriber = FakeTranscriber(source_epoch=1)
    proxy = SubtitleProxy(_settings(tmp_path), transcriber_factory=lambda _ctx: transcriber)
    await proxy.start()
    await proxy.push_audio(b"\x00\x00" * 1600)

    assert transcriber.sent_audio == []

    preparation = await proxy.prepare_capture("meeting-1", timeout_secs=1.0)
    proxy.commit_capture(preparation)
    await proxy.push_audio(b"\x00\x00" * 1600)
    await asyncio.sleep(0.1)
    assert transcriber.sent_audio == [b"\x00\x00" * 1600]
    await proxy.stop()


async def test_subtitle_reconnect_waits_while_ready_probe_denies(
    tmp_path: Path,
) -> None:
    ready = {"value": False}
    proxy = _flapping_proxy(tmp_path, backoff=(0.01, 0.02))

    async def probe() -> bool:
        return ready["value"]

    proxy._subtitle_session._readiness_probe = probe
    await proxy.start()
    preparation = await proxy.prepare_browser_capture(timeout_secs=1.0)
    proxy.commit_browser_capture(preparation)

    await asyncio.sleep(0.2)
    assert len(proxy._created) == 1

    ready["value"] = True
    await asyncio.sleep(0.15)
    assert len(proxy._created) >= 2
    await proxy.stop()


async def test_subtitle_reconnect_uses_backoff_without_stable_window(
    tmp_path: Path,
) -> None:
    created_at: list[float] = []
    loop = asyncio.get_running_loop()

    def factory(_ctx: ASRSessionContext) -> FakeTranscriber:
        created_at.append(loop.time())
        return ShortLivedTranscriber(source_epoch=1 + len(created_at))

    proxy = SubtitleProxy(
        _settings(tmp_path),
        transcriber_factory=factory,
        backoff_delays=(0.05, 0.2),
    )
    await proxy.start()
    preparation = await proxy.prepare_browser_capture(timeout_secs=1.0)
    proxy.commit_browser_capture(preparation)

    await asyncio.sleep(0.8)
    assert len(created_at) >= 3
    first_gap = created_at[1] - created_at[0]
    second_gap = created_at[2] - created_at[1]
    assert second_gap > first_gap * 1.5
    await proxy.stop()


async def test_stable_subtitle_connection_resets_backoff(tmp_path: Path) -> None:
    created: list[FakeTranscriber] = []

    def factory(_ctx: ASRSessionContext) -> FakeTranscriber:
        transcriber = FakeTranscriber(source_epoch=1 + len(created))
        created.append(transcriber)
        return transcriber

    proxy = SubtitleProxy(
        _settings(tmp_path),
        transcriber_factory=factory,
        backoff_delays=(0.05, 0.2),
        stable_reset_after_secs=0.1,
    )
    await proxy.start()
    preparation = await proxy.prepare_browser_capture(timeout_secs=1.0)
    proxy.commit_browser_capture(preparation)

    await asyncio.sleep(0.15)
    assert len(created) == 1

    first = created[0]
    first._end_stream = True
    await asyncio.sleep(0.3)
    assert len(created) == 2
    await proxy.stop()


async def test_capture_reconnect_waits_while_ready_probe_denies(
    tmp_path: Path,
) -> None:
    ready = {"value": False}
    created: list[FakeTranscriber] = []

    def factory(_ctx: ASRSessionContext) -> FakeTranscriber:
        transcriber = ShortLivedTranscriber(source_epoch=1 + len(created))
        created.append(transcriber)
        return transcriber

    async def probe() -> bool:
        return ready["value"]

    proxy = SubtitleProxy(
        _settings(tmp_path),
        transcriber_factory=factory,
        backoff_delays=(0.01, 0.02),
        readiness_probe=probe,
    )
    await proxy.start()
    preparation = await proxy.prepare_capture("meeting-1", timeout_secs=1.0)
    proxy.commit_capture(preparation)

    await asyncio.sleep(0.2)
    assert len(created) == 1

    ready["value"] = True
    await asyncio.sleep(0.15)
    assert len(created) >= 2
    await proxy.abort_capture()
    await proxy.stop()
