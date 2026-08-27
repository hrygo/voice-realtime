from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    speaker: str = Field(min_length=1)
    text: str = Field(min_length=1)


class MeetingFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meeting_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    meeting_type: str
    segments: list[Evidence] = Field(min_length=1)


class EvaluationQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str
    meeting_id: str
    intent: str
    question: str = Field(min_length=1)
    expected_evidence: tuple[str, ...] = ()
    expected_insufficient: bool = False


class EvaluationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meetings: tuple[MeetingFixture, ...]
    questions: tuple[EvaluationQuestion, ...]

    @property
    def meeting_ids(self) -> frozenset[str]:
        return frozenset(m.meeting_id for m in self.meetings)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_dataset(root: Path) -> EvaluationDataset:
    meeting_files = sorted(root.glob("*-review.json")) + sorted(
        root.glob("requirements-clarification.json")
    )
    meetings = tuple(MeetingFixture.model_validate(_read(path)) for path in meeting_files)
    questions = tuple(
        EvaluationQuestion.model_validate(item) for item in _read(root / "questions.json")
    )
    dataset = EvaluationDataset(meetings=meetings, questions=questions)
    if len(dataset.meetings) != 3 or len(dataset.questions) != 40:
        raise ValueError("Inner OS dataset must contain exactly three meetings and forty questions")
    if any(q.meeting_id not in dataset.meeting_ids for q in dataset.questions):
        raise ValueError("question references an unknown meeting")
    return dataset
