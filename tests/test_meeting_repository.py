"""会议 PostgreSQL repository 的临时 schema 集成测试。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from psycopg import AsyncConnection

from voice_realtime.config import MeetingSettings
from voice_realtime.meeting.migrations import run_migrations
from voice_realtime.meeting.models import (
    MeetingStatus,
    MinutesResult,
    NormalizedSegment,
    TranscriptWindow,
)
from voice_realtime.meeting.repository import (
    InvalidCursorError,
    MeetingConflictError,
    MeetingNotFoundError,
    PostgresMeetingRepository,
)


def _test_database_url() -> str:
    value = os.environ.get("VR_TEST_DATABASE_URL")
    if not value:
        pytest.skip("VR_TEST_DATABASE_URL 未设置；跳过真实 PostgreSQL 集成测试")
    return value


@pytest_asyncio.fixture
async def repository(tmp_path: Path) -> AsyncIterator[PostgresMeetingRepository]:
    database_url = _test_database_url()
    schema = f"vr_test_{uuid4().hex[:16]}"
    async with await AsyncConnection.connect(database_url, autocommit=True) as admin:
        await admin.execute(f'CREATE SCHEMA "{schema}"')

    settings = MeetingSettings(
        database_url=database_url,
        schema=schema,
        recovery_dir=tmp_path / "recovery",
    )
    await run_migrations(database_url, schema=schema)
    repo = PostgresMeetingRepository(settings)
    await repo.open()
    try:
        yield repo
    finally:
        await repo.close()
        async with await AsyncConnection.connect(database_url, autocommit=True) as admin:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _window(epoch: int, text: str, start_ms: int, end_ms: int) -> TranscriptWindow:
    return TranscriptWindow(
        source_epoch=epoch,
        segments=(
            NormalizedSegment(
                id=uuid4(),
                order=0,
                source_epoch=epoch,
                speaker_key=f"e{epoch}:s1",
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_reconcile_replaces_only_overlapping_window(
    repository: PostgresMeetingRepository,
) -> None:
    meeting = await repository.create_meeting("周会", language="Chinese", audio_source="microphone")
    await repository.reconcile_window(meeting.id, _window(0, "第一段", 0, 1000))
    await repository.reconcile_window(meeting.id, _window(1, "修订段", 900, 2000))

    document = await repository.get_transcript(meeting.id)

    assert [segment.text for segment in document.segments] == ["修订段"]
    assert document.transcript_revision == 2


@pytest.mark.asyncio
async def test_duplicate_window_is_idempotent(repository: PostgresMeetingRepository) -> None:
    meeting = await repository.create_meeting(
        "幂等性", language="Chinese", audio_source="microphone"
    )
    window = _window(0, "同一快照", 0, 1000)

    first = await repository.reconcile_window(meeting.id, window)
    second = await repository.reconcile_window(meeting.id, window)

    assert first.transcript_revision == second.transcript_revision == 1
    assert len((await repository.get_transcript(meeting.id)).segments) == 1


@pytest.mark.asyncio
async def test_speaker_rename_marks_minutes_source_stale(
    repository: PostgresMeetingRepository,
) -> None:
    meeting = await repository.create_meeting(
        "说话人", language="Chinese", audio_source="microphone"
    )
    await repository.reconcile_window(meeting.id, _window(1, "内容", 0, 1000))
    await repository.finalize_transcript(meeting.id)
    minutes = await repository.create_minutes(meeting.id, idempotency_key="same")

    changed = await repository.rename_speaker(meeting.id, "e1:s1", "张三")

    assert changed.content_revision > minutes.source_content_revision
    document = await repository.get_transcript(meeting.id)
    assert document.speakers[0].display_name == "张三"


@pytest.mark.asyncio
async def test_finalize_transcript_prevents_new_window(
    repository: PostgresMeetingRepository,
) -> None:
    meeting = await repository.create_meeting(
        "封存", language="Chinese", audio_source="microphone"
    )
    await repository.reconcile_window(meeting.id, _window(0, "最终段", 0, 1000))

    finalized = await repository.finalize_transcript(meeting.id)

    assert finalized.status.value == "completed"
    with pytest.raises(MeetingConflictError):
        await repository.reconcile_window(meeting.id, _window(0, "迟到", 1000, 2000))


@pytest.mark.asyncio
async def test_minutes_claim_and_complete_are_transactional(
    repository: PostgresMeetingRepository,
) -> None:
    meeting = await repository.create_meeting(
        "纪要", language="Chinese", audio_source="microphone"
    )
    await repository.reconcile_window(meeting.id, _window(0, "内容", 0, 1000))
    await repository.finalize_transcript(meeting.id)
    created = await repository.create_minutes(meeting.id, idempotency_key="same")
    same = await repository.create_minutes(meeting.id, idempotency_key="same")
    assert same.id == created.id

    job = await repository.claim_minutes()
    assert job is not None
    assert job.minutes.id == created.id
    assert job.minutes.status.value == "generating"
    assert job.minutes.attempts == 1

    result = MinutesResult(overview="已记录内容")
    completed = await repository.complete_minutes(created.id, result)
    assert completed.status.value == "completed"
    assert completed.content_json is not None
    assert completed.content_json.overview == "已记录内容"
    assert await repository.claim_minutes() is None
    latest = await repository.get_latest_minutes(meeting.id)
    assert latest is not None
    assert latest.content_json is not None
    assert latest.content_json.overview == "已记录内容"


@pytest.mark.asyncio
async def test_list_update_title_and_cursor_pagination(
    repository: PostgresMeetingRepository,
) -> None:
    first = await repository.create_meeting(
        "第一次", language="Chinese", audio_source="microphone"
    )
    second = await repository.create_meeting(
        "第二次", language="Chinese", audio_source="microphone"
    )

    page = await repository.list_meetings(cursor=None, limit=1)
    assert [item.id for item in page.items] == [second.id]
    assert page.next_cursor is not None
    next_page = await repository.list_meetings(cursor=page.next_cursor, limit=1)
    assert [item.id for item in next_page.items] == [first.id]

    renamed = await repository.update_title(first.id, " 新标题 ")
    assert renamed.title == "新标题"
    assert (await repository.get_meeting(first.id)).title == "新标题"  # type: ignore[union-attr]
    with pytest.raises(InvalidCursorError):
        await repository.list_meetings(cursor="not-a-cursor", limit=20)
    with pytest.raises(ValueError):
        await repository.list_meetings(cursor=None, limit=0)


@pytest.mark.asyncio
async def test_speakers_and_specific_minutes_version(
    repository: PostgresMeetingRepository,
) -> None:
    meeting = await repository.create_meeting(
        "详情", language="Chinese", audio_source="microphone"
    )
    await repository.reconcile_window(meeting.id, _window(2, "内容", 0, 1000))
    speakers = await repository.get_speakers(meeting.id)
    assert speakers[0].speaker_key == "e2:s1"
    assert speakers[0].display_name == "说话人 s1"
    unchanged = await repository.rename_speaker(meeting.id, "e2:s1", "说话人 s1")
    assert unchanged.content_revision == 1

    await repository.finalize_transcript(meeting.id)
    minutes = await repository.create_minutes(meeting.id, idempotency_key=None)
    selected = await repository.get_minutes(meeting.id, minutes.version)
    assert selected is not None and selected.id == minutes.id
    assert await repository.get_minutes(meeting.id, minutes.version + 1) is None
    with pytest.raises(ValueError):
        await repository.get_minutes(meeting.id, 0)


@pytest.mark.asyncio
async def test_minutes_failure_requeue_and_delete(
    repository: PostgresMeetingRepository,
) -> None:
    meeting = await repository.create_meeting(
        "失败纪要", language="Chinese", audio_source="microphone"
    )
    await repository.finalize_transcript(meeting.id)
    minutes = await repository.create_minutes(meeting.id, idempotency_key="retry")
    assert await repository.claim_minutes() is not None
    assert await repository.requeue_generating() == 1
    assert await repository.claim_minutes() is not None
    await repository.fail_minutes(
        minutes.id,
        code="model_error",
        message="模型不可用",
        raw_output="invalid",
    )
    failed = await repository.get_minutes(meeting.id, minutes.version)
    assert failed is not None
    assert failed.status.value == "failed"
    assert failed.error_code == "model_error"
    with pytest.raises(MeetingConflictError):
        await repository.complete_minutes(minutes.id, MinutesResult(overview="late"))

    await repository.delete_meeting(meeting.id)
    assert await repository.get_meeting(meeting.id) is None
    with pytest.raises(MeetingNotFoundError):
        await repository.delete_meeting(meeting.id)


@pytest.mark.asyncio
async def test_recover_stale_and_interrupted_finalization(
    repository: PostgresMeetingRepository,
) -> None:
    recording = await repository.create_meeting(
        "录制中", language="Chinese", audio_source="microphone"
    )
    finalizing = await repository.create_meeting(
        "封存中", language="Chinese", audio_source="microphone"
    )
    await repository.set_status(finalizing.id, MeetingStatus.FINALIZING)

    assert await repository.recover_stale() == 2
    recovered = await repository.get_meeting(recording.id)
    assert recovered is not None
    assert recovered.status is MeetingStatus.INTERRUPTED
    assert recovered.interruption_reason == "application_restart"

    timed_out = await repository.create_meeting(
        "超时", language="Chinese", audio_source="microphone"
    )
    await repository.set_status(timed_out.id, MeetingStatus.FINALIZING)
    interrupted = await repository.finalize_transcript(
        timed_out.id,
        final_status=MeetingStatus.INTERRUPTED,
        reason="finalization_timeout",
    )
    assert interrupted.status is MeetingStatus.INTERRUPTED
    assert interrupted.interruption_reason == "finalization_timeout"
