"""会议长文本分块切分 (Map-Reduce) 与结果归并算法。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

from sona.meeting.models import MinutesResult
from sona.meeting.summary.errors import SummaryValidationError
from sona.meeting.summary.evidence_anchor import _attr


def _copy_document(document: Any, segments: Sequence[Any]) -> Any:
    if hasattr(document, "model_copy"):
        return document.model_copy(update={"segments": tuple(segments)})
    if isinstance(document, Mapping):
        copied = dict(document)
        copied["segments"] = tuple(segments)
        return SimpleNamespace(**copied)
    values = dict(vars(document)) if hasattr(document, "__dict__") else {}
    values["segments"] = tuple(segments)
    return SimpleNamespace(**values)


def _dedupe_items(values: Sequence[Any], identity_fields: tuple[str, ...]) -> tuple[Any, ...]:
    seen: set[tuple[str, tuple[str, ...]]] = set()
    result: list[Any] = []
    for value in values:
        normalized = " ".join(
            str(_attr(value, field, "")).strip().lower() for field in identity_fields
        )
        evidence = tuple(
            sorted(str(item) for item in _attr(value, "evidence_segment_ids", ()) or ())
        )
        identity = (normalized, evidence)
        if identity not in seen:
            seen.add(identity)
            result.append(value)
    return tuple(result)


def _merge_results(results: Sequence[MinutesResult]) -> MinutesResult:
    if not results:
        raise SummaryValidationError("map 阶段没有有效纪要结果")
    first = results[0]
    title = next(
        (
            str(item.title).strip()
            for item in reversed(results)
            if str(_attr(item, "title", "") or "").strip()
        ),
        None,
    )
    values: dict[str, Any] = {
        "title": title,
        "overview": "\n".join(
            str(item.overview).strip() for item in results if str(item.overview).strip()
        ),
        "topics": _dedupe_items(
            [item for result in results for item in result.topics], ("title", "summary")
        ),
        "decisions": _dedupe_items(
            [item for result in results for item in result.decisions], ("content",)
        ),
        "action_items": _dedupe_items(
            [item for result in results for item in result.action_items],
            ("task", "owner", "due_date"),
        ),
        "risks": _dedupe_items(
            [item for result in results for item in result.risks], ("content",)
        ),
        "open_questions": _dedupe_items(
            [item for result in results for item in result.open_questions], ("content",)
        ),
        "highlights": _dedupe_items(
            [item for result in results for item in result.highlights], ("content",)
        ),
    }
    # Keep a valid non-empty overview even if a permissive fake returns blank text.
    if not values["overview"]:
        values["overview"] = str(first.overview)
    return MinutesResult.model_validate(values)


def split_document(document: Any, settings: Any) -> tuple[Any, ...]:
    """按时长与字符上限将会议转录切分为重叠窗口块。"""

    segments = tuple(_attr(document, "segments", ()) or ())
    max_chars = int(
        getattr(settings, "summary_max_input_chars", 0)
        or getattr(settings, "summary_context_chars", 0)
        or 20_000
    )
    max_duration_ms = int(
        getattr(settings, "summary_chunk_max_duration_ms", 0) or 1_200_000
    )
    if len(segments) <= 1:
        return (document,)
    chunks: list[Any] = []
    current: list[Any] = []
    current_len = 0
    overlap = int(getattr(settings, "summary_chunk_overlap_segments", 1) or 1)
    for segment in segments:
        line_len = len(str(_attr(segment, "text", ""))) + 100
        chunk_start_ms = int(_attr(current[0], "start_ms", 0)) if current else 0
        segment_end_ms = int(_attr(segment, "end_ms", chunk_start_ms))
        exceeds_duration = bool(
            current and segment_end_ms - chunk_start_ms > max_duration_ms
        )
        if current and (current_len + line_len > max_chars or exceeds_duration):
            chunks.append(_copy_document(document, current))
            current = current[-overlap:] if overlap else []
            current_len = sum(len(str(_attr(item, "text", ""))) + 100 for item in current)
        current.append(segment)
        current_len += line_len
    if current:
        chunks.append(_copy_document(document, current))
    return tuple(chunks)
