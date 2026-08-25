"""权威运行状态广播器的 latest-only 行为测试。"""

from __future__ import annotations

import asyncio

import pytest

from voice_realtime.meeting.models import PCMOwner, RuntimeMode
from voice_realtime.ui.protocol import DuplexMode, RuntimeStateSnapshot
from voice_realtime.ui.runtime_events import RuntimeStateBroadcaster


def snapshot(*, revision: int, mode: RuntimeMode) -> RuntimeStateSnapshot:
    owner = PCMOwner.NONE if mode is RuntimeMode.IDLE else PCMOwner(mode.value)
    return RuntimeStateSnapshot(
        pipeline="running",
        subtitle="connected",
        mic_muted=False,
        persona=None,
        voice="default",
        duplex_mode=DuplexMode.SPEAKER_FOCUS,
        session_started_at="2026-08-25T00:00:00+00:00",
        mode=mode,
        pcm_owner=owner,
        runtime_revision=revision,
    )


async def test_slow_client_only_keeps_latest_revision() -> None:
    current = snapshot(revision=1, mode=RuntimeMode.ASSISTANT)
    broadcaster = RuntimeStateBroadcaster(lambda: current)
    client = broadcaster.add_client()

    assert (await client.receive()).runtime_revision == 1

    broadcaster.publish(snapshot(revision=2, mode=RuntimeMode.IDLE))
    broadcaster.publish(snapshot(revision=3, mode=RuntimeMode.SUBTITLES))

    latest = await client.receive()
    assert latest.runtime_revision == 3
    assert latest.mode is RuntimeMode.SUBTITLES


def test_add_client_captures_current_state_without_await() -> None:
    broadcaster = RuntimeStateBroadcaster(
        lambda: snapshot(revision=7, mode=RuntimeMode.MEETING)
    )

    client = broadcaster.add_client()

    initial = client.latest_nowait()
    assert initial.runtime_revision == 7
    assert initial.mode is RuntimeMode.MEETING


async def test_remove_client_clears_pending_state_and_is_idempotent() -> None:
    broadcaster = RuntimeStateBroadcaster(
        lambda: snapshot(revision=1, mode=RuntimeMode.ASSISTANT)
    )
    client = broadcaster.add_client()

    broadcaster.remove_client(client)
    broadcaster.remove_client(client)
    broadcaster.publish(snapshot(revision=2, mode=RuntimeMode.SUBTITLES))

    with pytest.raises(RuntimeError, match="closed"):
        client.latest_nowait()
    with pytest.raises(RuntimeError, match="closed"):
        await client.receive()


async def test_remove_client_wakes_receiver_blocked_on_empty_queue() -> None:
    broadcaster = RuntimeStateBroadcaster(
        lambda: snapshot(revision=1, mode=RuntimeMode.ASSISTANT)
    )
    client = broadcaster.add_client()
    assert client.latest_nowait().runtime_revision == 1

    waiter = asyncio.create_task(client.receive())
    await asyncio.sleep(0)
    assert waiter.done() is False

    broadcaster.remove_client(client)

    with pytest.raises(RuntimeError, match="closed"):
        await asyncio.wait_for(waiter, timeout=0.1)


def test_clients_keep_independent_latest_snapshots() -> None:
    broadcaster = RuntimeStateBroadcaster(
        lambda: snapshot(revision=1, mode=RuntimeMode.ASSISTANT)
    )
    fast_client = broadcaster.add_client()
    slow_client = broadcaster.add_client()

    assert fast_client.latest_nowait().runtime_revision == 1

    broadcaster.publish(snapshot(revision=2, mode=RuntimeMode.SUBTITLES))
    assert fast_client.latest_nowait().runtime_revision == 2

    broadcaster.publish(snapshot(revision=3, mode=RuntimeMode.MEETING))

    assert fast_client.latest_nowait().runtime_revision == 3
    assert slow_client.latest_nowait().runtime_revision == 3

    broadcaster.remove_client(fast_client)
    broadcaster.publish(snapshot(revision=4, mode=RuntimeMode.IDLE))

    with pytest.raises(RuntimeError, match="closed"):
        fast_client.latest_nowait()
    assert slow_client.latest_nowait().runtime_revision == 4
