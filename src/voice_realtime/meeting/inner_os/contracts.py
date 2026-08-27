from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InnerOSEvidence(_Strict):
    segment_id: UUID
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker_key: str = Field(min_length=1)
    speaker_name: str = Field(min_length=1)
    text: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class InnerOSFact(_Strict):
    text: str = Field(min_length=1)
    evidence_segment_ids: tuple[UUID, ...] = Field(min_length=1)


class InnerOSJudgement(_Strict):
    text: str = Field(min_length=1)
    basis_segment_ids: tuple[UUID, ...]
    uncertainty: Literal["low", "medium", "high"]
    uncertainty_reason: str = Field(min_length=1)


class InnerOSDraft(_Strict):
    text: str = Field(min_length=1)


class InnerOSLimitation(_Strict):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class InnerOSAnswer(_Strict):
    intent: Literal["fact", "analysis", "draft", "mixed"]
    evidence: tuple[InnerOSEvidence, ...]
    facts: tuple[InnerOSFact, ...]
    judgements: tuple[InnerOSJudgement, ...]
    draft: InnerOSDraft | None
    limitations: tuple[InnerOSLimitation, ...]

    @model_validator(mode="after")
    def _validate_references(self) -> InnerOSAnswer:
        evidence_ids = {item.segment_id for item in self.evidence}
        references = {
            segment_id
            for fact in self.facts
            for segment_id in fact.evidence_segment_ids
        }
        references.update(
            segment_id for item in self.judgements for segment_id in item.basis_segment_ids
        )
        if not references <= evidence_ids:
            raise ValueError("answer references evidence outside its snapshot")
        return self
