"""会议转录证据锚定、UUID 校验与文本格式化。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sona.meeting.models import MinutesResult
from sona.meeting.summary.errors import InvalidEvidenceError


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _iso_utc(value: Any) -> str:
    if isinstance(value, datetime):
        rendered = value.isoformat()
        return rendered.replace("+00:00", "Z")
    return str(value or "")


def _speaker_name(speakers: Any, key: str) -> str:
    if isinstance(speakers, Mapping):
        speaker = speakers.get(key)
    else:
        speaker = next(
            (item for item in speakers or () if _attr(item, "speaker_key") == key),
            None,
        )
    return str(_attr(speaker, "display_name") or _attr(speaker, "default_label") or key)


def _segment_id(segment: Any) -> UUID:
    raw = _attr(segment, "id")
    if isinstance(raw, UUID):
        return raw
    return UUID(str(raw))


def _format_timestamp(ms: int) -> str:
    seconds, millis = divmod(max(0, int(ms)), 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def format_transcript(document: Any, speakers: Any = ()) -> str:
    """将封存转录格式化为带 UUID 和时间证据的可信资料块。"""

    lines: list[str] = []
    for segment in _attr(document, "segments", ()) or ():
        segment_id = _segment_id(segment)
        start_ms = int(_attr(segment, "start_ms", 0))
        end_ms = int(_attr(segment, "end_ms", start_ms))
        speaker_key = str(_attr(segment, "speaker_key", "unknown"))
        text = str(_attr(segment, "text", "")).replace("\x00", " ").strip()
        if not text:
            continue
        name = _speaker_name(speakers, speaker_key)
        lines.append(
            f"[SEG:{segment_id}][{_format_timestamp(start_ms)}–{_format_timestamp(end_ms)}]"
            f"[{name}] {text}"
        )
    return "\n".join(lines)


def _segment_references(document: Any) -> dict[str, UUID]:
    references: dict[str, UUID] = {}
    for segment in _attr(document, "segments", ()) or ():
        text = str(_attr(segment, "text", "")).replace("\x00", " ").strip()
        if not text:
            continue
        reference = f"S{len(references) + 1:04d}"
        references[reference] = _segment_id(segment)
    return references


def _format_model_transcript(
    document: Any,
    speakers: Any = (),
) -> tuple[str, dict[str, UUID]]:
    references: dict[str, UUID] = {}
    lines: list[str] = []
    for segment in _attr(document, "segments", ()) or ():
        start_ms = int(_attr(segment, "start_ms", 0))
        end_ms = int(_attr(segment, "end_ms", start_ms))
        speaker_key = str(_attr(segment, "speaker_key", "unknown"))
        text = str(_attr(segment, "text", "")).replace("\x00", " ").strip()
        if not text:
            continue
        reference = f"S{len(references) + 1:04d}"
        references[reference] = _segment_id(segment)
        name = _speaker_name(speakers, speaker_key)
        lines.append(
            f"[{reference}][{_format_timestamp(start_ms)}–{_format_timestamp(end_ms)}]"
            f"[{name}] {text}"
        )
    return "\n".join(lines), references


def _evidence_ids(value: Any) -> list[UUID]:
    try:
        return [item if isinstance(item, UUID) else UUID(str(item)) for item in value or ()]
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidEvidenceError("evidence_segment_ids 必须是 UUID 数组") from exc


def validate_evidence(result: MinutesResult, document: Any) -> MinutesResult:
    """确保所有纪要证据均能回指当前封存转录的 UUID。"""

    known = {_segment_id(segment) for segment in (_attr(document, "segments", ()) or ())}
    if not known and any(
        _evidence_ids(_attr(item, "evidence_segment_ids", ()))
        for field in (
            "topics",
            "decisions",
            "action_items",
            "risks",
            "open_questions",
            "highlights",
        )
        for item in (_attr(result, field, ()) or ())
    ):
        raise InvalidEvidenceError("纪要引用了不存在的转录证据")

    for field in (
        "topics",
        "decisions",
        "action_items",
        "risks",
        "open_questions",
        "highlights",
    ):
        for item in _attr(result, field, ()) or ():
            ids = _evidence_ids(_attr(item, "evidence_segment_ids", ()))
            if not ids:
                raise InvalidEvidenceError(f"纪要条目缺少转录证据: {field}")
            missing = [str(item_id) for item_id in ids if item_id not in known]
            if missing:
                raise InvalidEvidenceError(f"纪要引用了不存在的转录证据: {missing[0]}")
    return result
