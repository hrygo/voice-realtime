from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from voice_realtime.meeting.models import NormalizedSegment, TranscriptDocument


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    alias: str
    segment_id: UUID
    start_ms: int
    end_ms: int
    speaker_key: str
    speaker_name: str
    text: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class InnerOSContextSnapshot:
    meeting_id: UUID
    transcript_revision: int
    content_revision: int
    captured_at: datetime
    evidence: tuple[EvidenceSnapshot, ...]
    total_segment_count: int
    included_segment_count: int
    cropped: bool
    selection_strategy: str


def build_context_snapshot(
    document: TranscriptDocument,
    *,
    question: str,
    max_chars: int = 48_000,
    focus_segment_ids: tuple[UUID, ...] = (),
) -> InnerOSContextSnapshot:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    segments = tuple(
        sorted(document.segments, key=lambda item: (item.start_ms, item.end_ms, item.order))
    )
    known = {segment.id for segment in segments}
    speaker_names = {
        speaker.speaker_key: speaker.display_name for speaker in document.speakers
    }
    if any(segment_id not in known for segment_id in focus_segment_ids):
        raise ValueError("focus segment does not belong to current confirmed meeting")
    question_terms = {term for term in question.strip().lower().split() if term}
    ranked = sorted(
        segments,
        key=lambda item: (
            item.id not in focus_segment_ids,
            -sum(term in item.text.lower() for term in question_terms),
            item.start_ms,
        ),
    )
    selected: list[NormalizedSegment] = []
    used = 0
    for segment in ranked:
        cost = len(segment.text)
        if selected and used + cost > max_chars:
            continue
        selected.append(segment)
        used += cost
    selected.sort(key=lambda item: (item.start_ms, item.end_ms, item.order))
    evidence = tuple(
        EvidenceSnapshot(
            alias=f"S{index:04d}",
            segment_id=segment.id,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            speaker_key=segment.speaker_key,
            speaker_name=speaker_names.get(segment.speaker_key, segment.speaker_key),
            text=segment.text,
            content_hash=hashlib.sha256(segment.text.encode()).hexdigest(),
        )
        for index, segment in enumerate(selected, 1)
    )
    return InnerOSContextSnapshot(
        meeting_id=document.meeting_id,
        transcript_revision=document.transcript_revision,
        content_revision=document.content_revision,
        captured_at=datetime.now(UTC),
        evidence=evidence,
        total_segment_count=len(segments),
        included_segment_count=len(evidence),
        cropped=len(evidence) < len(segments),
        selection_strategy="question_relevance_then_timeline",
    )
