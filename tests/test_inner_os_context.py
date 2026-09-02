from uuid import UUID

import pytest

from sona.meeting.inner_os.context import build_context_snapshot
from sona.meeting.models import NormalizedSegment, TranscriptDocument

MEETING_ID = UUID("11111111-1111-4111-8111-111111111111")
SEGMENT_ID = UUID("11111111-1111-4111-8111-111111111112")


def test_snapshot_only_contains_current_confirmed_segments_and_stable_alias() -> None:
    document = TranscriptDocument(
        meeting_id=MEETING_ID,
        transcript_revision=3,
        content_revision=4,
        segments=(NormalizedSegment(
            id=SEGMENT_ID, order=0, source_epoch=1, speaker_key="s1",
            start_ms=0, end_ms=1000, text="确认的会议内容",
        ),),
    )
    snapshot = build_context_snapshot(document, question="会议内容", max_chars=1000)
    assert snapshot.meeting_id == MEETING_ID
    assert snapshot.evidence[0].alias == "S0001"
    assert snapshot.evidence[0].segment_id == SEGMENT_ID
    assert snapshot.cropped is False


def test_focus_from_another_meeting_is_rejected() -> None:
    document = TranscriptDocument(
        meeting_id=MEETING_ID, transcript_revision=1, content_revision=1, segments=()
    )
    with pytest.raises(ValueError, match="focus segment"):
        build_context_snapshot(document, question="x", focus_segment_ids=(UUID(int=2),))


def test_cjk_relevance_keeps_early_match_before_recent_window() -> None:
    relevant_id = UUID("11111111-1111-4111-8111-111111111113")
    middle_id = UUID("11111111-1111-4111-8111-111111111114")
    recent_id = UUID("11111111-1111-4111-8111-111111111115")
    document = TranscriptDocument(
        meeting_id=MEETING_ID,
        transcript_revision=3,
        content_revision=4,
        segments=(
            NormalizedSegment(
                id=relevant_id,
                order=0,
                source_epoch=1,
                speaker_key="s1",
                start_ms=0,
                end_ms=1_000,
                text="发布安排周五确认",
            ),
            NormalizedSegment(
                id=middle_id,
                order=1,
                source_epoch=1,
                speaker_key="s1",
                start_ms=2_000,
                end_ms=3_000,
                text="讨论办公用品采购",
            ),
            NormalizedSegment(
                id=recent_id,
                order=2,
                source_epoch=1,
                speaker_key="s1",
                start_ms=4_000,
                end_ms=5_000,
                text="最后确认负责人张三",
            ),
        ),
    )

    snapshot = build_context_snapshot(
        document,
        question="项目什么时候发布？",
        max_chars=20,
        recent_chars=10,
    )

    assert [item.segment_id for item in snapshot.evidence] == [relevant_id, recent_id]
    assert snapshot.cropped is True


def test_context_selection_never_exceeds_character_budget() -> None:
    document = TranscriptDocument(
        meeting_id=MEETING_ID,
        transcript_revision=1,
        content_revision=1,
        segments=(
            NormalizedSegment(
                id=SEGMENT_ID,
                order=0,
                source_epoch=1,
                speaker_key="s1",
                start_ms=0,
                end_ms=1_000,
                text="超出预算的长文本",
            ),
        ),
    )

    snapshot = build_context_snapshot(
        document, question="长文本", max_chars=2, recent_chars=1
    )

    assert snapshot.evidence == ()
