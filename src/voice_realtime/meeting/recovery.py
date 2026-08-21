"""PostgreSQL 暂时不可用时的 append-only 文本恢复 journal。"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .models import MeetingStatus, TranscriptWindow
from .repository import MeetingRepository


class RecoveryJournalError(RuntimeError):
    """journal 文件损坏、权限不足或包含不支持的操作。"""


class RecoveryEnvelope(BaseModel):
    """单条可重放的结构化文本变更。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    meeting_id: UUID
    sequence: int = Field(ge=1)
    operation: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RecoveryJournal:
    """每个会议一个 0600 JSONL 文件的 transient journal。

    journal 只保存规范化转录变更和生命周期字段；成功回放后才删除文件，
    因而不会成为 PostgreSQL 事实源或隐式音频缓存。
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._directory_lock = asyncio.Lock()

    async def append(
        self,
        meeting_id: UUID,
        operation: str | TranscriptWindow,
        payload: dict[str, Any] | None = None,
    ) -> RecoveryEnvelope:
        """持久化一条 journal 记录并在返回前完成 fsync。"""
        if isinstance(operation, TranscriptWindow):
            window = operation
            operation, payload = "reconcile_window", {"window": window.model_dump(mode="json")}
        if payload is None:
            payload = {}
        operation = operation.strip()
        if not operation or len(operation) > 128:
            raise ValueError("journal operation 无效")
        lock = self._locks.setdefault(meeting_id, asyncio.Lock())
        async with lock:
            await self._ensure_directory()
            path = self._path(meeting_id)
            sequence = self._next_sequence(path)
            envelope = RecoveryEnvelope(
                meeting_id=meeting_id,
                sequence=sequence,
                operation=operation,
                payload=payload,
            )
            encoded = (envelope.model_dump_json() + "\n").encode("utf-8")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "ab", closefd=True) as handle:
                        handle.write(encoded)
                        handle.flush()
                        os.fsync(handle.fileno())
                except BaseException:
                    # fdopen 未接管 fd 时保证不泄漏；常规路径由 with 关闭。
                    with contextlib.suppress(OSError):
                        os.close(fd)
                    raise
            except OSError as exc:
                raise RecoveryJournalError("无法写入 recovery journal") from exc
            return envelope

    async def replay(self, repository: MeetingRepository) -> int:
        """按会议与序号回放残留 journal，成功后删除对应文件。"""
        if not self.directory.exists():
            return 0
        total = 0
        for path in sorted(self.directory.glob("*.jsonl")):
            try:
                meeting_id = UUID(path.stem)
            except ValueError as exc:
                raise RecoveryJournalError("journal 文件名不是会议 UUID") from exc
            lock = self._locks.setdefault(meeting_id, asyncio.Lock())
            async with lock:
                envelopes = self._read(path, meeting_id)
                for envelope in envelopes:
                    await self._replay_one(repository, envelope)
                self._unlink(meeting_id)
                total += len(envelopes)
        return total

    async def discard(self, meeting_id: UUID) -> None:
        """仅删除指定会议的 journal；调用方应在 PG 事务成功后调用。"""
        lock = self._locks.setdefault(meeting_id, asyncio.Lock())
        async with lock:
            self._unlink(meeting_id)

    async def _ensure_directory(self) -> None:
        async with self._directory_lock:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                self.directory.chmod(0o700)
            except OSError as exc:
                raise RecoveryJournalError("无法设置 recovery journal 目录权限") from exc

    def _path(self, meeting_id: UUID) -> Path:
        return self.directory / f"{meeting_id}.jsonl"

    @staticmethod
    def _next_sequence(path: Path) -> int:
        if not path.exists():
            return 1
        envelopes = RecoveryJournal._read(path, UUID(path.stem))
        return envelopes[-1].sequence + 1 if envelopes else 1

    @staticmethod
    def _read(path: Path, meeting_id: UUID) -> list[RecoveryEnvelope]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise RecoveryJournalError("无法读取 recovery journal") from exc
        envelopes: list[RecoveryEnvelope] = []
        expected = 1
        for line in lines:
            if not line.strip():
                continue
            try:
                envelope = RecoveryEnvelope.model_validate_json(line)
            except ValueError as exc:
                raise RecoveryJournalError("recovery journal 包含无效 JSON") from exc
            if envelope.meeting_id != meeting_id or envelope.sequence != expected:
                raise RecoveryJournalError("recovery journal 序号或会议 ID 不连续")
            envelopes.append(envelope)
            expected += 1
        return envelopes

    @staticmethod
    async def _replay_one(repository: MeetingRepository, envelope: RecoveryEnvelope) -> None:
        payload = envelope.payload
        if envelope.operation == "reconcile_window":
            window_payload = payload.get("window")
            if not isinstance(window_payload, dict):
                raise RecoveryJournalError("reconcile_window payload 无效")
            try:
                window = TranscriptWindow.model_validate(window_payload)
            except ValueError as exc:
                raise RecoveryJournalError("reconcile_window payload 无效") from exc
            await repository.reconcile_window(envelope.meeting_id, window)
            return
        if envelope.operation == "set_status":
            try:
                status = MeetingStatus(str(payload.get("status")))
            except ValueError as exc:
                raise RecoveryJournalError("set_status status 无效") from exc
            reason = payload.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise RecoveryJournalError("set_status reason 无效")
            await repository.set_status(envelope.meeting_id, status, reason=reason)
            return
        if envelope.operation == "finalize_transcript":
            await repository.finalize_transcript(envelope.meeting_id)
            return
        if envelope.operation == "create_minutes":
            key = payload.get("idempotency_key")
            if key is not None and not isinstance(key, str):
                raise RecoveryJournalError("create_minutes idempotency_key 无效")
            await repository.create_minutes(envelope.meeting_id, idempotency_key=key)
            return
        raise RecoveryJournalError(f"不支持的 recovery operation: {envelope.operation}")

    def _unlink(self, meeting_id: UUID) -> None:
        try:
            self._path(meeting_id).unlink(missing_ok=True)
        except OSError as exc:
            raise RecoveryJournalError("无法删除已回放 recovery journal") from exc
