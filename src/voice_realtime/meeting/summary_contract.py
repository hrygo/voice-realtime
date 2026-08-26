"""LM Studio 会议纪要的受限模型侧契约。

模型只处理紧凑的 ``S0001`` 证据引用；应用在边界处把引用解析回真实
segment UUID。这样既减少输出 token，也避免模型复制长 UUID 时退化。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from voice_realtime.meeting.models import MinutesResult


class _ModelContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelTopic(_ModelContract):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1_000)
    evidence_segment_ids: tuple[str, ...] = Field(default=(), max_length=6)


class ModelDecision(_ModelContract):
    content: str = Field(min_length=1, max_length=1_000)
    evidence_segment_ids: tuple[str, ...] = Field(default=(), max_length=6)


class ModelActionItem(_ModelContract):
    task: str = Field(min_length=1, max_length=1_000)
    owner: str | None = Field(default=None, max_length=200)
    due_date: str | None = Field(default=None, max_length=64)
    evidence_segment_ids: tuple[str, ...] = Field(default=(), max_length=6)


class ModelEvidenceItem(_ModelContract):
    content: str = Field(min_length=1, max_length=1_000)
    evidence_segment_ids: tuple[str, ...] = Field(default=(), max_length=6)


class ModelMinutesResult(_ModelContract):
    title: str | None = Field(default=None, max_length=64)
    overview: str = Field(min_length=1, max_length=3_000)
    topics: tuple[ModelTopic, ...] = Field(default=(), max_length=12)
    decisions: tuple[ModelDecision, ...] = Field(default=(), max_length=12)
    action_items: tuple[ModelActionItem, ...] = Field(default=(), max_length=12)
    risks: tuple[ModelEvidenceItem, ...] = Field(default=(), max_length=8)
    open_questions: tuple[ModelEvidenceItem, ...] = Field(default=(), max_length=8)
    highlights: tuple[ModelEvidenceItem, ...] = Field(default=(), max_length=12)


def resolve_minutes_result(
    result: ModelMinutesResult,
    references: Mapping[str, UUID],
) -> MinutesResult:
    """把模型侧短引用或兼容 UUID 引用转换为最终领域模型。"""

    known_ids = frozenset(references.values())

    def resolve(raw: str) -> UUID:
        value = str(raw).strip()
        if value.startswith("SEG:"):
            value = value.removeprefix("SEG:").strip()
        direct = references.get(value)
        if direct is not None:
            return direct
        try:
            candidate = UUID(value)
        except ValueError as exc:
            raise ValueError(f"未知证据引用: {value[:64]}") from exc
        if candidate not in known_ids:
            raise ValueError(f"未知证据引用: {value[:64]}")
        return candidate

    payload = result.model_dump(mode="json")
    for field in (
        "topics",
        "decisions",
        "action_items",
        "risks",
        "open_questions",
        "highlights",
    ):
        for item in payload[field]:
            item["evidence_segment_ids"] = [
                str(resolve(value)) for value in item.get("evidence_segment_ids", ())
            ]
    return MinutesResult.model_validate(payload)


def model_schema() -> dict[str, Any]:
    return ModelMinutesResult.model_json_schema()


__all__ = ["ModelMinutesResult", "model_schema", "resolve_minutes_result"]
