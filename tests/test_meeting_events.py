from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import BaseModel

from sona.meeting.events import DURABLE_EVENT_TYPES, MeetingEventBroadcaster, make_event


def test_meeting_title_updated_is_a_durable_event() -> None:
    assert "meeting_title_updated" in DURABLE_EVENT_TYPES


@pytest.mark.asyncio
async def test_slow_client_gets_resync_required_for_durable_event() -> None:
    broadcaster = MeetingEventBroadcaster()
    client = broadcaster.add_test_client(queue_size=1)
    await broadcaster.publish(make_event("transcript_reconciled", "m1", {"transcript_revision": 1}))
    await broadcaster.publish(make_event("transcript_reconciled", "m1", {"transcript_revision": 2}))

    event = await client.receive()
    assert event["type"] == "resync_required"
    assert event["payload"] == {
        "expected_revision": 2,
        "reason": "client_queue_overflow",
    }
    assert broadcaster.snapshot()["payload"]["transcript_revision"] == 2


@pytest.mark.asyncio
async def test_revision_gap_emits_resync_instead_of_broadcasting_new_revision() -> None:
    broadcaster = MeetingEventBroadcaster()
    client = broadcaster.add_test_client()
    await broadcaster.publish(
        make_event(
            "transcript_reconciled",
            "m1",
            {"transcript_revision": 1, "content_revision": 1},
        )
    )
    assert (await client.receive())["payload"]["transcript_revision"] == 1

    await broadcaster.publish(
        make_event(
            "transcript_reconciled",
            "m1",
            {"transcript_revision": 3, "content_revision": 3},
        )
    )

    event = await client.receive()
    assert event["type"] == "resync_required"
    assert event["meeting_id"] == "m1"
    assert event["payload"] == {"expected_revision": 2, "reason": "revision_gap"}
    assert broadcaster.snapshot()["payload"]["transcript_revision"] == 1


@pytest.mark.asyncio
async def test_observed_snapshot_reseeds_revision_cursor_after_gap() -> None:
    broadcaster = MeetingEventBroadcaster()
    client = broadcaster.add_test_client()
    await broadcaster.publish(
        make_event(
            "transcript_reconciled",
            "m1",
            {"transcript_revision": 1, "content_revision": 1},
        )
    )
    await client.receive()
    await broadcaster.publish(
        make_event(
            "transcript_reconciled",
            "m1",
            {"transcript_revision": 3, "content_revision": 3},
        )
    )
    await client.receive()

    await broadcaster.observe_snapshot(
        make_event(
            "meeting_snapshot",
            "m1",
            {
                "meeting": {"id": "m1"},
                "transcript_revision": 3,
                "content_revision": 3,
            },
        )
    )
    await broadcaster.publish(
        make_event(
            "transcript_reconciled",
            "m1",
            {"transcript_revision": 4, "content_revision": 4},
        )
    )

    event = await client.receive()
    assert event["type"] == "transcript_reconciled"
    assert event["payload"]["transcript_revision"] == 4


@pytest.mark.asyncio
async def test_partial_event_can_be_dropped_without_resync() -> None:
    broadcaster = MeetingEventBroadcaster()
    client = broadcaster.add_test_client(queue_size=1)
    await broadcaster.publish(make_event("transcript_partial", "m1", {"text": "a"}))
    await broadcaster.publish(make_event("transcript_partial", "m1", {"text": "b"}))

    event = await client.receive()
    assert event["type"] == "transcript_partial"
    assert event["payload"] == {"text": "a"}
    assert client.queue.empty()


def test_event_envelope_has_version_and_utc_timestamp() -> None:
    event = make_event("health_changed", "m1", {"storage": "ok"})
    assert event["contract_version"] == "1"
    assert event["meeting_id"] == "m1"
    assert event["occurred_at"].endswith("Z")


