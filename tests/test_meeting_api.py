from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator, FormatChecker

from sona.meeting.api import MeetingAPIError, install_meeting_api
from sona.meeting.models import MinutesResult

MEETING_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SEGMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
MINUTES_ID = UUID("22222222-2222-4222-8222-222222222222")


def _meeting() -> SimpleNamespace:
    now = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    speaker = SimpleNamespace(
        speaker_key="e1:s1",
        original_speaker="1",
        default_label="说话人 1",
        display_name="说话人 1",
        updated_at=now,
    )
    return SimpleNamespace(
        id=MEETING_ID,
        title="周会",
        status="completed",
        language="Chinese",
        audio_source="microphone",
        started_at=now,
        ended_at=now,
        transcript_revision=3,
        content_revision=3,
        interruption_reason=None,
        metadata={},
        created_at=now,
        updated_at=now,
        speakers={"e1:s1": speaker},
        latest_minutes=None,
    )


def _minutes(
    *,
    status: str = "completed",
    source_content_revision: int = 2,
    content_json: Any = None,
) -> SimpleNamespace:
    now = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    return SimpleNamespace(
        id=MINUTES_ID,
        meeting_id=MEETING_ID,
        version=2,
        status=status,
        source_content_revision=source_content_revision,
        model="qwen3-35b",
        prompt_version="v1",
        content_json=content_json or {"overview": "已确认安排"},
        content_markdown="# 纪要\n\n已确认安排",
        raw_output='{"overview":"已确认安排"}',
        error_code=None,
        error_message=None,
        created_at=now,
    )


def _app(repo: Any | None = None, runtime: Any | None = None) -> FastAPI:
    app = FastAPI()
    if repo is not None:
        app.state.meeting_repository = repo
    if runtime is not None:
        app.state.meeting_runtime = runtime
    install_meeting_api(app)
    return app


async def _request(
    app: FastAPI,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, **kwargs)


class _Repo:
    async def list_meetings(self, *, cursor: str | None, limit: int) -> SimpleNamespace:
        return SimpleNamespace(items=[_meeting()], next_cursor=None)

    async def get_meeting(self, _meeting_id: UUID) -> SimpleNamespace:
        return _meeting()

    async def get_transcript(self, _meeting_id: UUID) -> SimpleNamespace:
        return SimpleNamespace(
            meeting_id=MEETING_ID,
            transcript_revision=3,
            content_revision=3,
            segments=(
                SimpleNamespace(
                    id=SEGMENT_ID,
                    order=0,
                    speaker_key="e1:s1",
                    start_ms=0,
                    end_ms=1000,
                    text="确认。",
                    translation=None,
                    detected_language="zh",
                    source_epoch=1,
                ),
            ),
        )


class _AllRoutesRepo(_Repo):
    def __init__(self, meeting: SimpleNamespace | None = None) -> None:
        self.meeting = meeting or _meeting()
        self.speaker_records = tuple(getattr(self.meeting, "speakers", {}).values())
        self.minutes = _minutes()
        self.last_idempotency_key: str | None = None
        self.deleted_id: UUID | None = None

    async def list_meetings(self, *, cursor: str | None, limit: int) -> SimpleNamespace:
        self.list_args = (cursor, limit)
        return SimpleNamespace(items=[self.meeting], next_cursor="next-page")

    async def get_meeting(self, _meeting_id: UUID) -> SimpleNamespace:
        return self.meeting

    async def get_transcript(self, _meeting_id: UUID) -> SimpleNamespace:
        document = await super().get_transcript(_meeting_id)
        document.speakers = tuple(self.meeting.speakers.values())
        return document

    async def get_speakers(self, _meeting_id: UUID) -> tuple[SimpleNamespace, ...]:
        return self.speaker_records

    async def get_latest_minutes(self, _meeting_id: UUID) -> SimpleNamespace:
        return self.minutes

    async def update_title(self, _meeting_id: UUID, title: str) -> SimpleNamespace:
        self.meeting.title = title
        return self.meeting

    async def rename_speaker(
        self, _meeting_id: UUID, speaker_key: str, display_name: str
    ) -> SimpleNamespace:
        self.meeting.speakers[speaker_key].display_name = display_name
        return self.meeting

    async def create_minutes(
        self, _meeting_id: UUID, *, idempotency_key: str | None
    ) -> SimpleNamespace:
        self.last_idempotency_key = idempotency_key
        return self.minutes

    async def get_minutes(self, _meeting_id: UUID, version: int) -> SimpleNamespace | None:
        return self.minutes if version == self.minutes.version else None

    async def delete_meeting(self, meeting_id: UUID) -> None:
        self.deleted_id = meeting_id


