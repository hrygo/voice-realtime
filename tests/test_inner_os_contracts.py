from uuid import UUID

import pytest
from pydantic import ValidationError

from voice_realtime.meeting.inner_os.contracts import InnerOSAnswer


def test_answer_requires_evidence_for_each_fact_and_basis_for_judgement() -> None:
    answer = InnerOSAnswer.model_validate({
        "intent": "mixed",
        "evidence": [{
            "segment_id": "11111111-1111-4111-8111-111111111112",
            "start_ms": 0, "end_ms": 1000, "speaker_key": "s1", "speaker_name": "甲",
            "text": "事实", "content_hash": "sha256:" + "a" * 64,
        }],
        "facts": [{
            "text": "事实",
            "evidence_segment_ids": ["11111111-1111-4111-8111-111111111112"],
        }],
        "judgements": [{
            "text": "判断",
            "basis_segment_ids": ["11111111-1111-4111-8111-111111111112"],
            "uncertainty": "medium",
            "uncertainty_reason": "证据有限",
        }],
        "draft": None,
        "limitations": [],
    })
    assert answer.facts[0].evidence_segment_ids == (
        UUID("11111111-1111-4111-8111-111111111112"),
    )


def test_answer_rejects_unknown_fields_and_confidence_percentages() -> None:
    with pytest.raises(ValidationError):
        InnerOSAnswer.model_validate({"intent": "fact", "confidence": 95})
