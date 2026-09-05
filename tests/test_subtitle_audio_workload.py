from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sona.audio.frame import AudioFrame, AudioSourceKind, AudioSourceRole
from sona.audio.output_source import AudioCaptureError
from sona.audio.selection import SubtitleCaptureSelection
from sona.audio.source import AudioSource
from sona.meeting.models import PCMOwner, RuntimeMode
from sona.meeting.runtime_mode import ModeConflictError, RuntimeModeCoordinator
from sona.subtitles.audio_workload import SubtitleAudioWorkload

OUTPUT = SubtitleCaptureSelection(source="physical_output", device_ref="vrdev1_" + "A" * 43)


class Sink:
    browser_capture_active = False

    def __init__(self):
        self.prepared = None
        self.received = []
        self.delivered = asyncio.Event()

    async def prepare_browser_capture(self, *, timeout_secs):
        self.prepared = object()
        return self.prepared

    def commit_browser_capture(self, preparation):
        assert preparation is self.prepared
        self.prepared = None
        self.browser_capture_active = True

    async def abort_browser_capture(self, preparation):
        assert preparation is self.prepared
        self.prepared = None

    async def deactivate_browser_capture(self):
        self.browser_capture_active = False

    async def push_audio(self, data):
        assert self.browser_capture_active
        self.received.append(data)
        self.delivered.set()


def setup_workload(*, timeout=1):
    sink = Sink()
    source = Mock(spec=AudioSource)
    queue = asyncio.Queue()

    async def frames():
        while True:
            item = await queue.get()
            if isinstance(item, Exception):
                raise item
            yield item

    source.frames = frames
    factory = Mock(return_value=source)
    owner = [PCMOwner.NONE]
    changed = asyncio.Event()
    workload = SubtitleAudioWorkload(
        sink, factory, can_forward=lambda: owner[0] is PCMOwner.SUBTITLES,
        on_frame=Mock(), on_state=changed.set, frame_timeout=timeout,
    )
    interaction = Mock(active=False, start=AsyncMock(), stop=AsyncMock())
    coordinator = RuntimeModeCoordinator(
        interaction, workload, initial_mode=RuntimeMode.IDLE,
        on_owner_changed=lambda value: owner.__setitem__(0, value),
    )
    return workload, sink, source, queue, factory, coordinator, changed


def frame(capture_id):
    return AudioFrame(
        capture_id=capture_id, source_id="test-output",
        source_kind=AudioSourceKind.PHYSICAL_OUTPUT, source_role=AudioSourceRole.FAR_END,
        device_generation=0, sequence=0, host_time_ns=100,
        pcm=bytes(1024),
    )


async def test_output_forwards_only_after_commit_and_stop_releases_source():
    workload, sink, source, queue, _, coordinator, _ = setup_workload()
    await coordinator.start_subtitles(OUTPUT)
    capture_id = source.prepare.await_args.args[0]
    source.commit.assert_awaited_once()
    await queue.put(frame(uuid4()))
    await queue.put(frame(capture_id))
    await asyncio.wait_for(sink.delivered.wait(), 1)
    assert sink.received == [bytes(1024)]
    assert coordinator.pcm_owner is PCMOwner.SUBTITLES
    await coordinator.stop_active_mode()
    source.stop.assert_awaited_once()
    assert not workload.browser_capture_active
    assert coordinator.mode is RuntimeMode.IDLE


async def test_prepare_is_silent_and_abort_releases_both_leases():
    workload, sink, source, _, _, _, _ = setup_workload()
    preparation = await workload.prepare_browser_capture(timeout_secs=1, capture=OUTPUT)
    source.commit.assert_not_awaited()
    assert sink.received == []
    await workload.abort_browser_capture(preparation)
    source.abort.assert_awaited_once()
    assert sink.prepared is None


async def test_microphone_never_launches_helper():
    _, _, _, _, factory, coordinator, _ = setup_workload()
    await coordinator.start_subtitles()
    factory.assert_not_called()
    await coordinator.stop()


