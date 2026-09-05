from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from sona.meeting.api import _segment_json, _speaker_json
from sona.meeting.models import NormalizedSegment
from sona.meeting.repository import PostgresMeetingRepository
from sona.meeting.session import MeetingSession

MEETING_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SPEAKER_KEY = "group:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb:speaker:spk_01"


def _speaker(*, display_name: str | None = None, raw_speaker: str = "spk_01") -> SimpleNamespace:
    default_label = f"说话人 {raw_speaker}"
    return SimpleNamespace(
        speaker_key=SPEAKER_KEY,
        original_speaker=raw_speaker,
        raw_speaker=raw_speaker,
        default_label=default_label,
        display_name=display_name or default_label,
    )


def _segment(*, speaker_key: str = SPEAKER_KEY) -> NormalizedSegment:
    return NormalizedSegment(
        order=0,
        source_epoch=1,
        speaker_key=speaker_key,
        start_ms=0,
        end_ms=1000,
        text="已确认。",
    )


def test_rest_and_ws_labels_hide_internal_key_and_match() -> None:
    speaker = _speaker()
    segment = _segment()

    rest = _segment_json(segment, (speaker,))
    websocket = MeetingSession._segment_payload(segment)

    assert rest["speaker_name"] == "说话人 1"
    assert websocket["speaker_name"] == rest["speaker_name"]
    assert SPEAKER_KEY not in str(rest["speaker_name"])
    assert SPEAKER_KEY not in str(websocket["speaker_name"])


def test_historical_default_label_is_normalized_without_overwriting_custom_name() -> None:
    speaker = _speaker(display_name="主持人")

    payload = _speaker_json(speaker)

    assert payload["default_label"] == "说话人 1"
    assert payload["display_name"] == "主持人"

    legacy_key_label = _speaker()
    legacy_key_label.default_label = f"说话人 {SPEAKER_KEY}"
    legacy_key_label.display_name = legacy_key_label.default_label
    assert _speaker_json(legacy_key_label)["display_name"] == "说话人 1"


@pytest.mark.parametrize(
    ("speaker_key", "raw_speaker"),
    [
        ("epoch:7:speaker:0", "0"),
        ("group:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb:speaker:spk_voice", "spk_voice"),
    ],
)
def test_unknown_or_anonymous_keys_use_friendly_label(
    speaker_key: str, raw_speaker: str
) -> None:
    speaker = SimpleNamespace(
        speaker_key=speaker_key,
        original_speaker=raw_speaker,
        raw_speaker=raw_speaker,
        default_label=f"说话人 {raw_speaker}",
        display_name=f"说话人 {raw_speaker}",
    )
    segment = _segment(speaker_key=speaker_key)

    assert _speaker_json(speaker)["display_name"] == "未识别说话人"
    assert _segment_json(segment, (speaker,))["speaker_name"] == "未识别说话人"
    assert MeetingSession._speaker_name_from_key(speaker_key) == "未识别说话人"


@pytest.mark.asyncio
async def test_ws_fallback_ignores_historical_default_but_keeps_custom_name() -> None:
    class _Repository:
        def __init__(self, display_name: str) -> None:
            self.display_name = display_name

        async def get_speakers(self, _meeting_id: UUID) -> tuple[SimpleNamespace, ...]:
            return (
                SimpleNamespace(
                    speaker_key=SPEAKER_KEY,
                    display_name=self.display_name,
                    default_label="说话人 spk_01",
                ),
            )

    segment = _segment()
    session = object.__new__(MeetingSession)
    session.repository = _Repository("说话人 spk_01")
    names = await session._load_speaker_names(MEETING_ID)
    assert names == {}
    assert session._segment_payload(segment, names)["speaker_name"] == "说话人 1"

    session.repository = _Repository("主持人")
    names = await session._load_speaker_names(MEETING_ID)
    assert names == {SPEAKER_KEY: "主持人"}
    assert session._segment_payload(segment, names)["speaker_name"] == "主持人"


class _Cursor:
    async def fetchone(self) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, params: tuple[object, ...]) -> _Cursor:
        self.calls.append((query, params))
        return _Cursor()


@pytest.mark.asyncio
async def test_repository_upsert_uses_normalized_default_label() -> None:
    repository = object.__new__(PostgresMeetingRepository)
    repository._schema = '"sona"'
    connection = _Connection()

    await repository._upsert_speaker(connection, MEETING_ID, _segment())

    params = connection.calls[-1][1]
    assert params[3:] == ("spk_01", "说话人 1", "说话人 1")
