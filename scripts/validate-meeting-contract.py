#!/usr/bin/env python3
"""Validate the versioned meeting REST/WS contract and all shared fixtures."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, SchemaError, ValidationError

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
REQUIRED_ASYNCAPI_REFERENCES = tuple(
    f"./schemas/{filename}" for filename in EVENT_SCHEMA_FILES.values()
)
INNER_OS_FIXTURE_FILES = {
    "inner-os-completed.json",
    "inner-os-insufficient.json",
    "inner-os-invalid-focus.json",
}


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON contract must be an object: {path}")
    return value


def validate_contract(repository_root: Path) -> tuple[int, int]:
    """Validate contract metadata, schemas and fixtures.

    Returns ``(fixture_count, schema_count)`` for stable CI output.
    """

    contract_root = repository_root / "contracts/meeting-assistant/v1"
    schema_root = contract_root / "schemas"
    fixture_root = contract_root / "fixtures"
    openapi = _load_json(contract_root / "openapi.json")
    required_paths = {
        "/api/v1/runtime",
        "/api/v1/meetings",
        "/api/v1/meetings/{meeting_id}/transcript",
        "/api/v1/meetings/{meeting_id}/inner-os/exchanges",
    }
    if not required_paths.issubset(openapi.get("paths", {})):
        raise ValueError("OpenAPI is missing a canonical meeting path")

    asyncapi = (contract_root / "asyncapi.yaml").read_text(encoding="utf-8")
    for marker in (
        "address: /ws/v1/control",
        "address: /ws/v1/meetings",
        "address: /ws/v1/meetings/{meeting_id}/inner-os",
        "start_subtitles",
    ):
        if marker not in asyncapi:
            raise ValueError(f"AsyncAPI is missing required marker: {marker}")
    for reference in REQUIRED_ASYNCAPI_REFERENCES:
        if reference not in asyncapi:
            raise ValueError(f"AsyncAPI is missing event schema reference: {reference}")

    format_checker = FormatChecker()
    envelope_path = schema_root / "event-envelope.schema.json"
    envelope = _load_json(envelope_path)
    Draft202012Validator.check_schema(envelope)
    envelope_validator = Draft202012Validator(envelope, format_checker=format_checker)

    inner_os_envelope_path = schema_root / "inner-os-event.schema.json"
    inner_os_envelope = _load_json(inner_os_envelope_path)
    Draft202012Validator.check_schema(inner_os_envelope)
    inner_os_envelope_validator = Draft202012Validator(
        inner_os_envelope, format_checker=format_checker
    )

    schemas = sorted(schema_root.glob("*.json"))
    for schema_path in schemas:
        Draft202012Validator.check_schema(_load_json(schema_path))

    fixtures = sorted(fixture_root.glob("*.json"))
    if not fixtures:
        raise ValueError("no meeting contract fixtures found")
    fixture_types: set[str] = set()
    for fixture_path in fixtures:
        if fixture_path.name in INNER_OS_FIXTURE_FILES:
            fixture = _load_json(fixture_path)
            inner_os_envelope_validator.validate(fixture)
            continue

        fixture = _load_json(fixture_path)
        event_type = fixture.get("type")
        if not isinstance(event_type, str) or event_type not in EVENT_SCHEMA_FILES:
            raise ValueError(f"unknown fixture event type: {fixture_path}")
        fixture_types.add(event_type)
        envelope_validator.validate(fixture)
        event_schema = _load_json(schema_root / EVENT_SCHEMA_FILES[event_type])
        Draft202012Validator(event_schema, format_checker=format_checker).validate(fixture)

    if fixture_types != set(EVENT_SCHEMA_FILES):
        missing = sorted(set(EVENT_SCHEMA_FILES) - fixture_types)
        raise ValueError(f"missing fixture event types: {', '.join(missing)}")
    return len(fixtures), len(schemas)


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    try:
        fixture_count, schema_count = validate_contract(repository_root)
    except (OSError, json.JSONDecodeError, SchemaError, ValidationError, ValueError) as exc:
        print(f"meeting contract validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "meeting contract validated: "
        f"{fixture_count} fixtures, {schema_count} JSON schemas, contract_version=1"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
