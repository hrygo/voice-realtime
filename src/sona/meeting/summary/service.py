"""会议 AI 纪要任务生命周期、并发控制与数据库持久化服务 (MeetingSummaryService)。"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID

from sona.inference.scheduler import WorkloadKind
from sona.meeting.minutes_rendering import render_minutes_markdown
from sona.meeting.summary.chunker import _merge_results, split_document
from sona.meeting.summary.errors import (
    InvalidEvidenceError,
    SummaryError,
    SummaryTimeoutError,
    SummaryUnavailableError,
    SummaryValidationError,
)
from sona.meeting.summary.evidence_anchor import _attr, validate_evidence
from sona.meeting.summary.model_gateway import (
    MeetingSummaryRepository,
    SummaryClientProtocol,
)
from sona.meeting.summary.prompt_builder import SUMMARY_PROMPT_VERSION
from sona.meeting.summary.schema_validator import (
    MinutesContent,
    SummaryArtifact,
    parse_summary_output,
)

logger = logging.getLogger(__name__)

EventPublisher = Callable[[str, UUID, object], Awaitable[None]]


def _is_default_title(title: str) -> bool:
    """判断会议标题是否为自动生成的默认占位标题。"""
    normalized = (title or "").strip()
    return not normalized or normalized.startswith("会议-") or normalized.startswith("会议纪要")


def _job_id(job: Any) -> UUID:
    raw = _attr(job, "id") or _attr(_attr(job, "minutes"), "id")
    if raw is None:
        raise SummaryError("纪要任务缺少 ID")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


def _job_meeting_id(job: Any) -> UUID:
    raw = (
        _attr(job, "meeting_id")
        or _attr(_attr(job, "minutes"), "meeting_id")
        or _attr(_attr(job, "meeting"), "id")
    )
    if raw is None:
        raise SummaryError("纪要任务缺少会议 ID")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


class MeetingSummaryService:
    """有界纪要 worker，保证一条任务只被 claim 一次并持久化结果。"""

    def __init__(
        self,
        repository: MeetingSummaryRepository,
        client: SummaryClientProtocol,
        settings: Any,
        *,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.repository = repository
        self.client = client
        self.settings = settings
        self.event_publisher = event_publisher
        self._stop_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._active = False
        self.scheduler = getattr(client, "scheduler", None)

    async def start(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._worker(), name="meeting-summary-worker")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._worker_task
        self._worker_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        close = getattr(self.client, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result

    async def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("会议纪要任务轮询暂时不可用", exc_info=True)
                processed = False
            if not processed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=0.5)

    async def requeue_for_recording(self) -> None:
        """暂停新后台模型调用；已入场调用保持非抢占。"""

        if self.scheduler is not None:
            self.scheduler.pause_background()
        if (
            self._active
            and self.scheduler is not None
            and self.scheduler.active_workload == WorkloadKind.SUMMARY
        ):
            return
        if self._active:
            task = self._worker_task
            self._worker_task = None
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        for name in ("requeue_generating", "requeue_active_minutes", "requeue_active"):
            method = getattr(self.repository, name, None)
            if method is None:
                continue
            result = method()
            if inspect.isawaitable(result):
                await result
            return

    async def resume_after_recording(self) -> None:
        """会议结束或中断后恢复纪要 worker。"""
        if self.scheduler is not None:
            self.scheduler.resume_background()
        await self.start()

    async def run_once(self) -> bool:
        if self.scheduler is not None and self.scheduler.background_paused:
            return False
        max_attempts = int(
            getattr(self.settings, "summary_max_attempts", 0) or 0
        )
        job = await self.repository.claim_minutes(
            max_attempts=max_attempts if max_attempts > 0 else None
        )
        if job is None:
            return False
        self._active = True
        minutes_id = _job_id(job)
        meeting_id: UUID | None = None
        minutes = _attr(job, "minutes", job)
        version = int(_attr(minutes, "version", 1) or 1)
        try:
            meeting_id = _job_meeting_id(job)
            reset_stats = getattr(self.client, "reset_call_stats", None)
            if reset_stats is not None:
                reset_stats()
            await self._emit(
                meeting_id,
                minutes_id,
                version,
                status="generating",
            )
            document = await self.repository.get_transcript(meeting_id)
            speakers = _attr(document, "speakers", ()) or _attr(
                _attr(job, "meeting"), "speakers", ()
            ) or ()
            job_timeout = float(
                getattr(self.settings, "summary_job_timeout_secs", 600.0) or 600.0
            )
            try:
                async with asyncio.timeout(job_timeout):
                    results = await self._generate(document, speakers)
            except TimeoutError as exc:
                raise SummaryTimeoutError("AI 纪要任务超过总时限，已停止") from exc
            for result in results if isinstance(results, tuple) else (results,):
                validate_evidence(result, document)
            content = results[-1] if isinstance(results, tuple) else results
            artifact = SummaryArtifact(
                content_json=content,
                content_markdown=render_minutes_markdown(content),
                model=str(
                    _attr(_attr(job, "minutes"), "model")
                    or _attr(job, "model")
                    or getattr(self.settings, "summary_model", "")
                ),
                prompt_version=SUMMARY_PROMPT_VERSION,
            )
            completed_minutes = await self.repository.complete_minutes(minutes_id, artifact)
            # 若会议原标题为默认时间戳占位符，且 AI 生成了具体标题，自动更新会议标题
            extracted_title = str(_attr(content, "title", "") or "").strip()
            meeting = _attr(job, "meeting", None)
            meeting_title = str(_attr(meeting, "title", "")).strip()
            if (
                extracted_title
                and _is_default_title(meeting_title)
                and hasattr(self.repository, "update_title")
            ):
                try:
                    updated_meeting = await self.repository.update_title(
                        meeting_id, extracted_title
                    )
                except Exception:
                    logger.warning(
                        "AI 纪要标题更新失败",
                        exc_info=True,
                        extra={"meeting_id": str(meeting_id)},
                    )
                else:
                    updated_title = str(
                        _attr(updated_meeting, "title", extracted_title) or extracted_title
                    )
                    await self._emit_title(meeting_id, updated_title)
            await self._emit(
                meeting_id,
                minutes_id,
                version,
                status="completed",
                minutes=completed_minutes,
                generation_stats=self._generation_stats(),
            )
        except InvalidEvidenceError as exc:
            await self._fail(minutes_id, exc.code, str(exc))
            await self._emit_failure(meeting_id, minutes_id, version, exc)
        except SummaryValidationError as exc:
            await self._fail(
                minutes_id,
                exc.code,
                str(exc),
                raw_output=getattr(exc, "raw_output", None),
            )
            await self._emit_failure(meeting_id, minutes_id, version, exc)
        except SummaryUnavailableError as exc:
            await self._fail(minutes_id, exc.code, str(exc))
            await self._emit_failure(meeting_id, minutes_id, version, exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("meeting summary task failed", extra={"minutes_id": str(minutes_id)})
            await self._fail(minutes_id, "internal_error", "纪要生成失败")
            if meeting_id is not None:
                await self._emit(
                    meeting_id,
                    minutes_id,
                    version,
                    status="failed",
                    error_code="internal_error",
                    error_message="纪要生成失败",
                )
        finally:
            self._active = False
        return True

    async def _emit_failure(
        self,
        meeting_id: UUID | None,
        minutes_id: UUID,
        version: int,
        exc: SummaryError,
    ) -> None:
        if meeting_id is None:
            return
        await self._emit(
            meeting_id,
            minutes_id,
            version,
            status="failed",
            error_code=exc.code,
            error_message=str(exc),
            generation_stats=self._generation_stats(),
        )

    def _generation_stats(self) -> list[dict[str, Any]] | None:
        stats = getattr(self.client, "call_stats", None)
        if not isinstance(stats, list) or not stats:
            return None
        return [dict(item) for item in stats if isinstance(item, Mapping)] or None

    async def _emit_title(self, meeting_id: UUID, title: str) -> None:
        publisher = self.event_publisher
        if publisher is None:
            return
        try:
            await publisher("meeting_title_updated", meeting_id, {"title": title})
        except Exception:
            logger.warning("会议标题事件广播失败", exc_info=True)

    async def _emit(
        self,
        meeting_id: UUID,
        minutes_id: UUID,
        version: int,
        *,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
        minutes: Any | None = None,
        generation_stats: list[dict[str, Any]] | None = None,
    ) -> None:
        publisher = self.event_publisher
        if publisher is None:
            return
        try:
            await publisher(
                "minutes_state_changed",
                meeting_id,
                {
                    "minutes_id": str(minutes_id),
                    "version": version,
                    "status": status,
                    "error_code": error_code,
                    "error_message": error_message,
                    "minutes": minutes,
                    "generation_stats": generation_stats,
                },
            )
        except Exception:
            logger.warning("会议纪要事件广播失败", exc_info=True)

    async def _fail(
        self,
        minutes_id: UUID,
        code: str,
        message: str,
        raw_output: str | None = None,
    ) -> None:
        try:
            await self.repository.fail_minutes(
                minutes_id,
                code=code,
                message=message,
                raw_output=raw_output,
            )
        except Exception:
            # 失败状态写不进去时不能用日志记录完整转录或模型输出。
            logger.exception(
                "meeting summary failure could not be persisted",
                extra={"minutes_id": str(minutes_id)},
            )

    async def _generate(
        self, document: Any, speakers: Any
    ) -> MinutesContent | tuple[MinutesContent, ...]:
        chunks = split_document(document, self.settings)
        if len(chunks) == 1:
            return await self._generate_with_repair(chunks[0], speakers)
        mapped: tuple[MinutesContent, ...] = tuple(
            [await self._generate_with_repair(chunk, speakers) for chunk in chunks]
        )
        reduce_method = getattr(self.client, "reduce", None)
        if reduce_method is not None:
            reduced = reduce_method(mapped, document, speakers)
            if inspect.isawaitable(reduced):
                reduced = await reduced
            return parse_summary_output(reduced)
        return _merge_results(mapped)

    async def _generate_with_repair(self, document: Any, speakers: Any) -> MinutesContent:
        try:
            result = await self._call_generate(document, speakers, repair=False)
            return parse_summary_output(result)
        except SummaryValidationError as exc:
            repair_output = getattr(self.client, "repair_output", None)
            raw_output = getattr(exc, "raw_output", None)
            if repair_output is not None and raw_output:
                parameters: Mapping[str, inspect.Parameter] = {}
                with contextlib.suppress(TypeError, ValueError):
                    parameters = inspect.signature(repair_output).parameters
                if "for_map" in parameters:
                    repaired = repair_output(
                        raw_output,
                        document,
                        speakers,
                        for_map=True,
                    )
                else:
                    # 兼容仍使用旧 repair_output(raw, document, speakers) 签名的 client。
                    repaired = repair_output(raw_output, document, speakers)
                if inspect.isawaitable(repaired):
                    repaired = await repaired
                return parse_summary_output(repaired)
            # 只允许一次格式修复。若 client 是旧的两参数 fake，调用适配器
            # 会自动省略 repair 关键字，但不会无限重试。
            repaired = await self._call_generate(document, speakers, repair=True)
            return parse_summary_output(repaired)

    async def _call_generate(self, document: Any, speakers: Any, *, repair: bool) -> Any:
        method = self.client.generate
        parameters: Mapping[str, inspect.Parameter] = {}
        with contextlib.suppress(TypeError, ValueError):
            parameters = inspect.signature(method).parameters
        if "repair" in parameters:
            return await method(document, speakers, repair=repair)
        return await method(document, speakers)
