from uuid import UUID

import pytest

from voice_realtime.meeting.inner_os.context import build_context_snapshot
from voice_realtime.meeting.models import NormalizedSegment, TranscriptDocument

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
