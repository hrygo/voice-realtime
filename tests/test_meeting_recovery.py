"""会议存储故障时的 transient recovery journal 测试。"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from psycopg import AsyncConnection

from voice_realtime.config import MeetingSettings
from voice_realtime.meeting.migrations import run_migrations
from voice_realtime.meeting.models import MeetingStatus, NormalizedSegment, TranscriptWindow
from voice_realtime.meeting.recovery import (
    RecoveryEnvelope,
    RecoveryJournal,
    RecoveryJournalError,
)
from voice_realtime.meeting.repository import PostgresMeetingRepository


def _test_database_url() -> str:
    value = os.environ.get("VR_TEST_DATABASE_URL")
    if not value:
        pytest.skip("VR_TEST_DATABASE_URL 未设置；跳过真实 PostgreSQL 集成测试")
    return value


class FakeRecoveryRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.error: Exception | None = None

    async def get_meeting(self, meeting_id) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append(("get_meeting", meeting_id))

    async def reconcile_window(self, meeting_id, window) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append(("reconcile_window", (meeting_id, window)))

    async def set_status(self, meeting_id, status, *, reason=None) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append(("set_status", (meeting_id, status, reason)))

    async def finalize_transcript(self, meeting_id) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append(("finalize_transcript", meeting_id))

    async def create_minutes(self, meeting_id, *, idempotency_key) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append(("create_minutes", (meeting_id, idempotency_key)))


def _window() -> TranscriptWindow:
    return TranscriptWindow(
        source_epoch=1,
        segments=(
            NormalizedSegment(
                id=uuid4(),
                order=0,
                source_epoch=1,
                speaker_key="e1:s1",
                start_ms=10,
                end_ms=100,
                text="可回放内容",
            ),
        ),
    )


@pytest_asyncio.fixture
async def repository_and_schema(
    tmp_path: Path,
) -> AsyncIterator[tuple[PostgresMeetingRepository, str]]:
    database_url = _test_database_url()
    schema = f"vr_recovery_{uuid4().hex[:16]}"
    async with await AsyncConnection.connect(database_url, autocommit=True) as admin:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
    await run_migrations(database_url, schema=schema)
    settings = MeetingSettings(
        database_url=database_url,
        schema=schema,
        recovery_dir=tmp_path / "recovery",
    )
    repo = PostgresMeetingRepository(settings)
    await repo.open()
    try:
        yield repo, schema
    finally:
        await repo.close()
        async with await AsyncConnection.connect(database_url, autocommit=True) as admin:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.asyncio
async def test_journal_replay_is_idempotent_and_removes_file(
    tmp_path: Path,
    repository_and_schema: tuple[PostgresMeetingRepository, str],
) -> None:
    repository, _schema = repository_and_schema
    meeting = await repository.create_meeting("恢复", language="Chinese", audio_source="microphone")
    journal = RecoveryJournal(tmp_path / "journal")
    window = TranscriptWindow(
        source_epoch=1,
        segments=(
            NormalizedSegment(
                id=uuid4(),
                order=0,
                source_epoch=1,
                speaker_key="e1:s1",
                start_ms=10,
                end_ms=100,
                text="journal 内容",
            ),
        ),
    )

    await journal.append(
        meeting.id,
        "reconcile_window",
        {"window": window.model_dump(mode="json")},
    )
    files = list((tmp_path / "journal").glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600

    assert await journal.replay(repository) == 1
    assert await journal.replay(repository) == 0
    assert not files[0].exists()
    assert (await repository.get_transcript(meeting.id)).segments[0].text == "journal 内容"


async def test_journal_replay_discards_residual_file_for_terminal_meeting(
    tmp_path: Path,
) -> None:
    class TerminalRepository(FakeRecoveryRepository):
        async def get_meeting(self, _meeting_id):
            return type("Record", (), {"status": MeetingStatus.COMPLETED})()

    journal = RecoveryJournal(tmp_path / "journal")
    repository = TerminalRepository()
    meeting_id = uuid4()
    await journal.append(meeting_id, "finalize_transcript")
    path = tmp_path / "journal" / f"{meeting_id}.jsonl"

    assert await journal.replay(repository) == 1

    assert not path.exists()
    assert repository.calls == []


@pytest.mark.asyncio
async def test_journal_append_sequence_is_monotonic(tmp_path: Path) -> None:
    journal = RecoveryJournal(tmp_path / "journal")
    meeting_id = uuid4()

    first = await journal.append(meeting_id, "mark", {"value": 1})
    second = await journal.append(meeting_id, "mark", {"value": 2})

    assert first.sequence == 1
    assert second.sequence == 2
    journal_path = tmp_path / "journal" / f"{meeting_id}.jsonl"
    payloads = [json.loads(line) for line in journal_path.read_text().splitlines()]
    assert [payload["sequence"] for payload in payloads] == [1, 2]


async def test_append_accepts_transcript_window_and_default_payload(tmp_path: Path) -> None:
    journal = RecoveryJournal(tmp_path / "journal")
    meeting_id = uuid4()
    window = _window()

    envelope = await journal.append(meeting_id, window)
    default_payload = await journal.append(meeting_id, " set_status ")

    assert envelope.operation == "reconcile_window"
    assert envelope.payload["window"] == window.model_dump(mode="json")
    assert default_payload.operation == "set_status"
    assert default_payload.payload == {}


@pytest.mark.parametrize("operation", ["", " " , "x" * 129])
async def test_append_rejects_invalid_operation(tmp_path: Path, operation: str) -> None:
    with pytest.raises(ValueError, match="operation"):
        await RecoveryJournal(tmp_path / "journal").append(uuid4(), operation)


async def test_replay_dispatches_every_supported_operation_and_deletes_file(
    tmp_path: Path,
) -> None:
    journal = RecoveryJournal(tmp_path / "journal")
    repository = FakeRecoveryRepository()
    meeting_id = uuid4()
    window = _window()

    await journal.append(
        meeting_id,
        "reconcile_window",
        {"window": window.model_dump(mode="json")},
    )
    await journal.append(
        meeting_id,
        "set_status",
        {"status": "interrupted", "reason": "ASR reconnect"},
    )
    await journal.append(meeting_id, "finalize_transcript")
    await journal.append(meeting_id, "create_minutes", {"idempotency_key": "meeting:v1"})

    assert await journal.replay(repository) == 4
    assert [call[0] for call in repository.calls] == [
        "get_meeting",
        "reconcile_window",
        "set_status",
        "finalize_transcript",
        "create_minutes",
    ]
    assert repository.calls[1][1][1] == window
    assert repository.calls[2][1][1].value == "interrupted"
    assert repository.calls[2][1][2] == "ASR reconnect"
    assert repository.calls[4][1][1] == "meeting:v1"
    assert list((tmp_path / "journal").glob("*.jsonl")) == []


async def test_replay_and_discard_are_noops_without_journal_directory(tmp_path: Path) -> None:
    journal = RecoveryJournal(tmp_path / "missing")

    assert await journal.replay(FakeRecoveryRepository()) == 0
    await journal.discard(uuid4())


async def test_discard_removes_only_the_selected_meeting(tmp_path: Path) -> None:
    journal = RecoveryJournal(tmp_path / "journal")
    first_id = uuid4()
    second_id = uuid4()
    await journal.append(first_id, "mark", {"value": 1})
    await journal.append(second_id, "mark", {"value": 2})

    await journal.discard(first_id)

    assert not (tmp_path / "journal" / f"{first_id}.jsonl").exists()
    assert (tmp_path / "journal" / f"{second_id}.jsonl").exists()


@pytest.mark.parametrize(
    ("operation", "payload", "message"),
    [
        ("reconcile_window", {}, "payload 无效"),
        ("reconcile_window", {"window": []}, "payload 无效"),
        (
            "reconcile_window",
            {"window": {"source_epoch": 1, "unexpected": True}},
            "payload 无效",
        ),
        ("set_status", {"status": "invalid"}, "status 无效"),
        ("set_status", {"status": "interrupted", "reason": 42}, "reason 无效"),
        ("create_minutes", {"idempotency_key": 42}, "idempotency_key 无效"),
        ("unknown", {}, "不支持的 recovery operation"),
    ],
)
async def test_replay_rejects_invalid_operations_and_payloads(
    operation: str,
    payload: dict[str, object],
    message: str,
) -> None:
    envelope = RecoveryEnvelope(
        meeting_id=uuid4(), sequence=1, operation=operation, payload=payload
    )

    with pytest.raises(RecoveryJournalError, match=message):
        await RecoveryJournal._replay_one(FakeRecoveryRepository(), envelope)


async def test_replay_preserves_file_when_repository_operation_fails(tmp_path: Path) -> None:
    journal = RecoveryJournal(tmp_path / "journal")
    meeting_id = uuid4()
    await journal.append(meeting_id, "finalize_transcript")
    repository = FakeRecoveryRepository()
    repository.error = RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="unavailable"):
        await journal.replay(repository)

    assert (tmp_path / "journal" / f"{meeting_id}.jsonl").exists()


async def test_replay_rejects_non_uuid_filename(tmp_path: Path) -> None:
    directory = tmp_path / "journal"
    directory.mkdir()
    (directory / "not-a-uuid.jsonl").write_text("{}\n")

    with pytest.raises(RecoveryJournalError, match="UUID"):
        await RecoveryJournal(directory).replay(FakeRecoveryRepository())


async def test_replay_rejects_invalid_json_and_keeps_file(tmp_path: Path) -> None:
    journal = RecoveryJournal(tmp_path / "journal")
    meeting_id = uuid4()
    await journal.append(meeting_id, "mark", {"value": 1})
    path = tmp_path / "journal" / f"{meeting_id}.jsonl"
    path.write_text("not-json\n")

    with pytest.raises(RecoveryJournalError, match="无效 JSON"):
        await journal.replay(FakeRecoveryRepository())

    assert path.exists()


@pytest.mark.parametrize(
    "envelope_factory",
    [
        lambda meeting_id: RecoveryEnvelope(
            meeting_id=meeting_id, sequence=3, operation="finalize_transcript"
        ),
        lambda meeting_id: RecoveryEnvelope(
            meeting_id=uuid4(), sequence=2, operation="finalize_transcript"
        ),
    ],
)
async def test_replay_rejects_non_contiguous_or_wrong_meeting_id(
    tmp_path: Path, envelope_factory
) -> None:
    journal = RecoveryJournal(tmp_path / "journal")
    meeting_id = uuid4()
    await journal.append(meeting_id, "finalize_transcript")
    path = tmp_path / "journal" / f"{meeting_id}.jsonl"
    path.write_text(
        RecoveryEnvelope(
            meeting_id=meeting_id, sequence=1, operation="finalize_transcript"
        ).model_dump_json()
        + "\n"
        + envelope_factory(meeting_id).model_dump_json()
        + "\n"
    )

    with pytest.raises(RecoveryJournalError, match="不连续"):
        await journal.replay(FakeRecoveryRepository())


async def test_replay_ignores_blank_lines(tmp_path: Path) -> None:
    journal = RecoveryJournal(tmp_path / "journal")
    meeting_id = uuid4()
    await journal.append(meeting_id, "finalize_transcript")
    path = tmp_path / "journal" / f"{meeting_id}.jsonl"
    path.write_text(path.read_text() + "\n\n")

    assert await journal.replay(FakeRecoveryRepository()) == 1


async def test_append_reports_filesystem_write_error(tmp_path: Path, monkeypatch) -> None:
    import voice_realtime.meeting.recovery as recovery_module

    def fail_open(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(recovery_module.os, "open", fail_open)

    with pytest.raises(RecoveryJournalError, match="写入"):
        await RecoveryJournal(tmp_path / "journal").append(uuid4(), "mark")


async def test_append_closes_file_when_fsync_setup_fails(tmp_path: Path, monkeypatch) -> None:
    import voice_realtime.meeting.recovery as recovery_module

    def fail_fchmod(*_args, **_kwargs):
        raise OSError("cannot change mode")

    monkeypatch.setattr(recovery_module.os, "fchmod", fail_fchmod)

    with pytest.raises(RecoveryJournalError, match="写入"):
        await RecoveryJournal(tmp_path / "journal").append(uuid4(), "mark")


async def test_append_reports_directory_permission_error(tmp_path: Path, monkeypatch) -> None:
    def fail_chmod(_self: Path, _mode: int) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "chmod", fail_chmod)

    with pytest.raises(RecoveryJournalError, match="权限"):
        await RecoveryJournal(tmp_path / "journal").append(uuid4(), "mark")


def test_read_reports_filesystem_read_error(tmp_path: Path) -> None:
    with pytest.raises(RecoveryJournalError, match="读取"):
        RecoveryJournal._read(tmp_path / "missing.jsonl", uuid4())


async def test_replay_reports_unlink_error_and_keeps_file(tmp_path: Path, monkeypatch) -> None:
    journal = RecoveryJournal(tmp_path / "journal")
    meeting_id = uuid4()
    await journal.append(meeting_id, "finalize_transcript")
    path = tmp_path / "journal" / f"{meeting_id}.jsonl"
    original_unlink = Path.unlink

    def fail_unlink(candidate: Path, *, missing_ok: bool = False) -> None:
        if candidate == path:
            raise OSError("busy")
        original_unlink(candidate, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    with pytest.raises(RecoveryJournalError, match="删除"):
        await journal.replay(FakeRecoveryRepository())

    assert path.exists()
