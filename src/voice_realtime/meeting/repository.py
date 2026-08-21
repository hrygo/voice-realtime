"""会议助手 PostgreSQL repository。

所有写入都在显式事务中完成，并且 schema 名称只通过启动时的严格校验后
插入 SQL 标识符位置；用户可控文本始终作为参数传给 psycopg。
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from voice_realtime.config import MeetingSettings

from .migrations import validate_schema_name
from .models import (
    MeetingPage,
    MeetingRecord,
    MeetingStatus,
    MinutesJob,
    MinutesRecord,
    MinutesResult,
    MinutesStatus,
    NormalizedSegment,
    SpeakerRecord,
    TranscriptDocument,
    TranscriptReconcileResult,
    TranscriptWindow,
)

_MEETING_COLUMNS = """
    id, title, status, language, audio_source, started_at, ended_at,
    transcript_revision, content_revision, interruption_reason, metadata,
    created_at, updated_at
"""
_MINUTES_COLUMNS = """
    id, meeting_id, version, status, source_content_revision, model,
    prompt_version, content_json, content_markdown, raw_output, error_code,
    error_message, lease_until, attempts, created_at, generated_at, updated_at
"""
_MINUTES_COLUMNS_QUALIFIED = """
    minutes.id, minutes.meeting_id, minutes.version, minutes.status,
    minutes.source_content_revision, minutes.model, minutes.prompt_version,
    minutes.content_json, minutes.content_markdown, minutes.raw_output,
    minutes.error_code, minutes.error_message, minutes.lease_until,
    minutes.attempts, minutes.created_at, minutes.generated_at, minutes.updated_at
"""
_SEGMENT_COLUMNS = """
    id, segment_order, source_epoch, speaker_key, start_ms, end_ms, text,
    translation, detected_language, created_at, updated_at
