from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class HumanRating(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str
    evidence_valid: bool
    evidence_covered: bool
    safe_insufficiency: bool
    draft_usable: bool
    usefulness: int = Field(ge=1, le=5)
    unsupported_claim: bool


class EvaluationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_validity: float = Field(ge=0, le=1)
    evidence_coverage: float = Field(ge=0, le=1)
    safe_insufficiency: float = Field(ge=0, le=1)
    draft_usable: float = Field(ge=0, le=1)
    effective_answer: float = Field(ge=0, le=1)
    average_usefulness: float = Field(ge=1, le=5)


def summarize_ratings(
    ratings: list[HumanRating], *, insufficient_count: int, draft_count: int
) -> EvaluationSummary:
    if not ratings or insufficient_count <= 0 or draft_count <= 0:
        raise ValueError("ratings and denominators must be positive")
    completed = len(ratings)
    answerable = completed - insufficient_count
    effective = sum(r.usefulness >= 4 and not r.unsupported_claim for r in ratings)
    return EvaluationSummary(
        evidence_validity=sum(r.evidence_valid for r in ratings) / len(ratings),
        evidence_coverage=sum(r.evidence_covered for r in ratings) / max(answerable, 1),
        safe_insufficiency=(
            min(sum(r.safe_insufficiency for r in ratings), insufficient_count)
            / insufficient_count
        ),
        draft_usable=sum(r.draft_usable for r in ratings) / draft_count,
        effective_answer=effective / completed,
        average_usefulness=sum(r.usefulness for r in ratings) / completed,
    )
