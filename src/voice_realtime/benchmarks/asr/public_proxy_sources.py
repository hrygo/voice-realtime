"""AliMeeting long TextGrid 的 frame 级解析与单说话人候选生成。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_SAMPLE_RATE = 16_000
_FRAMES_PER_MILLISECOND = _SAMPLE_RATE // 1000
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_ITEM = re.compile(r"(?m)^\s*item \[\d+\]:\s*$")
_INTERVAL = re.compile(r"(?m)^\s*intervals \[\d+\]:\s*$")


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


def _require_identity(value: str) -> str:
    normalized = value.strip()
    if not _IDENTITY.fullmatch(normalized):
        raise ValueError("TextGrid identity must be an opaque safe identifier")
    return normalized


def _seconds_to_frame(raw: str) -> int:
    try:
        frames = Decimal(raw.strip()) * _SAMPLE_RATE
    except InvalidOperation as exc:
        raise ValueError("TextGrid timestamp is invalid") from exc
    integral = frames.to_integral_value()
    if frames != integral:
        raise ValueError("TextGrid timestamp must align to a 16 kHz sample frame")
    return int(integral)


def _required_value(block: str, name: str) -> str:
    match = re.search(rf'(?m)^\s*{re.escape(name)}\s*=\s*"?(.*?)"?\s*$', block)
    if match is None:
        raise ValueError(f"TextGrid field is missing: {name}")
    return match.group(1).strip()


def _parse_interval(block: str, *, speaker: str) -> TextGridInterval:
    start = _seconds_to_frame(_required_value(block, "xmin"))
    end = _seconds_to_frame(_required_value(block, "xmax"))
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
        for right in intervals[index + 1 :]:
            if left.speaker == right.speaker:
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
            intervals.append(
                _parse_interval(
                    item[interval_match.end() : interval_end],
                    speaker=speaker,
                )
            )
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
    )


def generate_speaker_turn_candidates(
    parsed: ParsedTextGrid,
) -> tuple[SpeakerTurnCandidate, ...]:
    """仅生成完整、非空、无跨说话人 overlap、整数毫秒的 turn。"""
    candidates: list[SpeakerTurnCandidate] = []
    for interval in parsed.intervals:
        if interval.is_empty:
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
