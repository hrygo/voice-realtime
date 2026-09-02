"""AudioSourceRouter 两阶段生命周期与有界背压测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID

import pytest

from sona.audio.frame import AudioFrame, AudioSourceKind, AudioSourceRole
from sona.audio.profile import CaptureProfile
from sona.audio.router import (
    AudioSourceRouter,
    UnsupportedCaptureProfileError,
)
from sona.audio.source import AudioSourceHealth, AudioSourceState

VALID_DUAL = {
    "mode": "dual",
    "sources": [
        {"kind": "microphone", "role": "near_end"},
        {"kind": "physical_output", "role": "far_end"},
    ],
}


def make_frame(capture_id: UUID, *, sequence: int, sample: int) -> AudioFrame:
    return AudioFrame(
        capture_id=capture_id,
        source_id="mic-main",
        source_kind=AudioSourceKind.MICROPHONE,
        source_role=AudioSourceRole.NEAR_END,
        device_generation=0,
        sequence=sequence,
        host_time_ns=sequence + 1,
        pcm=sample.to_bytes(2, "little", signed=True) * 512,
    )


@dataclass
class FakeSource:
    kind: AudioSourceKind
    role: AudioSourceRole
    state: AudioSourceState = AudioSourceState.STOPPED
    prepare_error: BaseException | None = None
    commit_error: BaseException | None = None
    prepare_calls: int = 0
    commit_calls: int = 0
    abort_calls: int = 0
    stop_calls: int = 0
    queue: asyncio.Queue[AudioFrame] = field(default_factory=asyncio.Queue)

    async def prepare(self, capture_id: UUID) -> None:
        self.prepare_calls += 1
        if self.prepare_error is not None:
            raise self.prepare_error
        self.capture_id = capture_id
        self.state = AudioSourceState.READY

    async def commit(self) -> None:
        self.commit_calls += 1
        if self.commit_error is not None:
            raise self.commit_error
        self.state = AudioSourceState.ACTIVE

    async def abort(self) -> None:
        self.abort_calls += 1
        self.state = AudioSourceState.STOPPED

    async def stop(self) -> None:
        self.stop_calls += 1
        self.state = AudioSourceState.STOPPED

    async def frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            frame = await self.queue.get()
            try:
                yield frame
            finally:
                self.queue.task_done()

    def health(self) -> AudioSourceHealth:
        return AudioSourceHealth(
            state=self.state,
            queued_frames=self.queue.qsize(),
            dropped_frames=0,
            last_sequence=None,
            last_host_time_ns=None,
        )


async def test_router_prepare_commit_and_forward_single_source() -> None:
    source = FakeSource(AudioSourceKind.MICROPHONE, AudioSourceRole.NEAR_END)
    router = AudioSourceRouter([source], queue_size=2)
    capture_id = UUID(int=1)

    await router.prepare(CaptureProfile.microphone(), capture_id)
    assert router.health().state is AudioSourceState.READY
    await router.commit()
    await source.queue.put(make_frame(capture_id, sequence=0, sample=1))
    async with asyncio.timeout(1.0):
        frame = await anext(router.frames())

    assert frame.sequence == 0
    assert router.health().active_kind is AudioSourceKind.MICROPHONE
    assert source.prepare_calls == 1
    assert source.commit_calls == 1
    await router.stop()
    assert source.stop_calls == 1
    assert router.health().state is AudioSourceState.STOPPED


async def test_router_rejects_dual_before_preparing_sources() -> None:
    source = FakeSource(AudioSourceKind.MICROPHONE, AudioSourceRole.NEAR_END)
    router = AudioSourceRouter([source])

    with pytest.raises(UnsupportedCaptureProfileError, match="dual"):
        await router.prepare(CaptureProfile.model_validate(VALID_DUAL), UUID(int=1))

    assert source.prepare_calls == 0
    assert router.health().state is AudioSourceState.STOPPED


async def test_router_drops_oldest_without_growing_queue() -> None:
    source = FakeSource(AudioSourceKind.MICROPHONE, AudioSourceRole.NEAR_END)
    router = AudioSourceRouter([source], queue_size=1)
    capture_id = UUID(int=1)
    await router.prepare(CaptureProfile.microphone(), capture_id)
    await router.commit()

    await source.queue.put(make_frame(capture_id, sequence=1, sample=1))
    await source.queue.put(make_frame(capture_id, sequence=2, sample=2))
    await asyncio.sleep(0)
    assert router.health().dropped_frames == 1

    async with asyncio.timeout(1.0):
        frame = await anext(router.frames())
    assert frame.sequence == 2
    assert router.health().queued_frames == 0
    await router.stop()


async def test_router_ignores_frame_from_stale_capture() -> None:
    source = FakeSource(AudioSourceKind.MICROPHONE, AudioSourceRole.NEAR_END)
    router = AudioSourceRouter([source])
    capture_id = UUID(int=1)
    await router.prepare(CaptureProfile.microphone(), capture_id)
    await router.commit()

    await source.queue.put(make_frame(UUID(int=2), sequence=0, sample=1))
    await source.queue.put(make_frame(capture_id, sequence=1, sample=2))
    async with asyncio.timeout(1.0):
        frame = await anext(router.frames())

    assert frame.capture_id == capture_id
    assert frame.sequence == 1
    await router.stop()


async def test_router_prepare_failure_aborts_source_and_resets() -> None:
    source = FakeSource(
        AudioSourceKind.MICROPHONE,
        AudioSourceRole.NEAR_END,
        prepare_error=OSError("permission denied"),
    )
    router = AudioSourceRouter([source])

    with pytest.raises(OSError, match="permission denied"):
        await router.prepare(CaptureProfile.microphone(), UUID(int=1))

    assert source.abort_calls == 1
    assert router.health().state is AudioSourceState.STOPPED
    assert router.health().active_kind is None


async def test_router_commit_failure_aborts_source_and_resets() -> None:
    source = FakeSource(
        AudioSourceKind.MICROPHONE,
        AudioSourceRole.NEAR_END,
        commit_error=OSError("device gone"),
    )
    router = AudioSourceRouter([source])
    await router.prepare(CaptureProfile.microphone(), UUID(int=1))

    with pytest.raises(OSError, match="device gone"):
        await router.commit()

    assert source.abort_calls == 1
    assert router.health().state is AudioSourceState.STOPPED


async def test_router_stop_and_abort_are_idempotent() -> None:
    source = FakeSource(AudioSourceKind.MICROPHONE, AudioSourceRole.NEAR_END)
    router = AudioSourceRouter([source])
    await router.prepare(CaptureProfile.microphone(), UUID(int=1))

    await router.abort()
    await router.abort()
    await router.stop()

    assert source.abort_calls == 1
    assert source.stop_calls == 0


def test_router_rejects_duplicate_source_kind() -> None:
    first = FakeSource(AudioSourceKind.MICROPHONE, AudioSourceRole.NEAR_END)
    second = FakeSource(AudioSourceKind.MICROPHONE, AudioSourceRole.NEAR_END)

    with pytest.raises(ValueError, match="duplicate"):
        AudioSourceRouter([first, second])