@pytest.mark.asyncio
async def test_publish_event_fans_out_json_values_and_tracks_latest_snapshot() -> None:
    broadcaster = MeetingEventBroadcaster(queue_size=2)
    first = broadcaster.add_client()
    second = broadcaster.add_client()
    meeting_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    occurred = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)

    await broadcaster.publish_event(
        "speaker_updated",
        meeting_id,
        {"speaker_id": meeting_id, "occurred_at": occurred, "labels": ("主持人",)},
    )
    first_event = await first.receive()
    second_event = await second.receive()

    assert first_event == second_event
    assert first_event["meeting_id"] == str(meeting_id)
    assert first_event["payload"] == {
        "speaker_id": str(meeting_id),
        "occurred_at": "2026-08-21T10:00:00Z",
        "labels": ["主持人"],
    }
    assert broadcaster.client_count == 2
    assert broadcaster.clients == (first, second) or broadcaster.clients == (second, first)
    assert broadcaster.snapshot() == first_event


@pytest.mark.asyncio
async def test_client_removal_closes_and_drains_pending_events() -> None:
    broadcaster = MeetingEventBroadcaster()
    client = broadcaster.add_test_client()
    await broadcaster.publish(make_event("health_changed", "m1", {"storage": "ok"}))

    broadcaster.remove_client(client)

    assert client.closed is True
    assert broadcaster.client_count == 0
    assert client.queue.empty()
    with pytest.raises(asyncio.CancelledError):
        await client.receive()


@pytest.mark.asyncio
async def test_snapshot_factories_support_models_and_async_values() -> None:
    class SnapshotModel(BaseModel):
        revision: int

    sync_broadcaster = MeetingEventBroadcaster(
        snapshot_factory=lambda: SnapshotModel(revision=3)
    )

    async def async_factory() -> dict[str, object]:
        return {"revision": 4, "meeting_id": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")}

    async_broadcaster = MeetingEventBroadcaster(snapshot_factory=async_factory)

    assert sync_broadcaster.snapshot() == {"revision": 3}
    assert await sync_broadcaster.snapshot_async() == {"revision": 3}
    assert async_broadcaster.snapshot() is None
    assert await async_broadcaster.snapshot_async() == {
        "revision": 4,
        "meeting_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    }


@pytest.mark.asyncio
async def test_snapshot_uses_latest_event_and_invalid_events_are_rejected() -> None:
    broadcaster = MeetingEventBroadcaster()
    assert broadcaster.snapshot() is None
    await broadcaster.publish(make_event("health_changed", "m1", {"storage": "ok"}))

    assert broadcaster.snapshot()["type"] == "health_changed"
    await broadcaster.publish(make_event("resync_required", "m1", {"reason": "manual"}))
    assert broadcaster.snapshot()["type"] == "health_changed"
    with pytest.raises(TypeError, match="must be an object"):
        await broadcaster.publish(["not", "an", "event"])


@pytest.mark.asyncio
async def test_closed_clients_are_pruned_on_next_publish() -> None:
    broadcaster = MeetingEventBroadcaster()
    client = broadcaster.add_test_client()
    client.close()

    await broadcaster.publish(make_event("health_changed", "m1", {"storage": "ok"}))

    assert broadcaster.client_count == 0


@pytest.mark.asyncio
async def test_resync_event_does_not_replace_latest_durable_snapshot() -> None:
    broadcaster = MeetingEventBroadcaster(queue_size=1)
    client = broadcaster.add_test_client()
    first = make_event("transcript_reconciled", "m1", {"transcript_revision": 1})
    second = make_event("transcript_reconciled", "m1", {"transcript_revision": 2})
    await broadcaster.publish(first)
    await broadcaster.publish(second)

    resync = await client.receive()

    assert resync["type"] == "resync_required"
    assert broadcaster.snapshot() == second


@pytest.mark.asyncio
async def test_durable_overflow_without_revision_still_requests_resync() -> None:
    broadcaster = MeetingEventBroadcaster(queue_size=1)
    client = broadcaster.add_test_client()
    await broadcaster.publish(make_event("health_changed", "m1", None))
    await broadcaster.publish(make_event("health_changed", "m1", "unknown"))

    resync = await client.receive()

    assert resync["type"] == "resync_required"
    assert resync["payload"]["expected_revision"] is None
