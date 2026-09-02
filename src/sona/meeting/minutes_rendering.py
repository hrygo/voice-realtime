"""Canonical rendering of validated meeting minutes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID


def render_minutes_markdown(result: Any) -> str:
    """Render one stable Markdown representation for every persistence path."""
    title = str(_attr(result, "title", "") or "").strip()
    if title:
        if title.startswith("#"):
            header = title
        elif not title.startswith("会议纪要"):
            header = f"# 会议纪要：{title}"
        else:
            header = f"# {title}"
    else:
        header = "# 会议纪要"
    lines = [header, "", "## 概要", "", str(_attr(result, "overview", "")).strip(), ""]
    topics = list(_attr(result, "topics", ()) or ())
    if topics:
        lines.extend(["## 议题", ""])
        for topic in topics:
            lines.extend(
                [
                    f"### {str(_attr(topic, 'title', '')).strip()}",
                    "",
                    str(_attr(topic, "summary", "")).strip()
                    + _evidence_suffix(_attr(topic, "evidence_segment_ids", ())),
                    "",
                ]
            )
    sections: tuple[tuple[str, str, str], ...] = (
        ("决策", "decisions", "content"),
        ("行动项", "action_items", "task"),
        ("风险", "risks", "content"),
        ("待确认问题", "open_questions", "content"),
        ("重点", "highlights", "content"),
    )
    for heading, field, content_field in sections:
        values = list(_attr(result, field, ()) or ())
        if not values:
            continue
        lines.extend([f"## {heading}", ""])
        for item in values:
            text = str(_attr(item, content_field, "")).strip()
            if field == "action_items":
                metadata: list[str] = []
                owner = _attr(item, "owner")
                due_date = _attr(item, "due_date")
                if owner:
                    metadata.append(f"负责人：{owner}")
                if due_date:
                    metadata.append(f"截止：{due_date}")
                if metadata:
                    text += "（" + "；".join(metadata) + "）"
            text += _evidence_suffix(_attr(item, "evidence_segment_ids", ()))
            lines.extend([f"- {text}", ""])
    return "\n".join(lines).rstrip() + "\n"


def _attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _evidence_suffix(ids: Iterable[UUID | str]) -> str:
    values = [str(item) for item in ids]
    return f" 证据：{', '.join(f'[{item}]' for item in values)}" if values else ""


__all__ = ["render_minutes_markdown"]
