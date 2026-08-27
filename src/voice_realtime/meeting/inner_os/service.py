from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

from voice_realtime.lm_studio import LMStudioClient, NativeChatRequest
from voice_realtime.meeting.inner_os.context import build_context_snapshot
from voice_realtime.meeting.inner_os.contracts import InnerOSAnswer
from voice_realtime.meeting.inner_os.workload import LocalLLMWorkloadGate


class InnerOSQueryService:
    """单连接查询状态机的后端核心；结果只交给发起连接。"""

    def __init__(
        self,
        repository: Any,
        client: LMStudioClient,
        gate: LocalLLMWorkloadGate,
        *,
        model: str = "qwen/qwen3.6-35b-a3b",
    ) -> None:
        self.repository = repository
        self.client = client
        self.gate = gate
        self.model = model
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    async def start_query(
        self,
        *,
        meeting_id: UUID,
        question: str,
        intent: str,
        focus_segment_ids: tuple[UUID, ...],
        emit: Callable[[str, UUID, dict[str, Any]], Awaitable[None]],
    ) -> UUID:
        query_id = uuid4()
        await emit("inner_os_query_accepted", query_id, {"status": "accepted"})
        task = asyncio.create_task(
            self._run(query_id, meeting_id, question, intent, focus_segment_ids, emit),
            name=f"inner-os-{query_id}",
        )
        self._tasks[query_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(query_id, None))
        return query_id

    async def cancel(self, query_id: UUID) -> bool:
        task = self._tasks.get(query_id)
        if task is None:
            return False
        task.cancel()
        return True

    async def cancel_all(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(
        self,
        query_id: UUID,
        meeting_id: UUID,
        question: str,
        intent: str,
        focus_segment_ids: tuple[UUID, ...],
        emit: Callable[[str, UUID, dict[str, Any]], Awaitable[None]],
    ) -> None:
        try:
            async with self.gate.slot("inner_os"):
                document = await self.repository.get_transcript(meeting_id)
                snapshot = build_context_snapshot(
                    document, question=question, focus_segment_ids=focus_segment_ids
                )
                await emit("inner_os_answer_started", query_id, {"status": "started"})
                evidence = "\n".join(f"{item.alias}: {item.text}" for item in snapshot.evidence)
                request = NativeChatRequest(
                    model=self.model,
                    system_prompt=(
                        "仅依据证据回答，输出 JSON；事实必须引用证据别名，"
                        "证据不足时说明限制。"
                    ),
                    input=f"intent={intent}\n问题：{question}\n证据：\n{evidence}",
                    reasoning="off",
                    stream=True,
                    store=False,
                )
                parts = [
                    event.content
                    async for event in self.client.stream_chat(request)
                    if event.type == "message.delta" and event.content
                ]
                raw = json.loads("".join(parts))
                alias_map = {item.alias: str(item.segment_id) for item in snapshot.evidence}
                for item in raw.get("facts", []):
                    item["evidence_segment_ids"] = [
                        alias_map.get(value, value)
                        for value in item.get("evidence_segment_ids", [])
                    ]
                for item in raw.get("judgements", []):
                    item["basis_segment_ids"] = [
                        alias_map.get(value, value) for value in item.get("basis_segment_ids", [])
                    ]
                raw["evidence"] = [
                    {
                        "segment_id": str(item.segment_id),
                        "start_ms": item.start_ms,
                        "end_ms": item.end_ms,
                        "speaker_key": item.speaker_key,
                        "speaker_name": item.speaker_key,
                        "text": item.text,
                        "content_hash": f"sha256:{item.content_hash}",
                    }
                    for item in snapshot.evidence
                ]
                answer = InnerOSAnswer.model_validate(raw)
                await emit("inner_os_answer_completed", query_id, answer.model_dump(mode="json"))
        except asyncio.CancelledError:
            await emit("inner_os_answer_cancelled", query_id, {"reason": "user_cancelled"})
        except Exception as exc:
            await emit("inner_os_answer_failed", query_id, {
                "error": {"code": "inner_os_invalid_answer", "message": type(exc).__name__}
            })
