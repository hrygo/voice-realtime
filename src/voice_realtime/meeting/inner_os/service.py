from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import UUID, uuid4

from voice_realtime.meeting.inner_os.cache import BoundedTTLCache
from voice_realtime.meeting.inner_os.context import build_context_snapshot
from voice_realtime.meeting.inner_os.contracts import InnerOSAnswer
from voice_realtime.meeting.inner_os.model_client import InnerOSModel, InnerOSModelError
from voice_realtime.meeting.models import (
    MeetingRecord,
    MeetingStatus,
    TranscriptDocument,
)


class InnerOSReadRepository(Protocol):
    async def get_meeting(self, meeting_id: UUID) -> MeetingRecord | None: ...

    async def get_transcript(self, meeting_id: UUID) -> TranscriptDocument: ...


class InnerOSServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InnerOSQueryService:
    """单连接查询状态机的后端核心；结果只交给发起连接。"""

    def __init__(
        self,
        repository: InnerOSReadRepository,
        model_client: InnerOSModel,
        *,
        cache_ttl_secs: int = 1800,
        cache_max_entries: int = 128,
        cache_max_bytes: int = 4 * 1024 * 1024,
        max_context_chars: int = 48_000,
        recent_context_chars: int = 16_000,
    ) -> None:
        self.repository = repository
        self.model_client = model_client
        self._max_context_chars = max_context_chars
        self._recent_context_chars = recent_context_chars
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._completed = BoundedTTLCache[UUID, dict[str, Any]](
            ttl_secs=cache_ttl_secs,
            max_entries=cache_max_entries,
            max_bytes=cache_max_bytes,
        )
        self._cancel_reasons: dict[UUID, str] = {}
        self._task_meetings: dict[UUID, UUID] = {}

    async def start_query(
        self,
        *,
        meeting_id: UUID,
        query_id: UUID | None = None,
        question: str,
        intent: str,
        focus_segment_ids: tuple[UUID, ...],
        ephemeral_context: dict[str, str] | None = None,
        emit: Callable[[str, UUID, dict[str, Any]], Awaitable[None]],
    ) -> UUID:
        query_id = query_id or uuid4()
        await emit("inner_os_query_accepted", query_id, {"status": "accepted"})
        task = asyncio.create_task(
            self._run(
                query_id,
                meeting_id,
                question,
                intent,
                focus_segment_ids,
                ephemeral_context,
                emit,
            ),
            name=f"inner-os-{query_id}",
        )
        self._tasks[query_id] = task
        self._task_meetings[query_id] = meeting_id
        task.add_done_callback(lambda _: self._tasks.pop(query_id, None))
        task.add_done_callback(lambda _: self._task_meetings.pop(query_id, None))
        return query_id

    async def cancel(
        self,
        query_id: UUID,
        *,
        reason: str = "user_cancelled",
        timeout_secs: float = 2.0,
    ) -> bool:
        task = self._tasks.get(query_id)
        if task is None:
            return False
        self._cancel_reasons[query_id] = reason
        task.cancel()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=timeout_secs)
        return True

    async def cancel_meeting(
        self,
        meeting_id: UUID,
        *,
        reason: str = "meeting_finalizing",
        timeout_secs: float = 2.0,
    ) -> None:
        query_ids = tuple(
            query_id
            for query_id, task_meeting_id in self._task_meetings.items()
            if task_meeting_id == meeting_id
        )
        await asyncio.gather(
            *(
                self.cancel(query_id, reason=reason, timeout_secs=timeout_secs)
                for query_id in query_ids
            ),
            return_exceptions=True,
        )

    async def cancel_all(
        self, *, reason: str = "connection_closed", timeout_secs: float = 2.0
    ) -> None:
        query_ids = tuple(self._tasks)
        for query_id in query_ids:
            await self.cancel(query_id, reason=reason, timeout_secs=timeout_secs)

    async def _run(
        self,
        query_id: UUID,
        meeting_id: UUID,
        question: str,
        intent: str,
        focus_segment_ids: tuple[UUID, ...],
        ephemeral_context: dict[str, str] | None,
        emit: Callable[[str, UUID, dict[str, Any]], Awaitable[None]],
    ) -> None:
        try:
            meeting = await self.repository.get_meeting(meeting_id)
            if meeting is None or meeting.status != MeetingStatus.RECORDING:
                raise InnerOSServiceError(
                    "inner_os_not_active", "当前会议不在录音状态"
                )
            document = await self.repository.get_transcript(meeting_id)
            snapshot = build_context_snapshot(
                document,
                question=question,
                focus_segment_ids=focus_segment_ids,
                max_chars=self._max_context_chars,
                recent_chars=self._recent_context_chars,
            )
            await emit("inner_os_answer_started", query_id, {"status": "started"})
            if not snapshot.evidence:
                answer = _empty_evidence_answer(intent)
            else:
                answer = await self.model_client.generate(
                    snapshot=snapshot,
                    question=question,
                    intent=intent,
                    ephemeral_context=ephemeral_context,
                )
            self._store_completed(
                query_id,
                self._make_exchange(
                    query_id,
                    meeting_id,
                    question,
                    answer,
                    snapshot.transcript_revision,
                    snapshot.content_revision,
                    ephemeral_context,
                ),
            )
            await emit(
                "inner_os_answer_completed", query_id, answer.model_dump(mode="json")
            )
        except TimeoutError:
            await emit(
                "inner_os_answer_failed",
                query_id,
                {"error": {"code": "inner_os_busy", "message": "本地模型当前繁忙，请稍后重试"}},
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await emit(
                    "inner_os_answer_cancelled",
                    query_id,
                    {"reason": self._cancel_reasons.pop(query_id, "user_cancelled")},
                )
        except InnerOSServiceError as exc:
            await emit(
                "inner_os_answer_failed",
                query_id,
                {"error": {"code": exc.code, "message": exc.message}},
            )
        except InnerOSModelError as exc:
            await emit(
                "inner_os_answer_failed",
                query_id,
                {"error": {"code": exc.code, "message": exc.message}},
            )
        except Exception as exc:
            del exc
            await emit(
                "inner_os_answer_failed",
                query_id,
                {
                    "error": {
                        "code": "inner_os_invalid_answer",
                        "message": "模型返回无法校验的答案",
                    }
                },
            )

    def take_completed(self, exchange_id: UUID) -> dict[str, Any] | None:
        """移交一次已完成结果给显式保存 API；未保存结果只存在进程内。"""
        return self._completed.pop(exchange_id)

    def peek_completed(self, exchange_id: UUID) -> dict[str, Any] | None:
        return self._completed.get(exchange_id)

    def _store_completed(self, query_id: UUID, exchange: dict[str, Any]) -> None:
        if not self._completed.put(query_id, exchange):
            raise InnerOSServiceError(
                "inner_os_output_limit", "答案超过了未保存结果的大小限制"
            )

    def _make_exchange(
        self,
        query_id: UUID,
        meeting_id: UUID,
        question: str,
        answer: InnerOSAnswer,
        transcript_revision: int,
        content_revision: int,
        ephemeral_context: dict[str, str] | None,
    ) -> dict[str, Any]:
        return {
            "id": query_id,
            "meeting_id": meeting_id,
            "question": question,
            "intent": answer.intent,
            "answer": answer,
            "source_transcript_revision": transcript_revision,
            "source_content_revision": content_revision,
            "used_ephemeral_context": bool(ephemeral_context),
            "model": self.model_client.model,
            "reasoning": "off",
            "prompt_version": self.model_client.prompt_version,
        }

    async def close(self) -> None:
        await self.cancel_all(reason="connection_closed")
        await self.model_client.close()


def _empty_evidence_answer(intent: str) -> InnerOSAnswer:
    return InnerOSAnswer.model_validate(
        {
            "intent": intent,
            "evidence": [],
            "facts": [],
            "judgements": [],
            "draft": None,
            "limitations": [
                {
                    "code": "insufficient_evidence",
                    "message": "当前会议还没有可引用的已确认转录。",
                }
            ],
        }
    )