"""


class MeetingRepositoryError(RuntimeError):
    """repository 层的稳定错误基类。"""


class MeetingNotFoundError(MeetingRepositoryError):
    """请求的会议或纪要不存在。"""


class MeetingConflictError(MeetingRepositoryError):
    """当前资源状态不允许执行请求。"""


class RepositoryUnavailableError(MeetingRepositoryError):
    """数据库暂不可用。"""


class InvalidCursorError(MeetingRepositoryError):
    """游标不是 repository 产生的有效值。"""


class MeetingRepository(Protocol):
    """运行时和 API 使用的最小异步 repository 契约。"""

    async def check_writable(self) -> bool: ...

    async def create_meeting(
        self, title: str, *, language: str, audio_source: str
    ) -> MeetingRecord: ...

    async def get_meeting(self, meeting_id: UUID) -> MeetingRecord | None: ...

    async def list_meetings(self, *, cursor: str | None, limit: int) -> MeetingPage: ...

    async def update_title(self, meeting_id: UUID, title: str) -> MeetingRecord: ...

    async def set_status(
        self, meeting_id: UUID, status: MeetingStatus, *, reason: str | None = None
    ) -> MeetingRecord: ...

    async def reconcile_window(
        self, meeting_id: UUID, window: TranscriptWindow
    ) -> TranscriptReconcileResult: ...

    async def finalize_transcript(
        self,
        meeting_id: UUID,
        *,
        final_status: MeetingStatus = MeetingStatus.COMPLETED,
        reason: str | None = None,
    ) -> MeetingRecord: ...

    async def get_transcript(self, meeting_id: UUID) -> TranscriptDocument: ...

    async def get_speakers(self, meeting_id: UUID) -> tuple[SpeakerRecord, ...]: ...

    async def get_latest_minutes(self, meeting_id: UUID) -> MinutesRecord | None: ...

    async def rename_speaker(
        self, meeting_id: UUID, speaker_key: str, display_name: str
    ) -> MeetingRecord: ...

    async def create_minutes(
        self, meeting_id: UUID, *, idempotency_key: str | None
    ) -> MinutesRecord: ...

    async def claim_minutes(self) -> MinutesJob | None: ...

    async def complete_minutes(self, minutes_id: UUID, result: MinutesResult) -> None: ...

    async def fail_minutes(
        self,
        minutes_id: UUID,
        *,
        code: str,
        message: str,
        raw_output: str | None = None,
    ) -> None: ...

    async def delete_meeting(self, meeting_id: UUID) -> None: ...

    async def close(self) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_title(value: str) -> str:
    title = value.strip()
    if not 1 <= len(title) <= 200:
        raise ValueError("会议标题长度必须为 1–200")
    if any(ord(char) < 32 for char in title):
        raise ValueError("会议标题不能包含控制字符")
    return title


def _validate_display_name(value: str) -> str:
    name = value.strip()
    if not 1 <= len(name) <= 200:
        raise ValueError("说话人名称长度必须为 1–200")
    if any(ord(char) < 32 for char in name):
        raise ValueError("说话人名称不能包含控制字符")
    return name


def _encode_cursor(created_at: datetime, meeting_id: UUID) -> str:
    payload = json.dumps(
        {"created_at": created_at.isoformat(), "id": str(meeting_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        meeting_id = UUID(str(payload["id"]))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("会议游标无效") from exc
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return created_at, meeting_id


def _meeting_from_row(row: Sequence[Any]) -> MeetingRecord:
    return MeetingRecord(
        id=cast(UUID, row[0]),
        title=str(row[1]),
        status=MeetingStatus(str(row[2])),
        language=str(row[3]),
        audio_source=str(row[4]),
        started_at=cast(datetime, row[5]),
        ended_at=cast(datetime | None, row[6]),
        transcript_revision=int(row[7]),
        content_revision=int(row[8]),
        interruption_reason=cast(str | None, row[9]),
        metadata=dict(cast(Mapping[str, Any], row[10] or {})),
        created_at=cast(datetime, row[11]),
        updated_at=cast(datetime, row[12]),
    )


def _minutes_from_row(row: Sequence[Any]) -> MinutesRecord:
    content = row[7]
    parsed_content = MinutesResult.model_validate(content) if content is not None else None
    return MinutesRecord(
        id=cast(UUID, row[0]),
        meeting_id=cast(UUID, row[1]),
        version=int(row[2]),
        status=MinutesStatus(str(row[3])),
        source_content_revision=int(row[4]),
        model=str(row[5]),
        prompt_version=str(row[6]),
        content_json=parsed_content,
        content_markdown=cast(str | None, row[8]),
        raw_output=cast(str | None, row[9]),
        error_code=cast(str | None, row[10]),
        error_message=cast(str | None, row[11]),
        lease_until=cast(datetime | None, row[12]),
        attempts=int(row[13]),
        created_at=cast(datetime, row[14]),
        generated_at=cast(datetime | None, row[15]),
        updated_at=cast(datetime, row[16]),
    )


class PostgresMeetingRepository:
    """使用 psycopg 3 异步连接池的会议 repository。"""

    def __init__(
        self,
        settings: MeetingSettings,
        *,
        pool: AsyncConnectionPool[Any] | None = None,
    ) -> None:
        self.settings = settings
        self.schema = validate_schema_name(settings.schema_name)
        self._schema = f'"{self.schema}"'
        self._pool: AsyncConnectionPool[Any] = pool or AsyncConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=max(2, settings.summary_concurrency + 1),
            open=False,
            kwargs={"row_factory": tuple_row},
        )
        self._owns_pool = pool is None
        self._opened = False

    async def open(self) -> None:
        """打开连接池；多次调用安全。"""
        if not self._opened:
            await self._pool.open(wait=True)
            self._opened = True

    async def start(self) -> None:
        """生命周期别名，供应用启动器使用。"""
        await self.open()

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        await self.open()
        async with self._pool.connection() as connection:
            yield connection

    async def check_writable(self) -> bool:
        try:
            async with self._connection() as connection:
                cursor = await connection.execute("SELECT 1")
                await cursor.fetchone()
                return True
        except Exception:
            return False

    async def create_meeting(
        self, title: str, *, language: str, audio_source: str
    ) -> MeetingRecord:
        title = _validate_title(title)
        language = language.strip()
        audio_source = audio_source.strip()
        if not language or len(language) > 32:
            raise ValueError("会议语言无效")
        if audio_source != "microphone":
            raise ValueError("首版会议只支持 microphone 音频源")
        meeting_id = uuid4()
        now = _utc_now()
        async with self._connection() as connection:  # noqa: SIM117
            async with connection.transaction():
                await connection.execute(
                    f"""
                    INSERT INTO {self._schema}.meetings
                        (id, title, status, language, audio_source, started_at,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        meeting_id,
                        title,
                        MeetingStatus.RECORDING.value,
                        language,
                        audio_source,
                        now,
                        now,
                        now,
                    ),
                )
                await self._insert_event(connection, meeting_id, "meeting_started", {})
                cursor = await connection.execute(
                    f"SELECT {_MEETING_COLUMNS} FROM {self._schema}.meetings WHERE id = %s",
                    (meeting_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RepositoryUnavailableError("会议创建后无法读取")
                return _meeting_from_row(row)

    async def get_meeting(self, meeting_id: UUID) -> MeetingRecord | None:
        async with self._connection() as connection:
            cursor = await connection.execute(
                f"SELECT {_MEETING_COLUMNS} FROM {self._schema}.meetings WHERE id = %s",
                (meeting_id,),
            )
            row = await cursor.fetchone()
            return _meeting_from_row(row) if row is not None else None

    async def list_meetings(self, *, cursor: str | None, limit: int) -> MeetingPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1–100 之间")
        cursor_values = _decode_cursor(cursor) if cursor else None
        async with self._connection() as connection:
            if cursor_values is None:
                query = (
                    f"SELECT {_MEETING_COLUMNS} FROM {self._schema}.meetings "
                    "ORDER BY created_at DESC, id DESC LIMIT %s"
                )
                params: tuple[Any, ...] = (limit + 1,)
            else:
                query = (
                    f"SELECT {_MEETING_COLUMNS} FROM {self._schema}.meetings "
                    "WHERE (created_at, id) < (%s, %s) "
                    "ORDER BY created_at DESC, id DESC LIMIT %s"
                )
                params = (*cursor_values, limit + 1)
            result = await connection.execute(query, params)
            rows = await result.fetchall()
        records = [_meeting_from_row(row) for row in rows]
        next_cursor = None
        if len(records) > limit:
            last = records[limit - 1]
            next_cursor = _encode_cursor(last.created_at, last.id)
            records = records[:limit]
        return MeetingPage(items=tuple(records), next_cursor=next_cursor)

    async def update_title(self, meeting_id: UUID, title: str) -> MeetingRecord:
        """更新会议标题；标题不改变内容 revision。"""
        title = _validate_title(title)
        async with self._connection() as connection, connection.transaction():
            meeting = await self._lock_meeting(connection, meeting_id)
            if meeting is None:
                raise MeetingNotFoundError("会议不存在")
            if meeting.title == title:
                return meeting
            now = _utc_now()
            cursor = await connection.execute(
                f"""
                UPDATE {self._schema}.meetings
                SET title = %s, updated_at = %s
                WHERE id = %s
                RETURNING {_MEETING_COLUMNS}
                """,
                (title, now, meeting_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RepositoryUnavailableError("标题更新后无法读取会议")
            await self._insert_event(
                connection,
                meeting_id,
                "meeting_title_updated",
                {"title": title},
            )
            return _meeting_from_row(row)

    async def set_status(
        self, meeting_id: UUID, status: MeetingStatus, *, reason: str | None = None
    ) -> MeetingRecord:
        if reason is not None:
            reason = reason.strip()
            if len(reason) > 128 or any(ord(char) < 32 for char in reason):
                raise ValueError("中断原因无效")
        async with self._connection() as connection, connection.transaction():
            meeting = await self._lock_meeting(connection, meeting_id)
            if meeting is None:
                raise MeetingNotFoundError("会议不存在")
            self._ensure_transition(meeting.status, status)
            now = _utc_now()
            ended_at = now if status in {
                MeetingStatus.COMPLETED,
                MeetingStatus.INTERRUPTED,
                MeetingStatus.STORAGE_ERROR,
            } else meeting.ended_at
            await connection.execute(
                f"""
                    UPDATE {self._schema}.meetings
                    SET status = %s, interruption_reason = %s, ended_at = %s, updated_at = %s
                    WHERE id = %s
                    """,
                (status.value, reason, ended_at, now, meeting_id),
            )
            await self._insert_event(
                connection,
                meeting_id,
                "meeting_state_changed",
                {"status": status.value, "reason": reason} if reason else {"status": status.value},
            )
            cursor = await connection.execute(
                f"SELECT {_MEETING_COLUMNS} FROM {self._schema}.meetings WHERE id = %s",
                (meeting_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RepositoryUnavailableError("状态更新后无法读取会议")
            return _meeting_from_row(row)

    async def reconcile_window(
        self, meeting_id: UUID, window: TranscriptWindow
    ) -> TranscriptReconcileResult:
        segments = tuple(
            sorted(window.segments, key=lambda item: (item.start_ms, item.end_ms, item.order))
        )
        replace_from_ms = min((segment.start_ms for segment in segments), default=0)
        async with self._connection() as connection, connection.transaction():
            meeting = await self._lock_meeting(connection, meeting_id)
            if meeting is None:
                raise MeetingNotFoundError("会议不存在")
            if meeting.status not in {MeetingStatus.RECORDING, MeetingStatus.FINALIZING}:
                raise MeetingConflictError("会议已不再接受转录")
            if not segments:
                return TranscriptReconcileResult(
                    meeting_id=meeting_id,
                    transcript_revision=meeting.transcript_revision,
                    content_revision=meeting.content_revision,
                    replace_from_ms=replace_from_ms,
                    segments=(),
                )
            current_cursor = await connection.execute(
                f"""
                SELECT {_SEGMENT_COLUMNS}
                FROM {self._schema}.transcript_segments
                WHERE meeting_id = %s AND end_ms >= %s
                ORDER BY start_ms, end_ms, segment_order
                """,
                (meeting_id, replace_from_ms),
            )
            current_rows = await current_cursor.fetchall()
            current_signature = [
                tuple(row[index] for index in range(0, 9)) for row in current_rows
            ]
            desired_signature = [
                (
                    segment.id,
                    segment.order,
                    segment.source_epoch,
                    segment.speaker_key,
                    segment.start_ms,
                    segment.end_ms,
                    segment.text,
                    segment.translation,
                    segment.detected_language,
                )
                for segment in segments
            ]
            if current_signature == desired_signature:
                return TranscriptReconcileResult(
                    meeting_id=meeting_id,
                    transcript_revision=meeting.transcript_revision,
                    content_revision=meeting.content_revision,
                    replace_from_ms=replace_from_ms,
                    segments=segments,
                )

            await connection.execute(
                f"DELETE FROM {self._schema}.transcript_segments "
                "WHERE meeting_id = %s AND end_ms >= %s",
                (meeting_id, replace_from_ms),
            )
            for segment in segments:
                await connection.execute(
                    f"""
                        INSERT INTO {self._schema}.transcript_segments
                            (id, meeting_id, segment_order, source_epoch, speaker_key,
                             start_ms, end_ms, text, translation, detected_language)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            segment_order = EXCLUDED.segment_order,
                            source_epoch = EXCLUDED.source_epoch,
                            speaker_key = EXCLUDED.speaker_key,
                            start_ms = EXCLUDED.start_ms,
                            end_ms = EXCLUDED.end_ms,
                            text = EXCLUDED.text,
                            translation = EXCLUDED.translation,
                            detected_language = EXCLUDED.detected_language,
                            updated_at = now()
                        """,
                    (
                        segment.id,
                        meeting_id,
                        segment.order,
                        segment.source_epoch,
                        segment.speaker_key,
                        segment.start_ms,
                        segment.end_ms,
                        segment.text,
                        segment.translation,
                        segment.detected_language,
                    ),
                )
                await self._upsert_speaker(connection, meeting_id, segment)
            now = _utc_now()
            transcript_revision = meeting.transcript_revision + 1
            content_revision = meeting.content_revision + 1
            await connection.execute(
                f"""
                    UPDATE {self._schema}.meetings
                    SET transcript_revision = %s, content_revision = %s, updated_at = %s
                    WHERE id = %s
                    """,
                (transcript_revision, content_revision, now, meeting_id),
            )
            await self._insert_event(
                connection,
                meeting_id,
                "transcript_reconciled",
                {
                    "transcript_revision": transcript_revision,
                    "content_revision": content_revision,
                    "replace_from_ms": replace_from_ms,
                    "segment_count": len(segments),
                },
            )
            return TranscriptReconcileResult(
                meeting_id=meeting_id,
                transcript_revision=transcript_revision,
                content_revision=content_revision,
                replace_from_ms=replace_from_ms,
                segments=segments,
            )

    async def finalize_transcript(
        self,
        meeting_id: UUID,
        *,
        final_status: MeetingStatus = MeetingStatus.COMPLETED,
        reason: str | None = None,
    ) -> MeetingRecord:
        if final_status not in {MeetingStatus.COMPLETED, MeetingStatus.INTERRUPTED}:
            raise ValueError("封存终态必须是 completed 或 interrupted")
        if reason is not None:
            reason = reason.strip()
            if len(reason) > 128 or any(ord(char) < 32 for char in reason):
                raise ValueError("中断原因无效")
        async with self._connection() as connection:  # noqa: SIM117
            async with connection.transaction():
                meeting = await self._lock_meeting(connection, meeting_id)
                if meeting is None:
                    raise MeetingNotFoundError("会议不存在")
                if meeting.status == final_status:
                    return meeting
                if meeting.status not in {MeetingStatus.RECORDING, MeetingStatus.FINALIZING}:
                    raise MeetingConflictError("会议无法封存")
                await connection.execute(
                    f"""
                    WITH ordered AS (
                        SELECT id,
                               row_number() OVER (ORDER BY start_ms, end_ms, id) - 1 AS new_order
                        FROM {self._schema}.transcript_segments
                        WHERE meeting_id = %s
                    )
                    UPDATE {self._schema}.transcript_segments AS segments
                    SET segment_order = ordered.new_order, updated_at = now()
                    FROM ordered
                    WHERE segments.id = ordered.id
                    """,
                    (meeting_id,),
                )
                now = _utc_now()
                await connection.execute(
                    f"""
                    UPDATE {self._schema}.meetings
                    SET status = %s, interruption_reason = %s, ended_at = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (final_status.value, reason, now, now, meeting_id),
                )
                await self._insert_event(
                    connection,
                    meeting_id,
                    (
                        "meeting_completed"
                        if final_status is MeetingStatus.COMPLETED
                        else "meeting_interrupted"
                    ),
                    {"reason": reason} if reason else {},
                )
                cursor = await connection.execute(
                    f"SELECT {_MEETING_COLUMNS} FROM {self._schema}.meetings WHERE id = %s",
                    (meeting_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RepositoryUnavailableError("封存后无法读取会议")
                return _meeting_from_row(row)

    async def get_transcript(self, meeting_id: UUID) -> TranscriptDocument:
        async with self._connection() as connection:
            meeting = await self._lock_meeting(connection, meeting_id, lock=False)
            if meeting is None:
                raise MeetingNotFoundError("会议不存在")
            segment_cursor = await connection.execute(
                f"""
                SELECT {_SEGMENT_COLUMNS}
                FROM {self._schema}.transcript_segments
                WHERE meeting_id = %s
                ORDER BY segment_order, start_ms, id
                """,
                (meeting_id,),
            )
            segment_rows = await segment_cursor.fetchall()
            segments = tuple(
                NormalizedSegment(
                    id=cast(UUID, row[0]),
                    order=int(row[1]),
                    source_epoch=int(row[2]),
                    speaker_key=str(row[3]),
                    start_ms=int(row[4]),
                    end_ms=int(row[5]),
                    text=str(row[6]),
                    translation=cast(str | None, row[7]),
                    detected_language=cast(str | None, row[8]),
                )
                for row in segment_rows
            )
            speaker_cursor = await connection.execute(
                f"""
                SELECT meeting_id, speaker_key, source_epoch, raw_speaker,
                       default_label, display_name, created_at, updated_at
                FROM {self._schema}.meeting_speakers
                WHERE meeting_id = %s
                ORDER BY source_epoch, speaker_key
                """,
                (meeting_id,),
            )
            speaker_rows = await speaker_cursor.fetchall()
            speakers = tuple(
                SpeakerRecord(
                    meeting_id=cast(UUID, row[0]),
                    speaker_key=str(row[1]),
                    source_epoch=int(row[2]),
                    raw_speaker=str(row[3]),
                    default_label=str(row[4]),
                    display_name=str(row[5]),
                    created_at=cast(datetime, row[6]),
                    updated_at=cast(datetime, row[7]),
                )
                for row in speaker_rows
            )
            return TranscriptDocument(
                meeting_id=meeting_id,
                transcript_revision=meeting.transcript_revision,
                content_revision=meeting.content_revision,
                segments=segments,
                speakers=speakers,
            )

    async def get_speakers(self, meeting_id: UUID) -> tuple[SpeakerRecord, ...]:
        """读取会议内匿名 speaker 映射，供详情 API 使用。"""
        async with self._connection() as connection:
            if await self._lock_meeting(connection, meeting_id, lock=False) is None:
                raise MeetingNotFoundError("会议不存在")
            cursor = await connection.execute(
                f"""
                SELECT meeting_id, speaker_key, source_epoch, raw_speaker,
                       default_label, display_name, created_at, updated_at
                FROM {self._schema}.meeting_speakers
                WHERE meeting_id = %s
                ORDER BY source_epoch, speaker_key
                """,
                (meeting_id,),
            )
            rows = await cursor.fetchall()
            return tuple(
                SpeakerRecord(
                    meeting_id=cast(UUID, row[0]),
                    speaker_key=str(row[1]),
                    source_epoch=int(row[2]),
                    raw_speaker=str(row[3]),
                    default_label=str(row[4]),
                    display_name=str(row[5]),
                    created_at=cast(datetime, row[6]),
                    updated_at=cast(datetime, row[7]),
                )
                for row in rows
            )

    async def get_latest_minutes(self, meeting_id: UUID) -> MinutesRecord | None:
        """读取最新纪要版本；会议不存在时抛出稳定 not-found 错误。"""
        async with self._connection() as connection:
            if await self._lock_meeting(connection, meeting_id, lock=False) is None:
                raise MeetingNotFoundError("会议不存在")
            cursor = await connection.execute(
                f"""
                SELECT {_MINUTES_COLUMNS}
                FROM {self._schema}.meeting_minutes
                WHERE meeting_id = %s
                ORDER BY version DESC
                LIMIT 1
                """,
                (meeting_id,),
            )
            row = await cursor.fetchone()
            return _minutes_from_row(row) if row is not None else None

    async def get_minutes(self, meeting_id: UUID, version: int) -> MinutesRecord | None:
        """读取指定纪要版本。"""
        if version < 1:
            raise ValueError("纪要版本必须为正整数")
        async with self._connection() as connection:
            if await self._lock_meeting(connection, meeting_id, lock=False) is None:
                raise MeetingNotFoundError("会议不存在")
            cursor = await connection.execute(
                f"""
                SELECT {_MINUTES_COLUMNS}
                FROM {self._schema}.meeting_minutes
                WHERE meeting_id = %s AND version = %s
                """,
                (meeting_id, version),
            )
            row = await cursor.fetchone()
            return _minutes_from_row(row) if row is not None else None

    async def rename_speaker(
        self, meeting_id: UUID, speaker_key: str, display_name: str
    ) -> MeetingRecord:
        speaker_key = speaker_key.strip()
        display_name = _validate_display_name(display_name)
        async with self._connection() as connection, connection.transaction():
            meeting = await self._lock_meeting(connection, meeting_id)
            if meeting is None:
                raise MeetingNotFoundError("会议不存在")
            cursor = await connection.execute(
                f"""
                SELECT display_name FROM {self._schema}.meeting_speakers
                WHERE meeting_id = %s AND speaker_key = %s
                FOR UPDATE
                """,
                (meeting_id, speaker_key),
            )
            row = await cursor.fetchone()
            if row is None:
                raise MeetingNotFoundError("说话人不存在")
            if str(row[0]) == display_name:
                return meeting
            now = _utc_now()
            await connection.execute(
                f"""
                UPDATE {self._schema}.meeting_speakers
                SET display_name = %s, updated_at = %s
                WHERE meeting_id = %s AND speaker_key = %s
                """,
                (display_name, now, meeting_id, speaker_key),
            )
            content_revision = meeting.content_revision + 1
            await connection.execute(
                f"""
                UPDATE {self._schema}.meetings
                SET content_revision = %s, updated_at = %s
                WHERE id = %s
                """,
                (content_revision, now, meeting_id),
            )
            await self._insert_event(
                connection,
                meeting_id,
                "speaker_updated",
                {"speaker_key": speaker_key, "content_revision": content_revision},
            )
            cursor = await connection.execute(
                f"SELECT {_MEETING_COLUMNS} FROM {self._schema}.meetings WHERE id = %s",
                (meeting_id,),
            )
            updated = await cursor.fetchone()
            if updated is None:
                raise RepositoryUnavailableError("说话人更新后无法读取会议")
            return _meeting_from_row(updated)

    async def create_minutes(
        self, meeting_id: UUID, *, idempotency_key: str | None
    ) -> MinutesRecord:
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
            if not idempotency_key or len(idempotency_key) > 200:
                raise ValueError("idempotency_key 无效")
        async with self._connection() as connection:  # noqa: SIM117
            async with connection.transaction():
                meeting = await self._lock_meeting(connection, meeting_id)
                if meeting is None:
                    raise MeetingNotFoundError("会议不存在")
                if meeting.status in {MeetingStatus.RECORDING, MeetingStatus.FINALIZING}:
                    raise MeetingConflictError("会议尚未封存")
                if idempotency_key is not None:
                    existing_cursor = await connection.execute(
                        f"""
                        SELECT {_MINUTES_COLUMNS}
                        FROM {self._schema}.meeting_minutes
                        WHERE meeting_id = %s AND idempotency_key = %s
                        """,
                        (meeting_id, idempotency_key),
                    )
                    existing = await existing_cursor.fetchone()
                    if existing is not None:
                        return _minutes_from_row(existing)
                version_cursor = await connection.execute(
                    f"""
                    SELECT COALESCE(MAX(version), 0) + 1
                    FROM {self._schema}.meeting_minutes
                    WHERE meeting_id = %s
                    """,
                    (meeting_id,),
                )
                version_row = await version_cursor.fetchone()
                version = int(version_row[0]) if version_row is not None else 1
                minutes_id = uuid4()
                now = _utc_now()
                await connection.execute(
                    f"""
                    INSERT INTO {self._schema}.meeting_minutes
                        (id, meeting_id, version, status, source_content_revision, model,
                         prompt_version, idempotency_key, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        minutes_id,
                        meeting_id,
                        version,
                        MinutesStatus.QUEUED.value,
                        meeting.content_revision,
                        self.settings.summary_model,
                        "v1",
                        idempotency_key,
                        now,
                        now,
                    ),
                )
                await self._insert_event(
                    connection,
                    meeting_id,
                    "minutes_queued",
                    {"version": version},
                )
                result = await connection.execute(
                    f"SELECT {_MINUTES_COLUMNS} FROM {self._schema}.meeting_minutes WHERE id = %s",
                    (minutes_id,),
                )
                row = await result.fetchone()
                if row is None:
                    raise RepositoryUnavailableError("纪要创建后无法读取")
                return _minutes_from_row(row)

    async def claim_minutes(self) -> MinutesJob | None:
        async with self._connection() as connection, connection.transaction():
            result = await connection.execute(
                f"""
                    WITH candidate AS (
                        SELECT id
                        FROM {self._schema}.meeting_minutes
                        WHERE status = %s OR (status = %s AND lease_until < now())
                        ORDER BY created_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE {self._schema}.meeting_minutes AS minutes
                    SET status = %s,
                        lease_until = now() + interval '15 minutes',
                        attempts = minutes.attempts + 1,
                        updated_at = now()
                    FROM candidate
                    WHERE minutes.id = candidate.id
                    RETURNING {_MINUTES_COLUMNS_QUALIFIED}
                    """,
                (
                    MinutesStatus.QUEUED.value,
                    MinutesStatus.GENERATING.value,
                    MinutesStatus.GENERATING.value,
                ),
            )
            row = await result.fetchone()
            if row is None:
                return None
            minutes = _minutes_from_row(row)
            meeting_cursor = await connection.execute(
                f"SELECT {_MEETING_COLUMNS} FROM {self._schema}.meetings WHERE id = %s",
                (minutes.meeting_id,),
            )
            meeting_row = await meeting_cursor.fetchone()
            if meeting_row is None:
                raise RepositoryUnavailableError("纪要对应的会议不存在")
            return MinutesJob(minutes=minutes, meeting=_meeting_from_row(meeting_row))

    async def complete_minutes(self, minutes_id: UUID, result: MinutesResult) -> None:
        validated_result, markdown = _coerce_minutes_result(result)
        async with self._connection() as connection, connection.transaction():
            now = _utc_now()
            cursor = await connection.execute(
                f"""
                    UPDATE {self._schema}.meeting_minutes
                    SET status = %s, content_json = %s, content_markdown = %s,
                        raw_output = NULL, error_code = NULL, error_message = NULL,
                        generated_at = %s, lease_until = NULL, updated_at = %s
                    WHERE id = %s AND status = %s
                    """,
                (
                    MinutesStatus.COMPLETED.value,
                    Jsonb(validated_result.model_dump(mode="json")),
                    markdown,
                    now,
                    now,
                    minutes_id,
                    MinutesStatus.GENERATING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise MeetingConflictError("纪要任务不在 generating 状态")

    async def fail_minutes(
        self,
        minutes_id: UUID,
        *,
        code: str,
        message: str,
        raw_output: str | None = None,
    ) -> None:
        code = code.strip()
        message = message.strip()
        if not code or len(code) > 128 or len(message) > 2_000:
            raise ValueError("纪要错误信息无效")
        async with self._connection() as connection, connection.transaction():
            cursor = await connection.execute(
                f"""
                    UPDATE {self._schema}.meeting_minutes
                    SET status = %s, error_code = %s, error_message = %s,
                        raw_output = %s, lease_until = NULL, updated_at = %s
                    WHERE id = %s AND status IN (%s, %s)
                    """,
                (
                    MinutesStatus.FAILED.value,
                    code,
                    message,
                    raw_output,
                    _utc_now(),
                    minutes_id,
                    MinutesStatus.GENERATING.value,
                    MinutesStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise MeetingNotFoundError("纪要任务不存在或已完成")

    async def delete_meeting(self, meeting_id: UUID) -> None:
        async with self._connection() as connection, connection.transaction():
            meeting = await self._lock_meeting(connection, meeting_id)
            if meeting is None:
                raise MeetingNotFoundError("会议不存在")
            if meeting.status in {MeetingStatus.RECORDING, MeetingStatus.FINALIZING}:
                raise MeetingConflictError("录制中的会议不能删除")
            cursor = await connection.execute(
                f"DELETE FROM {self._schema}.meetings WHERE id = %s", (meeting_id,)
            )
            if cursor.rowcount != 1:
                raise MeetingNotFoundError("会议不存在")

    async def requeue_generating(self) -> int:
        """录制优先时释放纪要 worker 租约，返回重新排队数量。"""
        async with self._connection() as connection, connection.transaction():
            cursor = await connection.execute(
                f"""
                UPDATE {self._schema}.meeting_minutes
                SET status = %s, lease_until = NULL, updated_at = %s
                WHERE status = %s
                """,
                (
                    MinutesStatus.QUEUED.value,
                    _utc_now(),
                    MinutesStatus.GENERATING.value,
                ),
            )
            return int(cursor.rowcount)

    async def recover_stale(self) -> int:
        """启动时将上次崩溃留下的 recording/finalizing 标为 interrupted。"""
        async with self._connection() as connection, connection.transaction():
            cursor = await connection.execute(
                f"""
                UPDATE {self._schema}.meetings
                SET status = %s, interruption_reason = %s, ended_at = %s, updated_at = %s
                WHERE status IN (%s, %s)
                RETURNING id
                """,
                (
                    MeetingStatus.INTERRUPTED.value,
                    "application_restart",
                    _utc_now(),
                    _utc_now(),
                    MeetingStatus.RECORDING.value,
                    MeetingStatus.FINALIZING.value,
                ),
            )
            rows = await cursor.fetchall()
            for row in rows:
                await self._insert_event(
                    connection,
                    cast(UUID, row[0]),
                    "meeting_interrupted",
                    {"reason": "application_restart"},
                )
            return len(rows)

    async def close(self) -> None:
        """关闭连接池；不会删除任何会议数据。"""
        if self._opened and self._owns_pool:
            await self._pool.close()
            self._opened = False

    async def _lock_meeting(
        self, connection: Any, meeting_id: UUID, *, lock: bool = True
    ) -> MeetingRecord | None:
        suffix = " FOR UPDATE" if lock else ""
        cursor = await connection.execute(
            f"SELECT {_MEETING_COLUMNS} FROM {self._schema}.meetings WHERE id = %s{suffix}",
            (meeting_id,),
        )
        row = await cursor.fetchone()
        return _meeting_from_row(row) if row is not None else None

    @staticmethod
    def _ensure_transition(current: MeetingStatus, target: MeetingStatus) -> None:
        allowed: dict[MeetingStatus, set[MeetingStatus]] = {
            MeetingStatus.RECORDING: {
                MeetingStatus.RECORDING,
                MeetingStatus.FINALIZING,
                MeetingStatus.INTERRUPTED,
                MeetingStatus.STORAGE_ERROR,
            },
            MeetingStatus.FINALIZING: {
                MeetingStatus.FINALIZING,
                MeetingStatus.COMPLETED,
                MeetingStatus.INTERRUPTED,
                MeetingStatus.STORAGE_ERROR,
            },
            MeetingStatus.COMPLETED: {
                MeetingStatus.COMPLETED,
                MeetingStatus.INTERRUPTED,
                MeetingStatus.STORAGE_ERROR,
            },
            MeetingStatus.INTERRUPTED: {MeetingStatus.INTERRUPTED},
            MeetingStatus.STORAGE_ERROR: {MeetingStatus.STORAGE_ERROR},
        }
        if target not in allowed[current]:
            raise MeetingConflictError(f"会议状态不能从 {current.value} 变为 {target.value}")

    async def _upsert_speaker(
        self, connection: Any, meeting_id: UUID, segment: NormalizedSegment
    ) -> None:
        raw_speaker = segment.speaker_key.rsplit(":", 1)[-1]
        default_label = f"说话人 {raw_speaker}"
        await connection.execute(
            f"""
            INSERT INTO {self._schema}.meeting_speakers
                (meeting_id, speaker_key, source_epoch, raw_speaker, default_label, display_name)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (meeting_id, speaker_key) DO UPDATE SET
                source_epoch = EXCLUDED.source_epoch,
                raw_speaker = EXCLUDED.raw_speaker,
                default_label = EXCLUDED.default_label,
                updated_at = now()
            """,
            (
                meeting_id,
                segment.speaker_key,
                segment.source_epoch,
                raw_speaker,
                default_label,
                default_label,
            ),
        )

    async def _insert_event(
        self, connection: Any, meeting_id: UUID, event_type: str, payload: dict[str, Any]
    ) -> None:
        await connection.execute(
            f"""
            INSERT INTO {self._schema}.meeting_events (id, meeting_id, event_type, payload)
            VALUES (%s, %s, %s, %s)
            """,
            (uuid4(), meeting_id, event_type, Jsonb(payload)),
        )


def _render_minutes_markdown(result: MinutesResult) -> str:
    """从已校验结构稳定渲染 Markdown，不记录原始模型 prompt。"""
    lines = ["# 会议纪要", "", "## 概要", "", result.overview]
    sections: tuple[tuple[str, Sequence[Any]], ...] = (
        ("主题", result.topics),
        ("决策", result.decisions),
        ("行动项", result.action_items),
        ("风险", result.risks),
        ("待确认问题", result.open_questions),
        ("重点", result.highlights),
    )
    for title, items in sections:
        if not items:
            continue
        lines.extend(["", f"## {title}", ""])
        for item in items:
            if hasattr(item, "title"):
                lines.append(f"- **{item.title}**：{item.summary}")
            elif hasattr(item, "task"):
                owner = f"（负责人：{item.owner}）" if item.owner else ""
                due = f"（截止：{item.due_date}）" if item.due_date else ""
                lines.append(f"- {item.task}{owner}{due}")
            else:
                lines.append(f"- {item.content}")
    return "\n".join(lines) + "\n"


def _coerce_minutes_result(result: Any) -> tuple[MinutesResult, str]:
    """兼容 summary workstream 的 artifact 包装，同时保持严格结果校验。"""
    if isinstance(result, Mapping):
        content = result.get("content_json")
        markdown = result.get("content_markdown")
    else:
        content = getattr(result, "content_json", None)
        markdown = getattr(result, "content_markdown", None)
    if content is not None:
        validated = MinutesResult.model_validate(content)
        rendered = str(markdown) if markdown is not None else _render_minutes_markdown(validated)
        return validated, rendered
    validated = MinutesResult.model_validate(result)
    return validated, _render_minutes_markdown(validated)