@pytest.mark.parametrize("stage", ["prepare", "commit"])
async def test_output_failure_aborts_and_preserves_idle(stage):
    workload, sink, source, _, _, coordinator, _ = setup_workload()
    getattr(source, stage).side_effect = AudioCaptureError(
        "permission_denied", "系统音频权限未授予", retryable=False
    )
    with pytest.raises(AudioCaptureError):
        await coordinator.start_subtitles(OUTPUT)
    source.abort.assert_awaited_once()
    assert coordinator.mode is RuntimeMode.IDLE
    assert not workload.browser_capture_active
    assert sink.prepared is None


async def test_cancel_pending_helper_commit_cleans_both_leases():
    workload, sink, source, _, _, coordinator, _ = setup_workload()
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    source.commit.side_effect = blocked
    task = asyncio.create_task(coordinator.start_subtitles(OUTPUT))
    await entered.wait()
    await coordinator.stop()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sink.prepared is None
    assert not workload.browser_capture_active
    source.abort.assert_awaited_once()


@pytest.mark.parametrize("silent", [False, True])
async def test_source_disconnect_or_missing_callbacks_is_visible_and_stops_capture(silent):
    workload, sink, source, queue, _, coordinator, changed = setup_workload(timeout=0.02)
    await coordinator.start_subtitles(OUTPUT)
    if not silent:
        await queue.put(AudioCaptureError("helper_disconnected", "连接已断开", retryable=True))
    await asyncio.wait_for(changed.wait(), 1)
    assert workload.error
    assert workload.uses_output
    assert not sink.browser_capture_active
    source.stop.assert_awaited_once()
    await coordinator.stop()


async def test_switching_input_while_active_requires_explicit_stop():
    _, _, source, _, _, coordinator, _ = setup_workload()
    await coordinator.start_subtitles(OUTPUT)
    with pytest.raises(ModeConflictError):
        await coordinator.start_subtitles(SubtitleCaptureSelection())
    assert coordinator.mode is RuntimeMode.SUBTITLES
    source.stop.assert_not_awaited()
    await coordinator.stop()


async def test_stop_joins_inflight_failure_cleanup_without_double_release():
    _, sink, source, queue, _, coordinator, _ = setup_workload()
    entered, released = asyncio.Event(), asyncio.Event()

    async def release_source():
        entered.set()
        await released.wait()

    source.stop.side_effect = release_source
    await coordinator.start_subtitles(OUTPUT)
    await queue.put(AudioCaptureError("helper_disconnected", "连接已断开", retryable=True))
    await entered.wait()
    stopping = asyncio.create_task(coordinator.stop_active_mode())
    await asyncio.sleep(0)
    assert not stopping.done()
    released.set()
    await asyncio.wait_for(stopping, 1)
    source.stop.assert_awaited_once()
    assert not sink.browser_capture_active
    assert coordinator.mode is RuntimeMode.IDLE


async def test_missing_stop_ack_does_not_prevent_explicit_retry():
    workload, _, source, _, _, coordinator, _ = setup_workload()
    source.stop.side_effect = AudioCaptureError("helper_disconnected", "连接已断开", retryable=True)
    await coordinator.start_subtitles(OUTPUT)
    await coordinator.stop_active_mode()
    await coordinator.start_subtitles(OUTPUT)
    assert workload.browser_capture_active
    assert workload.error is None
    await coordinator.stop()


@pytest.mark.parametrize("capture", [
    {"source": "physical_output"}, {"source": "dual"},
    {"source": "physical_output", "device_ref": "raw-device-uid"},
    {"source": "microphone", "device_ref": OUTPUT.device_ref},
])
def test_selection_rejects_ambiguous_or_private_identifiers(capture):
    with pytest.raises(ValidationError):
        SubtitleCaptureSelection.model_validate(capture)