@pytest.mark.asyncio
async def test_transcript_response_exposes_public_speaker_fields() -> None:
    app = FastAPI()
    app.state.meeting_repository = _Repo()
    install_meeting_api(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/v1/meetings/{MEETING_ID}/transcript")
    assert response.status_code == 200
    body = response.json()
    assert body["transcript_revision"] == 3
    assert body["segments"][0]["speaker_name"] == "说话人 1"


@pytest.mark.asyncio
async def test_api_not_found_uses_stable_error_envelope() -> None:
    class MissingRepo(_Repo):
        async def get_meeting(self, _meeting_id: UUID) -> None:
            return None

    app = FastAPI()
    app.state.meeting_repository = MissingRepo()
    install_meeting_api(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/v1/meetings/{MEETING_ID}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_rename_speaker_refetches_repository_speaker() -> None:
    class RepositoryWithoutEmbeddedSpeakers(_Repo):
        async def rename_speaker(
            self, _meeting_id: UUID, _speaker_key: str, _display_name: str
        ) -> SimpleNamespace:
            meeting = _meeting()
            del meeting.speakers
            return meeting

        async def get_meeting(self, _meeting_id: UUID) -> SimpleNamespace:
            meeting = _meeting()
            del meeting.speakers
            return meeting

        async def get_speakers(self, _meeting_id: UUID) -> tuple[SimpleNamespace, ...]:
            speaker = _meeting().speakers["e1:s1"]
            speaker.display_name = "张三"
            return (speaker,)

    app = FastAPI()
    app.state.meeting_repository = RepositoryWithoutEmbeddedSpeakers()
    install_meeting_api(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            f"/api/v1/meetings/{MEETING_ID}/speakers/e1:s1",
            json={"display_name": "张三"},
        )
    assert response.status_code == 200
    assert response.json()["display_name"] == "张三"


@pytest.mark.asyncio
async def test_runtime_and_list_routes_project_json_and_pagination() -> None:
    repo = _AllRoutesRepo()
    observed_at = datetime(2026, 8, 21, 10, 1, tzinfo=UTC)
    runtime = SimpleNamespace(
        snapshot=lambda: {
            "mode": "idle",
            "revision": 2,
            "meeting_id": MEETING_ID,
            "observed_at": observed_at,
            "tags": ("meeting", "ready"),
        }
    )
    app = _app(repo, runtime)

    runtime_response = await _request(app, "GET", "/api/v1/runtime")
    list_response = await _request(app, "GET", "/api/v1/meetings?cursor=abc&limit=3")

    assert runtime_response.status_code == 200
    assert runtime_response.json() == {
        "mode": "idle",
        "revision": 2,
        "meeting_id": str(MEETING_ID),
        "observed_at": "2026-08-21T10:01:00Z",
        "tags": ["meeting", "ready"],
    }
    assert list_response.status_code == 200
    assert list_response.json()["next_cursor"] == "next-page"
    assert list_response.json()["items"][0]["id"] == str(MEETING_ID)
    assert repo.list_args == ("abc", 3)


@pytest.mark.asyncio
async def test_missing_dependencies_and_validation_use_stable_error_envelope() -> None:
    runtime_response = await _request(_app(), "GET", "/api/v1/runtime")
    storage_response = await _request(
        _app(),
        "GET",
        "/api/v1/meetings",
        headers={"X-Request-ID": "request-123"},
    )
    invalid_query = await _request(
        _app(_Repo()), "GET", "/api/v1/meetings?limit=0", headers={"X-Request-ID": "bad-limit"}
    )

    assert runtime_response.status_code == 503
    assert runtime_response.json()["error"]["code"] == "service_unavailable"
    assert storage_response.status_code == 503
    assert storage_response.json() == {
        "error": {
            "code": "storage_unavailable",
            "message": "会议存储暂不可用",
            "request_id": "request-123",
            "details": {},
        }
    }
    assert invalid_query.status_code == 422
    assert invalid_query.json()["error"]["code"] == "invalid_request"
    assert invalid_query.json()["error"]["request_id"] == "bad-limit"


@pytest.mark.asyncio
async def test_runtime_rejects_non_object_snapshots() -> None:
    runtime = SimpleNamespace(snapshot=lambda: ["invalid"])
    response = await _request(_app(runtime=runtime), "GET", "/api/v1/runtime")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


@pytest.mark.asyncio
async def test_list_and_transcript_repository_errors_are_mapped() -> None:
    class InvalidCursorError(RuntimeError):
        pass

    class MeetingNotFoundError(RuntimeError):
        pass

    class InvalidCursorRepo(_Repo):
        async def list_meetings(self, *, cursor: str | None, limit: int) -> SimpleNamespace:
            del cursor, limit
            raise InvalidCursorError("bad cursor")

    class MissingTranscriptRepo(_Repo):
        async def get_transcript(self, _meeting_id: UUID) -> SimpleNamespace:
            raise MeetingNotFoundError("missing transcript")

    invalid_cursor = await _request(_app(InvalidCursorRepo()), "GET", "/api/v1/meetings")
    missing_transcript = await _request(
        _app(MissingTranscriptRepo()), "GET", f"/api/v1/meetings/{MEETING_ID}/transcript"
    )
    malformed_id = await _request(_app(_Repo()), "GET", "/api/v1/meetings/not-a-uuid/transcript")

    assert invalid_cursor.status_code == 400
    assert invalid_cursor.json()["error"]["code"] == "invalid_request"
    assert missing_transcript.status_code == 404
    assert missing_transcript.json()["error"]["code"] == "not_found"
    assert malformed_id.status_code == 422
    assert malformed_id.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_repository_failures_keep_stable_conflict_storage_and_custom_errors() -> None:
    class MeetingConflictError(RuntimeError):
        pass

    class DatabaseUnavailableError(RuntimeError):
        pass

    class ConflictRepo(_AllRoutesRepo):
        async def create_minutes(
            self, _meeting_id: UUID, *, idempotency_key: str | None
        ) -> SimpleNamespace:
            del idempotency_key
            raise MeetingConflictError("already queued")

    class StorageRepo(_AllRoutesRepo):
        async def get_transcript(self, _meeting_id: UUID) -> SimpleNamespace:
            raise DatabaseUnavailableError("database offline")

    class DetailedRepo(_AllRoutesRepo):
        async def get_meeting(self, _meeting_id: UUID) -> SimpleNamespace:
            raise MeetingAPIError(
                "rate_limited",
                "请求过于频繁",
                status_code=429,
                details={"retry_after_seconds": 3},
            )

    conflict = await _request(
        _app(ConflictRepo()), "POST", f"/api/v1/meetings/{MEETING_ID}/minutes"
    )
    storage = await _request(
        _app(StorageRepo()), "GET", f"/api/v1/meetings/{MEETING_ID}/transcript"
    )
    detailed = await _request(_app(DetailedRepo()), "GET", f"/api/v1/meetings/{MEETING_ID}")

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"
    assert conflict.json()["error"]["message"] == "操作冲突"
    assert storage.status_code == 503
    assert storage.json()["error"]["code"] == "storage_unavailable"
    assert detailed.status_code == 429
    assert detailed.json()["error"] == {
        "code": "rate_limited",
        "message": "请求过于频繁",
        "request_id": detailed.json()["error"]["request_id"],
        "details": {"retry_after_seconds": 3},
    }


@pytest.mark.asyncio
async def test_meeting_detail_loads_fallback_speakers_and_stale_minutes() -> None:
    repo = _AllRoutesRepo()
    del repo.meeting.speakers
    repo.meeting.ended_at = None
    repo.minutes.content_json = MinutesResult(overview="结构化纪要")
    response = await _request(_app(repo), "GET", f"/api/v1/meetings/{MEETING_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["ended_at"] is None
    assert body["speakers"]["e1:s1"]["display_name"] == "说话人 1"
    assert body["latest_minutes"]["version"] == 2
    assert body["latest_minutes"]["content_json"]["overview"] == "结构化纪要"
    assert body["latest_minutes"]["is_stale"] is True
    assert body["metadata"] == {}


@pytest.mark.asyncio
async def test_title_and_speaker_routes_update_and_validate_inputs() -> None:
    repo = _AllRoutesRepo()
    app = _app(repo)
    title_response = await _request(
        app, "PATCH", f"/api/v1/meetings/{MEETING_ID}", json={"title": "  复盘会  "}
    )
    speaker_response = await _request(
        app,
        "PATCH",
        f"/api/v1/meetings/{MEETING_ID}/speakers/e1:s1",
        json={"display_name": "  张三  "},
    )
    invalid_title = await _request(
        app, "PATCH", f"/api/v1/meetings/{MEETING_ID}", json={"title": "   "}
    )
    invalid_speaker = await _request(
        app,
        "PATCH",
        f"/api/v1/meetings/{MEETING_ID}/speakers/{'x' * 201}",
        json={"display_name": "张三"},
    )

    assert title_response.status_code == 200
    assert title_response.json()["title"] == "复盘会"
    assert speaker_response.status_code == 200
    assert speaker_response.json()["display_name"] == "张三"
    assert invalid_title.status_code == 422
    assert invalid_title.json()["error"]["code"] == "invalid_request"
    assert invalid_speaker.status_code == 400
    assert invalid_speaker.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_title_and_minutes_routes_accept_sync_repository_adapters() -> None:
    class SyncRepo(_AllRoutesRepo):
        def update_title(self, _meeting_id: UUID, title: str) -> SimpleNamespace:
            self.meeting.title = title
            return self.meeting

        def get_minutes(self, _meeting_id: UUID, version: int) -> SimpleNamespace | None:
            return self.minutes if version == 2 else None

    repo = SyncRepo()
    title = await _request(
        _app(repo), "PATCH", f"/api/v1/meetings/{MEETING_ID}", json={"title": "同步标题"}
    )
    minutes = await _request(
        _app(repo), "GET", f"/api/v1/meetings/{MEETING_ID}/minutes/2"
    )

    assert title.status_code == 200
    assert title.json()["title"] == "同步标题"
    assert minutes.status_code == 200
    assert minutes.json()["version"] == 2


@pytest.mark.asyncio
async def test_speaker_route_supports_direct_and_transcript_fallback_results() -> None:
    speaker = _meeting().speakers["e1:s1"]

    class DirectSpeakerRepo(_Repo):
        async def rename_speaker(
            self, _meeting_id: UUID, _speaker_key: str, display_name: str
        ) -> SimpleNamespace:
            speaker.display_name = display_name
            return speaker

    class TranscriptFallbackRepo(_Repo):
        async def rename_speaker(
            self, _meeting_id: UUID, _speaker_key: str, _display_name: str
        ) -> SimpleNamespace:
            meeting = _meeting()
            del meeting.speakers
            return meeting

        async def get_transcript(self, _meeting_id: UUID) -> SimpleNamespace:
            document = await super().get_transcript(_meeting_id)
            document.speakers = (speaker,)
            return document

    direct = await _request(
        _app(DirectSpeakerRepo()),
        "PATCH",
        f"/api/v1/meetings/{MEETING_ID}/speakers/e1:s1",
        json={"display_name": "李四"},
    )
    fallback = await _request(
        _app(TranscriptFallbackRepo()),
        "PATCH",
        f"/api/v1/meetings/{MEETING_ID}/speakers/e1:s1",
        json={"display_name": "王五"},
    )

    assert direct.status_code == 200
    assert direct.json()["display_name"] == "李四"
    assert fallback.status_code == 200
    assert fallback.json()["speaker_key"] == "e1:s1"


@pytest.mark.asyncio
async def test_missing_speaker_and_missing_update_method_are_stable_errors() -> None:
    class MissingSpeakerRepo(_Repo):
        async def rename_speaker(
            self, _meeting_id: UUID, _speaker_key: str, _display_name: str
        ) -> SimpleNamespace:
            meeting = _meeting()
            meeting.speakers = {}
            return meeting

    class NoTitleUpdateRepo(_Repo):
        update_title = None
        rename_meeting = None

    missing_speaker = await _request(
        _app(MissingSpeakerRepo()),
        "PATCH",
        f"/api/v1/meetings/{MEETING_ID}/speakers/e1:s1",
        json={"display_name": "张三"},
    )
    no_update_method = await _request(
        _app(NoTitleUpdateRepo()),
        "PATCH",
        f"/api/v1/meetings/{MEETING_ID}",
        json={"title": "新标题"},
    )

    assert missing_speaker.status_code == 404
    assert missing_speaker.json()["error"]["code"] == "not_found"
    assert no_update_method.status_code == 500
    assert no_update_method.json()["error"]["code"] == "internal_error"


@pytest.mark.asyncio
async def test_minutes_create_and_version_routes_preserve_contract_fields() -> None:
    repo = _AllRoutesRepo()
    app = _app(repo)
    created = await _request(
        app,
        "POST",
        f"/api/v1/meetings/{MEETING_ID}/minutes",
        headers={"Idempotency-Key": "request-minutes-1"},
    )
    version = await _request(app, "GET", f"/api/v1/meetings/{MEETING_ID}/minutes/2")
    invalid_version = await _request(app, "GET", f"/api/v1/meetings/{MEETING_ID}/minutes/0")
    missing_version = await _request(app, "GET", f"/api/v1/meetings/{MEETING_ID}/minutes/3")

    assert created.status_code == 200
    assert created.json()["status"] == "completed"
    assert repo.last_idempotency_key == "request-minutes-1"
    assert version.status_code == 200
    assert version.json()["version"] == 2
    assert version.json()["is_stale"] is True
    assert invalid_version.status_code == 400
    assert invalid_version.json()["error"]["code"] == "invalid_request"
    assert missing_version.status_code == 404
    assert missing_version.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_minutes_routes_handle_invalid_repository_shapes() -> None:
    class EmptyMinutesRepo(_Repo):
        async def create_minutes(
            self, _meeting_id: UUID, *, idempotency_key: str | None
        ) -> None:
            del idempotency_key
            return

    class NoVersionRepo(_Repo):
        pass

    empty = await _request(
        _app(EmptyMinutesRepo()), "POST", f"/api/v1/meetings/{MEETING_ID}/minutes"
    )
    no_version = await _request(
        _app(NoVersionRepo()), "GET", f"/api/v1/meetings/{MEETING_ID}/minutes/1"
    )

    assert empty.status_code == 500
    assert empty.json()["error"]["code"] == "internal_error"
    assert no_version.status_code == 404
    assert no_version.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_export_route_supports_markdown_text_srt_and_json() -> None:
    meeting = _meeting()
    meeting.title = "weekly/2026"
    repo = _AllRoutesRepo(meeting)
    app = _app(repo)

    markdown = await _request(
        app, "GET", f"/api/v1/meetings/{MEETING_ID}/export?format=md"
    )
    text = await _request(app, "GET", f"/api/v1/meetings/{MEETING_ID}/export?format=txt")
    srt = await _request(app, "GET", f"/api/v1/meetings/{MEETING_ID}/export?format=srt")
    json_export = await _request(
        app, "GET", f"/api/v1/meetings/{MEETING_ID}/export?format=json"
    )
    invalid_format = await _request(
        app, "GET", f"/api/v1/meetings/{MEETING_ID}/export?format=csv"
    )

    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert 'filename="weekly_2026.md"' in markdown.headers["content-disposition"]
    assert markdown.text.startswith("# weekly/2026\n")
    assert "- [00:00:00.000] 说话人 1: 确认。" in markdown.text
    assert text.status_code == 200
    assert text.headers["content-type"].startswith("text/plain")
    assert text.text.startswith("weekly/2026\n")
    assert "[00:00:00.000] 说话人 1: 确认。" in text.text
    assert srt.status_code == 200
    assert srt.headers["content-type"].startswith("application/x-subrip")
    assert "00:00:00,000 --> 00:00:01,000" in srt.text
    assert "说话人 1: 确认。" in srt.text
    assert json_export.status_code == 200
    exported = json_export.json()
    assert exported["meeting"]["id"] == str(MEETING_ID)
    assert exported["transcript"]["segments"][0]["text"] == "确认。"
    assert invalid_format.status_code == 422
    assert invalid_format.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_export_and_delete_routes_report_missing_or_conflicting_resources() -> None:
    class MissingRepo(_Repo):
        async def get_meeting(self, _meeting_id: UUID) -> None:
            return None

    missing_export = await _request(
        _app(MissingRepo()), "GET", f"/api/v1/meetings/{MEETING_ID}/export"
    )
    missing_delete = await _request(
        _app(MissingRepo()), "DELETE", f"/api/v1/meetings/{MEETING_ID}"
    )
    recording = _meeting()
    recording.status = "recording"
    conflict_delete = await _request(
        _app(_AllRoutesRepo(recording)), "DELETE", f"/api/v1/meetings/{MEETING_ID}"
    )
    completed_repo = _AllRoutesRepo()
    deleted = await _request(
        _app(completed_repo), "DELETE", f"/api/v1/meetings/{MEETING_ID}"
    )

    assert missing_export.status_code == 404
    assert missing_export.json()["error"]["code"] == "not_found"
    assert missing_delete.status_code == 404
    assert missing_delete.json()["error"]["code"] == "not_found"
    assert conflict_delete.status_code == 409
    assert conflict_delete.json()["error"]["code"] == "conflict"
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert completed_repo.deleted_id == MEETING_ID


def test_install_meeting_api_is_idempotent_and_keeps_explicit_dependencies() -> None:
    app = FastAPI()
    repo = _Repo()
    runtime = SimpleNamespace(snapshot=lambda: {"mode": "idle"})
    router = install_meeting_api(app, repository=repo, runtime=runtime)

    assert install_meeting_api(app) is router
    assert app.state.meeting_repository is repo
    assert app.state.meeting_runtime is runtime


def test_contract_artifacts_and_fixtures_are_loadable() -> None:
    root = Path("contracts/meeting-assistant/v1")
    json.loads((root / "openapi.json").read_text())
    assert (root / "asyncapi.yaml").read_text().startswith("asyncapi:")
    schema_files = list((root / "schemas").glob("*.schema.json"))
    fixture_files = list((root / "fixtures").glob("*.json"))
    assert schema_files
    assert fixture_files
    event_schema = json.loads((root / "schemas/event-envelope.schema.json").read_text())
    inner_os_event_schema = json.loads((root / "schemas/inner-os-event.schema.json").read_text())
    validator = Draft202012Validator(event_schema, format_checker=FormatChecker())
    inner_os_validator = Draft202012Validator(inner_os_event_schema, format_checker=FormatChecker())
    for fixture in fixture_files:
        value = json.loads(fixture.read_text())
        if fixture.name.startswith("inner-os-"):
            inner_os_validator.validate(value)
        else:
            validator.validate(value)


@pytest.mark.asyncio
async def test_non_meeting_validation_keeps_fastapi_default_shape() -> None:
    app = FastAPI()

    @app.get("/legacy/{value}")
    async def legacy(value: int) -> dict[str, int]:
        return {"value": value}

    install_meeting_api(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/legacy/not-an-int")

    assert response.status_code == 422
    assert "detail" in response.json()
    assert "error" not in response.json()


@pytest.mark.asyncio
async def test_generate_meeting_title_endpoint() -> None:
    app = FastAPI()
    repo = _AllRoutesRepo()

    async def fake_generate_title(doc: Any, speakers: Any = ()) -> str:
        return "AI提炼：语音交互架构讨论"

    summary_client = SimpleNamespace(generate_title=fake_generate_title)
    summary_service = SimpleNamespace(client=summary_client)
    install_meeting_api(app, repository=repo, summary_service=summary_service)

    class _Events:
        def __init__(self) -> None:
            self.items: list[tuple[str, UUID, dict[str, object]]] = []

        async def publish_event(
            self, event_type: str, meeting_id: UUID, payload: dict[str, object]
        ) -> None:
            self.items.append((event_type, meeting_id, payload))

    events = _Events()
    app.state.meeting_events = events

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/v1/meetings/{MEETING_ID}/generate-title")

    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "AI提炼：语音交互架构讨论"
    assert repo.meeting.title == "AI提炼：语音交互架构讨论"
    assert events.items == [
        (
            "meeting_title_updated",
            MEETING_ID,
            {"title": "AI提炼：语音交互架构讨论"},
        )
    ]
