"""LM Studio 会议纪要的分阶段模型侧契约。

模型只处理紧凑的 ``S0001`` 证据引用；map 中间结果允许达到领域模型容量，
最终 reduce/repair 结果使用更紧凑的集合上限。应用在边界处把引用解析回真实
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
    title: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=250)
    evidence_segment_ids: tuple[str, ...] = Field(default=(), max_length=4)


class ModelDecision(_ModelContract):
    content: str = Field(min_length=1, max_length=250)
    evidence_segment_ids: tuple[str, ...] = Field(default=(), max_length=4)


class ModelActionItem(_ModelContract):
    task: str = Field(min_length=1, max_length=250)
    owner: str | None = Field(default=None, max_length=80)
    due_date: str | None = Field(default=None, max_length=64)
    evidence_segment_ids: tuple[str, ...] = Field(default=(), max_length=4)


class ModelEvidenceItem(_ModelContract):
    content: str = Field(min_length=1, max_length=200)
    evidence_segment_ids: tuple[str, ...] = Field(default=(), max_length=4)


class ModelMinutesResult(_ModelContract):
    """最终 reduce/repair 使用的紧凑模型侧纪要契约。"""

    title: str | None = Field(default=None, max_length=64)
    overview: str = Field(min_length=1, max_length=600)
    topics: tuple[ModelTopic, ...] = Field(default=(), max_length=8)
    decisions: tuple[ModelDecision, ...] = Field(default=(), max_length=8)
    action_items: tuple[ModelActionItem, ...] = Field(default=(), max_length=8)
    risks: tuple[ModelEvidenceItem, ...] = Field(default=(), max_length=4)
    open_questions: tuple[ModelEvidenceItem, ...] = Field(default=(), max_length=4)
    highlights: tuple[ModelEvidenceItem, ...] = Field(default=(), max_length=6)


class ModelMapMinutesResult(ModelMinutesResult):
    """分块 map 的中间契约，允许完整领域模型容量以内的结果。"""

    topics: tuple[ModelTopic, ...] = Field(default=(), max_length=12)
    decisions: tuple[ModelDecision, ...] = Field(default=(), max_length=12)
    action_items: tuple[ModelActionItem, ...] = Field(default=(), max_length=12)
    risks: tuple[ModelEvidenceItem, ...] = Field(default=(), max_length=8)
    open_questions: tuple[ModelEvidenceItem, ...] = Field(default=(), max_length=8)
    highlights: tuple[ModelEvidenceItem, ...] = Field(default=(), max_length=12)


def resolve_minutes_result(
    result: ModelMinutesResult | ModelMapMinutesResult,
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


def model_schema(*, for_map: bool = False) -> dict[str, Any]:
    contract = ModelMapMinutesResult if for_map else ModelMinutesResult
    return contract.model_json_schema()


__all__ = [
    "ModelMapMinutesResult",
    "ModelMinutesResult",
    "model_schema",
    "resolve_minutes_result",
]
