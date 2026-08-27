from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import httpx

from voice_realtime.lm_studio import LMStudioClient, NativeChatRequest
from voice_realtime.meeting.inner_os.cache import BoundedTTLCache
from voice_realtime.meeting.inner_os.context import build_context_snapshot
from voice_realtime.meeting.inner_os.contracts import InnerOSAnswer
from voice_realtime.meeting.inner_os.workload import LocalLLMWorkloadGate
from voice_realtime.meeting.models import MeetingStatus


class InnerOSServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InnerOSQueryService:
    """单连接查询状态机的后端核心；结果只交给发起连接。"""

    def __init__(
        self,
        repository: Any,
        client: LMStudioClient,
        gate: LocalLLMWorkloadGate,
        *,
        model: str = "qwen/qwen3.6-35b-a3b",
        cache_ttl_secs: int = 1800,
        cache_max_entries: int = 128,
        cache_max_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.repository = repository
        self.client = client
        self.gate = gate
        self.model = model
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
            async with self.gate.slot("inner_os", acquire_timeout_secs=2.0):
                get_meeting = getattr(self.repository, "get_meeting", None)
                if get_meeting is not None:
                    meeting = await get_meeting(meeting_id)
                    meeting_status = getattr(meeting, "status", None)
                    meeting_status = getattr(meeting_status, "value", meeting_status)
                    if meeting is None or meeting_status != MeetingStatus.RECORDING.value:
                        raise InnerOSServiceError(
                            "inner_os_not_active", "当前会议不在录音状态"
                        )
                document = await self.repository.get_transcript(meeting_id)
                snapshot = build_context_snapshot(
                    document, question=question, focus_segment_ids=focus_segment_ids
                )
                await emit("inner_os_answer_started", query_id, {"status": "started"})
                if not snapshot.evidence:
                    answer = InnerOSAnswer.model_validate(
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
                    exchange = self._make_exchange(
                        query_id,
                        meeting_id,
                        question,
                        answer,
                        snapshot.transcript_revision,
                        snapshot.content_revision,
                        ephemeral_context,
                    )
                    self._store_completed(query_id, exchange)
                    await emit(
                        "inner_os_answer_completed", query_id, answer.model_dump(mode="json")
                    )
                    return
                evidence = "\n".join(
                    f"{item.alias}: {item.text}" for item in snapshot.evidence
                )
                request = NativeChatRequest(
                    model=self.model,
                    system_prompt=(
                        "仅依据证据回答，严格只输出一个 JSON 对象，不要 Markdown 代码围栏。"
                        "JSON 必须严格包含 intent、evidence、facts、judgements、draft、"
                        "limitations。"
                        "evidence 是证据数组；facts 每项必须含 text 和 evidence_segment_ids；"
                        "judgements 每项必须含 text、basis_segment_ids、uncertainty、"
                        "uncertainty_reason；draft 无法提供时为 null；"
                        "limitations 每项含 code 和 message。"
                        "事实引用只能使用证据别名（如 S0001）；证据不足时使用空 facts、"
                        "draft 为 null，并加入 code=insufficient_evidence 的 limitation。"
                    ),
                    input=self._build_input(
                        intent, question, evidence, ephemeral_context
                    ),
                    reasoning="off",
                    stream=True,
                    store=False,
                )
                parts: list[str] = []
                saw_end = False
                async for event in self.client.stream_chat(request):
                    if event.type == "message.delta" and event.content:
                        parts.append(event.content)
                    if event.type in {"chat.end", "chat.end.result"}:
                        saw_end = True
                if not saw_end:
                    raise InnerOSServiceError(
                        "inner_os_model_unavailable", "模型连接未正常结束"
                    )
                raw_text = "".join(parts).strip()
                if raw_text.startswith("```"):
                    raw_text = raw_text.split("\n", 1)[-1]
                    raw_text = raw_text.rsplit("```", 1)[0].strip()
                raw = json.loads(raw_text)
                if not isinstance(raw, dict):
                    raise ValueError("answer must be an object")
                alias_map = {
                    item.alias: str(item.segment_id) for item in snapshot.evidence
                }
                for item in raw.get("facts", []):
                    item["evidence_segment_ids"] = [
                        alias_map.get(value, value)
                        for value in item.get("evidence_segment_ids", [])
                    ]
                for item in raw.get("judgements", []):
                    item["basis_segment_ids"] = [
                        alias_map.get(value, value)
                        for value in item.get("basis_segment_ids", [])
                    ]
                raw["evidence"] = [
                    {
                        "segment_id": str(item.segment_id),
                        "start_ms": item.start_ms,
                        "end_ms": item.end_ms,
                        "speaker_key": item.speaker_key,
                        "speaker_name": item.speaker_name,
                        "text": item.text,
                        "content_hash": f"sha256:{item.content_hash}",
                    }
                    for item in snapshot.evidence
                ]
                answer = InnerOSAnswer.model_validate(raw)
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
        except httpx.TimeoutException:
            await emit(
                "inner_os_answer_failed",
                query_id,
                {"error": {"code": "inner_os_timeout", "message": "模型响应超时"}},
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
            "model": self.model,
            "reasoning": "off",
            "prompt_version": "inner-os-v1",
        }

    async def close(self) -> None:
        await self.cancel_all(reason="connection_closed")
        await self.client.aclose()

    @staticmethod
    def _build_input(
        intent: str,
        question: str,
        evidence: str,
        ephemeral_context: dict[str, str] | None,
    ) -> str:
        context = ""
        if ephemeral_context:
            values = "\n".join(
                f"{key}: {value}" for key, value in ephemeral_context.items() if value
            )
            if values:
                context = "\n临时背景（仅本次请求使用，不属于会议事实）：\n" + values
        return f"intent={intent}\n问题：{question}{context}\n证据：\n{evidence}"
