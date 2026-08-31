"""AudioSource 生命周期与麦克风适配器测试。"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from voice_realtime.audio.hub import AudioHub
from voice_realtime.audio.source import (
    AudioSource,
    AudioSourceState,
    MicrophoneSource,
)


async def test_microphone_source_prepare_commit_and_frame() -> None:
    hub = AudioHub()
    source = MicrophoneSource(hub, source_id="mic-main", queue_size=2)

    await source.prepare(UUID(int=1))
    assert source.state is AudioSourceState.READY
    assert isinstance(source, AudioSource)

    await source.commit()
    hub._loop = asyncio.get_running_loop()
    hub._running = True
    hub._start_sink_workers()
    hub._on_chunk_received(b"\x01\x00" * 512)
    async with asyncio.timeout(1.0):
        frame = await anext(source.frames())

    assert frame.capture_id == UUID(int=1)
    assert frame.source_id == "mic-main"
    assert frame.sequence == 0
    assert frame.host_time_ns > 0
    assert frame.pcm == b"\x01\x00" * 512

    await source.stop()
    await hub.stop()
    assert source.state is AudioSourceState.STOPPED


async def test_microphone_source_ignores_frames_before_commit() -> None:
    source = MicrophoneSource(AudioHub(), source_id="mic-main")
    await source.prepare(UUID(int=1))

    await source._receive_pcm(b"\x01\x00" * 512)

    assert source.health().queued_frames == 0
    assert source.health().last_sequence is None
    await source.abort()


async def test_microphone_source_abort_removes_sink_and_is_idempotent() -> None:
    hub = AudioHub()
    source = MicrophoneSource(hub, source_id="mic-main")
    await source.prepare(UUID(int=1))

    await source.abort()
    await source.abort()

    assert source.state is AudioSourceState.STOPPED
    assert not hub._sinks


async def test_microphone_source_drops_oldest_when_full() -> None:
    source = MicrophoneSource(AudioHub(), source_id="mic-main", queue_size=1)
    await source.prepare(UUID(int=1))
    await source.commit()

    await source._receive_pcm(b"\x01\x00" * 512)
    await source._receive_pcm(b"\x02\x00" * 512)
    async with asyncio.timeout(1.0):
        frame = await anext(source.frames())

    health = source.health()
    assert frame.pcm == b"\x02\x00" * 512
    assert frame.sequence == 1
    assert health.dropped_frames == 1
    assert health.last_sequence == 1
    assert health.last_host_time_ns is not None
    assert not hasattr(health, "queue")
    assert not hasattr(health, "pcm")
    await source.stop()


async def test_microphone_source_rejects_invalid_transitions() -> None:
    source = MicrophoneSource(AudioHub(), source_id="mic-main")
    with pytest.raises(RuntimeError, match="ready"):
        await source.commit()

    await source.prepare(UUID(int=1))
    with pytest.raises(RuntimeError, match="stopped"):
        await source.prepare(UUID(int=2))

    await source.abort()


def test_microphone_source_validates_identity_and_queue_size() -> None:
    with pytest.raises(ValueError, match="source_id"):
        MicrophoneSource(AudioHub(), source_id="  ")
    with pytest.raises(ValueError, match="queue_size"):
        MicrophoneSource(AudioHub(), source_id="mic-main", queue_size=0)
