"""AliMeeting long TextGrid 的 frame 级解析与单说话人候选生成。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal

_SAMPLE_RATE = 16_000
_FRAMES_PER_MILLISECOND = _SAMPLE_RATE // 1000
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_ITEM = re.compile(r"(?m)^\s*item \[\d+\]:\s*$")
_INTERVAL = re.compile(r"(?m)^\s*intervals \[\d+\]:\s*$")
_AISHELL4_CONTROL_MARKERS = (
    "<sil>",
    "<%>",
    "<->",
    "<$>",
    "<#>",
    "<_>",
    "<space>",
)
BoundaryPolicy = Literal["exact-frame", "nearest-ms"]


@dataclass(frozen=True, slots=True)
class TextGridInterval:
    speaker: str
    start_frame: int
    end_frame: int
    reference: str

    @property
    def is_empty(self) -> bool:
        return not self.reference.strip()

    def candidate_id(self, *, session: str, content_group: str) -> str:
        payload = (
            f"{session}\0{content_group}\0{self.speaker}\0"
            f"{self.start_frame}\0{self.end_frame}"
        )
        return f"ali-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


@dataclass(frozen=True, slots=True)
class SpeakerOverlap:
    speaker_a: str
    speaker_b: str
    start_frame: int
    end_frame: int


@dataclass(frozen=True, slots=True)
class ParsedTextGrid:
    session: str
    content_group: str
    intervals: tuple[TextGridInterval, ...]
    overlaps: tuple[SpeakerOverlap, ...]
    collapsed_interval_count: int = 0


@dataclass(frozen=True, slots=True)
class SpeakerTurnCandidate:
    candidate_id: str
    session: str
    content_group: str
    speaker: str
    start_frame: int
    end_frame: int
    duration_ms: int
    reference: str


@dataclass(frozen=True, slots=True)
class NonSpeechCandidate:
    candidate_id: str
    session: str
    content_group: str
    start_frame: int
    end_frame: int
    duration_ms: int


def normalize_aishell4_reference(reference: str) -> str:
    """移除 AISHELL-4 标注控制符，保留真实词汇与语言标点。"""
    normalized = reference
    for marker in _AISHELL4_CONTROL_MARKERS:
        normalized = normalized.replace(marker, "")
    return normalized.replace("&", "").replace("`", "").strip()


def _is_lexical_reference(reference: str) -> bool:
    return bool(normalize_aishell4_reference(reference))


def _non_speech_candidate_id(
    *, session: str, content_group: str, start_frame: int, end_frame: int
) -> str:
    payload = f"{session}\0{content_group}\0{start_frame}\0{end_frame}"
    return f"neg-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def _require_identity(value: str) -> str:
    normalized = value.strip()
    if not _IDENTITY.fullmatch(normalized):
        raise ValueError("TextGrid identity must be an opaque safe identifier")
    return normalized


def _seconds_to_frame(raw: str, *, boundary_policy: BoundaryPolicy) -> int:
    try:
        seconds = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ValueError("TextGrid timestamp is invalid") from exc
    if boundary_policy == "nearest-ms":
        milliseconds = (seconds * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return int(milliseconds) * _FRAMES_PER_MILLISECOND
    frames = seconds * _SAMPLE_RATE
    integral = frames.to_integral_value()
    if frames != integral:
        raise ValueError("TextGrid timestamp must align to a 16 kHz sample frame")
    return int(integral)


def _required_value(block: str, name: str) -> str:
    match = re.search(rf'(?m)^\s*{re.escape(name)}\s*=\s*"?(.*?)"?\s*$', block)
    if match is None:
        raise ValueError(f"TextGrid field is missing: {name}")
    return match.group(1).strip()


def _parse_interval(
    block: str, *, speaker: str, boundary_policy: BoundaryPolicy
) -> TextGridInterval | None:
    start = _seconds_to_frame(
        _required_value(block, "xmin"), boundary_policy=boundary_policy
    )
    end = _seconds_to_frame(
        _required_value(block, "xmax"), boundary_policy=boundary_policy
    )
    if end == start and boundary_policy == "nearest-ms":
        return None
    if end <= start:
        raise ValueError("TextGrid interval end must be greater than start")
    raw_text = _required_value(block, "text")
    reference = raw_text.replace(r'\"', '"').replace('""', '"')
    return TextGridInterval(
        speaker=speaker,
        start_frame=start,
        end_frame=end,
        reference=reference,
    )


def _find_overlaps(
    intervals: tuple[TextGridInterval, ...],
) -> tuple[SpeakerOverlap, ...]:
    overlaps: list[SpeakerOverlap] = []
    for index, left in enumerate(intervals):
        if not _is_lexical_reference(left.reference):
            continue
        for right in intervals[index + 1 :]:
            if left.speaker == right.speaker or not _is_lexical_reference(
                right.reference
            ):
                continue
            start = max(left.start_frame, right.start_frame)
            end = min(left.end_frame, right.end_frame)
            if start >= end:
                continue
            speaker_a, speaker_b = sorted((left.speaker, right.speaker))
            overlaps.append(
                SpeakerOverlap(
                    speaker_a=speaker_a,
                    speaker_b=speaker_b,
                    start_frame=start,
                    end_frame=end,
                )
            )
    return tuple(
        sorted(
            set(overlaps),
            key=lambda item: (
                item.start_frame,
                item.end_frame,
                item.speaker_a,
                item.speaker_b,
            ),
        )
    )


def parse_long_textgrid(
    text: str,
    *,
    session: str,
    content_group: str,
    boundary_policy: BoundaryPolicy = "exact-frame",
) -> ParsedTextGrid:
    """解析 long TextGrid；时间统一成 16 kHz 半开 frame 区间。"""
    if 'Object class = "TextGrid"' not in text:
        raise ValueError("unsupported TextGrid object class")
    safe_session = _require_identity(session)
    safe_content_group = _require_identity(content_group)
    item_matches = tuple(_ITEM.finditer(text))
    if not item_matches:
        raise ValueError("TextGrid contains no interval tiers")
    intervals: list[TextGridInterval] = []
    collapsed_interval_count = 0
    for item_index, item_match in enumerate(item_matches):
        item_end = (
            item_matches[item_index + 1].start()
            if item_index + 1 < len(item_matches)
            else len(text)
        )
        item = text[item_match.end() : item_end]
        if _required_value(item, "class") != "IntervalTier":
            raise ValueError("TextGrid tier must be IntervalTier")
        speaker = _require_identity(_required_value(item, "name"))
        interval_matches = tuple(_INTERVAL.finditer(item))
        if not interval_matches:
            raise ValueError("TextGrid tier contains no intervals")
        for interval_index, interval_match in enumerate(interval_matches):
            interval_end = (
                interval_matches[interval_index + 1].start()
                if interval_index + 1 < len(interval_matches)
                else len(item)
            )
            interval = _parse_interval(
                item[interval_match.end() : interval_end],
                speaker=speaker,
                boundary_policy=boundary_policy,
            )
            if interval is None:
                collapsed_interval_count += 1
            else:
                intervals.append(interval)
    ordered = tuple(
        sorted(
            intervals,
            key=lambda item: (
                item.start_frame,
                item.end_frame,
                item.speaker,
                item.reference,
            ),
        )
    )
    return ParsedTextGrid(
        session=safe_session,
        content_group=safe_content_group,
        intervals=ordered,
        overlaps=_find_overlaps(ordered),
        collapsed_interval_count=collapsed_interval_count,
    )


def generate_speaker_turn_candidates(
    parsed: ParsedTextGrid,
) -> tuple[SpeakerTurnCandidate, ...]:
    """仅生成完整、非空、无跨说话人 overlap、整数毫秒的 turn。"""
    candidates: list[SpeakerTurnCandidate] = []
    for interval in parsed.intervals:
        if not _is_lexical_reference(interval.reference):
            continue
        if (interval.end_frame - interval.start_frame) % _FRAMES_PER_MILLISECOND:
            continue
        if any(
            max(interval.start_frame, overlap.start_frame)
            < min(interval.end_frame, overlap.end_frame)
            for overlap in parsed.overlaps
        ):
            continue
        candidates.append(
            SpeakerTurnCandidate(
                candidate_id=interval.candidate_id(
                    session=parsed.session,
                    content_group=parsed.content_group,
                ),
                session=parsed.session,
                content_group=parsed.content_group,
                speaker=interval.speaker,
                start_frame=interval.start_frame,
                end_frame=interval.end_frame,
                duration_ms=(
                    interval.end_frame - interval.start_frame
                )
                // _FRAMES_PER_MILLISECOND,
                reference=interval.reference,
            )
        )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.start_frame,
                item.end_frame,
                item.speaker,
                item.candidate_id,
            ),
        )
    )


def generate_non_speech_candidates(
    parsed: ParsedTextGrid,
    *,
    min_duration_ms: int = 1_000,
    max_duration_ms: int = 20_000,
) -> tuple[NonSpeechCandidate, ...]:
    """生成所有说话人均无词汇内容的真实背景区间。"""
    if min_duration_ms <= 0 or max_duration_ms < min_duration_ms:
        raise ValueError("non-speech duration bounds are invalid")
    if not parsed.intervals:
        return ()

    timeline_start = min(interval.start_frame for interval in parsed.intervals)
    timeline_end = max(interval.end_frame for interval in parsed.intervals)
    lexical = sorted(
        (
            (interval.start_frame, interval.end_frame)
            for interval in parsed.intervals
            if _is_lexical_reference(interval.reference)
        ),
        key=lambda bounds: (bounds[0], bounds[1]),
    )
    merged: list[tuple[int, int]] = []
    for start_frame, end_frame in lexical:
        if not merged or start_frame > merged[-1][1]:
            merged.append((start_frame, end_frame))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end_frame))

    gaps: list[tuple[int, int]] = []
    cursor = timeline_start
    for start_frame, end_frame in merged:
        if cursor < start_frame:
            gaps.append((cursor, start_frame))
        cursor = max(cursor, end_frame)
    if cursor < timeline_end:
        gaps.append((cursor, timeline_end))

    minimum_frames = min_duration_ms * _FRAMES_PER_MILLISECOND
    maximum_frames = max_duration_ms * _FRAMES_PER_MILLISECOND
    candidates: list[NonSpeechCandidate] = []
    for gap_start, gap_end in gaps:
        if (gap_end - gap_start) % _FRAMES_PER_MILLISECOND:
            continue
        chunk_start = gap_start
        while gap_end - chunk_start >= minimum_frames:
            remaining_frames = gap_end - chunk_start
            chunk_frames = min(maximum_frames, remaining_frames)
            remainder_frames = remaining_frames - chunk_frames
            if 0 < remainder_frames < minimum_frames:
                adjusted_frames = remaining_frames - minimum_frames
                if adjusted_frames >= minimum_frames:
                    chunk_frames = adjusted_frames
            chunk_end = chunk_start + chunk_frames
            duration_ms = (chunk_end - chunk_start) // _FRAMES_PER_MILLISECOND
            candidates.append(
                NonSpeechCandidate(
                    candidate_id=_non_speech_candidate_id(
                        session=parsed.session,
                        content_group=parsed.content_group,
                        start_frame=chunk_start,
                        end_frame=chunk_end,
                    ),
                    session=parsed.session,
                    content_group=parsed.content_group,
                    start_frame=chunk_start,
                    end_frame=chunk_end,
                    duration_ms=duration_ms,
                )
            )
            chunk_start = chunk_end
    return tuple(candidates)
