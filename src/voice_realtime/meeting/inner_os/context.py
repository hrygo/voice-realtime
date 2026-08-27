from __future__ import annotations

import hashlib
import re
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
    recent_chars: int = 16_000,
    focus_segment_ids: tuple[UUID, ...] = (),
) -> InnerOSContextSnapshot:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if recent_chars < 0:
        raise ValueError("recent_chars must not be negative")
    segments = tuple(
        sorted(document.segments, key=lambda item: (item.start_ms, item.end_ms, item.order))
    )
    known = {segment.id for segment in segments}
    speaker_names = {
        speaker.speaker_key: speaker.display_name for speaker in document.speakers
    }
    if any(segment_id not in known for segment_id in focus_segment_ids):
        raise ValueError("focus segment does not belong to current confirmed meeting")
    if sum(len(segment.text) for segment in segments) <= max_chars:
        selected = list(segments)
    else:
        recent_budget = min(recent_chars, max_chars)
        recent: list[NormalizedSegment] = []
        recent_used = 0
        for segment in reversed(segments):
            cost = len(segment.text)
            if cost <= recent_budget - recent_used:
                recent.append(segment)
                recent_used += cost
        recent.reverse()
        recent_ids = {segment.id for segment in recent}
        question_terms = _relevance_terms(question)
        ranked = sorted(
            (segment for segment in segments if segment.id not in recent_ids),
            key=lambda item: (
                item.id not in focus_segment_ids,
                -_relevance_score(item.text, question_terms),
                item.start_ms,
                item.order,
            ),
        )
        early: list[NormalizedSegment] = []
        remaining = max_chars - recent_used
        for segment in ranked:
            cost = len(segment.text)
            if cost <= remaining:
                early.append(segment)
                remaining -= cost
        early.sort(key=lambda item: (item.start_ms, item.end_ms, item.order))
        selected = early + recent
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
        selection_strategy="cjk_relevance_then_recent_window",
    )


def _relevance_terms(question: str) -> frozenset[str]:
    normalized = question.strip().lower()
    terms = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    for sequence in re.findall(r"[\u3400-\u9fff]+", normalized):
        if len(sequence) == 1:
            terms.add(sequence)
            continue
        terms.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return frozenset(terms)


def _relevance_score(text: str, terms: frozenset[str]) -> int:
    normalized = text.lower()
    return sum(1 for term in terms if term in normalized)
