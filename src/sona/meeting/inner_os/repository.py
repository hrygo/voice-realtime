from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

_COLUMNS = """id, meeting_id, question, intent, answer_json,
    source_transcript_revision, source_content_revision,
    used_ephemeral_context, model, reasoning, prompt_version, created_at"""


class InnerOSExchangeRepository:
    """已保存问答的独立 PostgreSQL 读写边界。"""

    def __init__(self, meeting_repository: Any) -> None:
        self._repository = meeting_repository

    @property
    def _schema(self) -> str:
        return cast(str, self._repository._schema)

    @staticmethod
    def _cursor(created_at: datetime, exchange_id: UUID) -> str:
        raw = json.dumps(
            {"created_at": created_at.astimezone(UTC).isoformat(), "id": str(exchange_id)},
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(value: str) -> tuple[datetime, UUID]:
        padded = value + "=" * (-len(value) % 4)
        raw = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return datetime.fromisoformat(str(raw["created_at"])), UUID(str(raw["id"]))

    async def save(self, exchange: dict[str, Any]) -> dict[str, Any]:
        async with self._repository._connection() as connection:
            query = f"""INSERT INTO {self._schema}.meeting_inner_os_exchanges
                (id, meeting_id, question, intent, answer_json, source_transcript_revision,
                 source_content_revision, used_ephemeral_context, model, reasoning, prompt_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
                RETURNING id, meeting_id, question, intent, answer_json,
                    source_transcript_revision, source_content_revision,
                    used_ephemeral_context, model, reasoning, prompt_version, created_at"""
            from psycopg.types.json import Jsonb

            cursor = await connection.execute(
                query,
                (
                    exchange["id"],
                    exchange["meeting_id"],
                    exchange["question"],
                    exchange["intent"],
                    Jsonb(exchange["answer"].model_dump(mode="json")),
                    exchange["source_transcript_revision"],
                    exchange["source_content_revision"],
                    exchange["used_ephemeral_context"],
                    exchange["model"],
                    exchange["reasoning"],
                    exchange["prompt_version"],
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        return self._row(row)

    async def get(self, meeting_id: UUID, exchange_id: UUID) -> dict[str, Any] | None:
        async with self._repository._connection() as connection:
            cursor = await connection.execute(
                f"SELECT {_COLUMNS} FROM {self._schema}.meeting_inner_os_exchanges "
                "WHERE meeting_id=%s AND id=%s",
                (meeting_id, exchange_id),
            )
            row = await cursor.fetchone()
        return self._row(row) if row else None

    async def list(
        self, meeting_id: UUID, cursor_value: str | None, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: list[Any] = [meeting_id]
        clause = ""
        if cursor_value:
            created_at, exchange_id = self._decode_cursor(cursor_value)
            clause = " AND (created_at, id) < (%s, %s)"
            params.extend((created_at, exchange_id))
        params.append(limit + 1)
        async with self._repository._connection() as connection:
            cursor = await connection.execute(
                f"SELECT {_COLUMNS} FROM {self._schema}.meeting_inner_os_exchanges "
                f"WHERE meeting_id=%s{clause} "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                tuple(params),
            )
            rows = await cursor.fetchall()
        items = [self._row(row) for row in rows[:limit]]
        next_cursor = self._cursor(rows[limit][11], rows[limit][0]) if len(rows) > limit else None
        return items, next_cursor

    async def delete(self, meeting_id: UUID, exchange_id: UUID) -> None:
        async with self._repository._connection() as connection:
            await connection.execute(
                f"DELETE FROM {self._schema}.meeting_inner_os_exchanges "
                "WHERE meeting_id=%s AND id=%s",
                (meeting_id, exchange_id),
            )
            await connection.commit()

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        keys = (
            "id",
            "meeting_id",
            "question",
            "intent",
            "answer",
            "source_transcript_revision",
            "source_content_revision",
            "used_ephemeral_context",
            "model",
            "reasoning",
            "prompt_version",
            "created_at",
        )
        return dict(zip(keys, row, strict=True))
