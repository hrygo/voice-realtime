from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path("contracts/meeting-assistant/v1")
EVENT_SCHEMA_FILES = {
    "meeting_snapshot": "event-meeting-snapshot.schema.json",
    "meeting_state_changed": "event-meeting-state-changed.schema.json",
    "transcript_partial": "event-transcript-partial.schema.json",
    "transcript_reconciled": "event-transcript-reconciled.schema.json",
    "speaker_updated": "event-speaker-updated.schema.json",
    "meeting_title_updated": "event-meeting-title-updated.schema.json",
    "minutes_state_changed": "event-minutes-state-changed.schema.json",
    "health_changed": "event-health-changed.schema.json",
    "transcription_gap": "event-transcription-gap.schema.json",
    "resync_required": "event-resync-required.schema.json",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _event_fixtures() -> list[Path]:
    return sorted((ROOT / "fixtures").glob("*.json"))


def test_contract_metadata_and_canonical_surface_exist() -> None:
    assert (ROOT / "README.md").is_file()
    assert (ROOT.parent / "CHANGELOG.md").is_file()

    openapi = _load_json(ROOT / "openapi.json")
    assert openapi["paths"]["/api/v1/runtime"]["get"]

    asyncapi = (ROOT / "asyncapi.yaml").read_text(encoding="utf-8")
    assert "address: /ws/v1/control" in asyncapi
    assert "address: /ws/v1/meetings" in asyncapi
    assert "start_subtitles" in asyncapi


def test_every_fixture_has_a_strict_event_schema() -> None:
    envelope = _load_json(ROOT / "schemas/event-envelope.schema.json")
    envelope_validator = Draft202012Validator(envelope, format_checker=FormatChecker())

    fixtures = _event_fixtures()
    assert fixtures
    assert {json.loads(path.read_text(encoding="utf-8"))["type"] for path in fixtures} == set(
        EVENT_SCHEMA_FILES
    )

    for fixture_path in fixtures:
        value = _load_json(fixture_path)
        envelope_validator.validate(value)

        event_type = value["type"]
        schema_path = ROOT / "schemas" / EVENT_SCHEMA_FILES[event_type]
        assert schema_path.is_file(), f"missing schema for {event_type}"
        validator = Draft202012Validator(_load_json(schema_path), format_checker=FormatChecker())
        validator.validate(value)


def test_event_schema_rejects_payload_from_another_event() -> None:
    value = _load_json(ROOT / "fixtures/transcript-partial.json")
    schema = _load_json(ROOT / "schemas/event-transcript-reconciled.schema.json")

    with pytest.raises(ValidationError):
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
