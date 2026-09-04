"""LM Studio 原生调用、流式解析与纪要生成网关 (MeetingSummaryClient)。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

import httpx

from sona.inference.scheduler import LocalInferenceScheduler, WorkloadKind
from sona.lm_studio import (
    DEFAULT_LM_STUDIO_API_KEY,
    LMStudioClient,
    LMStudioOutputLimitError,
    LMStudioProtocolError,
    LMStudioResponseError,
    NativeChatRequest,
    lm_studio_auth_headers,
    lm_studio_root_url,
)
from sona.meeting.summary.errors import (
    SummaryError,
    SummaryOutputLimitError,
    SummaryTimeoutError,
    SummaryUnavailableError,
    SummaryValidationError,
)
from sona.meeting.summary.evidence_anchor import (
    _format_model_transcript,
    _segment_references,
    format_transcript,
)
from sona.meeting.summary.prompt_builder import (
    map_instructions,
    reduce_instructions,
    repair_instructions,
    title_instructions,
)
from sona.meeting.summary.schema_validator import (
    MinutesContent,
    _parse_model_output,
)
from sona.network import local_async_client

logger = logging.getLogger(__name__)


class SummaryClientProtocol(Protocol):
    async def generate(
        self,
        document: Any,
        speakers: Any,
        *,
        repair: bool = False,
    ) -> Any: ...

    async def generate_title(
        self,
        document: Any,
        speakers: Any = (),
    ) -> str: ...


class MeetingSummaryRepository(Protocol):
    async def claim_minutes(self, *, max_attempts: int | None = None) -> Any: ...

    async def get_transcript(self, meeting_id: UUID) -> Any: ...

    async def complete_minutes(self, minutes_id: UUID, result: Any) -> Any: ...

    async def fail_minutes(
        self,
        minutes_id: UUID,
        *,
        code: str,
        message: str,
        raw_output: str | None = None,
    ) -> None: ...


class MeetingSummaryClient:
    """调用 LM Studio 原生聊天端点并解析 SSE 文本。"""

    def __init__(
        self,
        settings: Any | None = None,
        *,
        model: str | None = None,
        base_url: str | None = None,
        reasoning: str | None = None,
        temperature: float | None = None,
        timeout_secs: float | None = None,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        scheduler: LocalInferenceScheduler | None = None,
    ) -> None:
        self.model = model or str(getattr(settings, "summary_model", "local/kat-coder-2.5"))
        self.reasoning = reasoning or str(getattr(settings, "summary_reasoning", "off"))
        self.temperature = (
            float(temperature)
            if temperature is not None
            else float(getattr(settings, "summary_temperature", 0.2))
        )
        resolved_base = base_url or getattr(settings, "summary_base_url", None)
        if not resolved_base:
            resolved_base = getattr(settings, "lm_studio_base_url", "http://127.0.0.1:1234")
        self.base_url = lm_studio_root_url(str(resolved_base))
        resolved_api_key = (
            api_key
            if api_key is not None
            else getattr(settings, "llm_api_key", DEFAULT_LM_STUDIO_API_KEY)
        )
        self._api_key = str(resolved_api_key).strip()
        auth_headers = lm_studio_auth_headers(self._api_key)
        self.timeout_secs = (
            float(timeout_secs)
            if timeout_secs is not None
            else float(getattr(settings, "summary_timeout_secs", 60.0) or 60.0)
        )
        self.request_timeout_secs = float(
            getattr(settings, "summary_request_timeout_secs", 180.0) or 180.0
        )
        self.max_output_chars = int(
            getattr(settings, "summary_max_output_chars", 65_536) or 65_536
        )
        self.map_max_output_tokens = int(
            getattr(settings, "summary_map_max_output_tokens", 2_048) or 2_048
        )
        self.reduce_max_output_tokens = int(
            getattr(settings, "summary_reduce_max_output_tokens", 10_240) or 10_240
        )
        self.title_max_output_tokens = int(
            getattr(settings, "summary_title_max_output_tokens", 128) or 128
        )
        self._http = http_client or local_async_client(
            base_url=self.base_url,
            headers=auth_headers,
            timeout=httpx.Timeout(
                connect=5.0,
                read=self.timeout_secs,
                write=10.0,
                pool=5.0,
            ),
        )
        self._lm_studio = LMStudioClient(
            base_url=self.base_url, api_key=self._api_key, http_client=self._http
        )
        self.scheduler = scheduler
        self._closed = False
        self.call_stats: list[dict[str, Any]] = []

    def _build_payload(
        self,
        instructions: str,
        transcript: str,
        *,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "system_prompt": instructions,
            "input": transcript,
            "reasoning": self.reasoning,
            "temperature": self.temperature,
            "stream": True,
            "store": False,
        }
        if max_output_tokens is not None and max_output_tokens > 0:
            payload["max_output_tokens"] = max_output_tokens
        return payload

    async def _stream_text(
        self,
        payload: dict[str, Any],
        *,
        stage: str = "summary",
    ) -> str:
        if self.scheduler is None:
            return await self._stream_text_admitted(payload, stage=stage)
        async with self.scheduler.lease(workload=WorkloadKind.SUMMARY):
            return await self._stream_text_admitted(payload, stage=stage)

    async def _stream_text_admitted(
        self,
        payload: dict[str, Any],
        *,
        stage: str,
    ) -> str:
        # Keep legacy tests/integrators that replace ``_http`` compatible while
        # routing the request lifecycle through the shared LM Studio client.
        self._lm_studio = LMStudioClient(
            base_url=self.base_url, api_key=self._api_key, http_client=self._http
        )
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.request_timeout_secs):
                completion = await self._lm_studio.complete_chat(
                    NativeChatRequest.from_payload(payload),
                    max_output_chars=self.max_output_chars,
                    require_chat_end=False,
                )
                self._record_stats(completion.stats, stage, started)
        except SummaryError:
            raise
        except LMStudioOutputLimitError as exc:
            raise SummaryOutputLimitError(
                "AI 纪要输出超过安全上限，已停止退化生成"
            ) from exc
        except LMStudioProtocolError as exc:
            if exc.code == "invalid_delta":
                raise SummaryUnavailableError("LM Studio 纪要 delta 不是文本") from exc
            if exc.code == "invalid_event":
                raise SummaryUnavailableError("LM Studio SSE 事件格式无效") from exc
            raise SummaryUnavailableError(f"LM Studio 请求失败: {exc}") from exc
        except LMStudioResponseError as exc:
            raise SummaryUnavailableError(f"LM Studio 请求失败: {exc}") from exc
        except TimeoutError as exc:
            raise SummaryTimeoutError("AI 纪要生成超过单次调用时限，已停止") from exc
        except httpx.TimeoutException as exc:
            raise SummaryTimeoutError("AI 纪要流式连接超时，请检查 LLM 服务负载") from exc
        except (httpx.HTTPError, OSError) as exc:
            msg = "AI 纪要服务暂不可用，请检查 LLM (LM Studio) 是否正常运行"
            raise SummaryUnavailableError(msg) from exc
        text = completion.text.strip()
        if not text:
            raise SummaryUnavailableError("LM Studio 未返回纪要内容")
        return text

    def _record_stats(self, raw: Any, stage: str, started: float) -> None:
        if not isinstance(raw, Mapping):
            return
        allowed = (
            "input_tokens",
            "total_output_tokens",
            "reasoning_output_tokens",
            "tokens_per_second",
            "time_to_first_token_seconds",
        )
        stats: dict[str, Any] = {
            "stage": stage,
            "model": self.model,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        for key in allowed:
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                stats[key] = value
        self.call_stats.append(stats)
        logger.info("会议纪要模型调用完成", extra={"summary_call_stats": stats})

    def reset_call_stats(self) -> None:
        self.call_stats.clear()

    def _output_token_limit_reached(
        self,
        stage: str,
        max_output_tokens: int,
        stats_start: int,
    ) -> bool:
        """判断最近一次同阶段调用是否耗尽模型输出预算。"""

        for stats in reversed(self.call_stats[stats_start:]):
            if stats.get("stage") != stage:
                continue
            total = stats.get("total_output_tokens")
            return (
                isinstance(total, (int, float))
                and not isinstance(total, bool)
                and total >= max_output_tokens - 1
            )
        return False

    def _parse_bounded_output(
        self,
        raw: str,
        references: Mapping[str, UUID],
        *,
        stage: str,
        max_output_tokens: int,
        stats_start: int,
        for_map: bool = False,
    ) -> MinutesContent:
        try:
            return _parse_model_output(raw, references, for_map=for_map)
        except SummaryValidationError as exc:
            if self._output_token_limit_reached(stage, max_output_tokens, stats_start):
                raise SummaryOutputLimitError(
                    f"AI 纪要 {stage} 输出达到 {max_output_tokens} token 上限，JSON 未完整闭合"
                ) from exc
            raise

    async def generate(
        self,
        document: Any,
        speakers: Any,
        *,
        repair: bool = False,
    ) -> MinutesContent:
        transcript, references = _format_model_transcript(document, speakers)
        if not transcript:
            raise SummaryValidationError("会议没有可生成纪要的已确认转录")
        instructions = map_instructions(repair=repair)
        stats_start = len(self.call_stats)
        raw = await self._stream_text(
            self._build_payload(
                instructions,
                transcript,
                max_output_tokens=self.map_max_output_tokens,
            ),
            stage="map",
        )
        return self._parse_bounded_output(
            raw,
            references,
            stage="map",
            max_output_tokens=self.map_max_output_tokens,
            stats_start=stats_start,
            for_map=True,
        )

    async def generate_title(
        self,
        document: Any,
        speakers: Any = (),
        *,
        max_output_tokens: int | None = None,
    ) -> str:
        """根据会议转录提炼精准简明的会议标题。"""
        transcript = format_transcript(document, speakers)
        if not transcript:
            raise SummaryValidationError("会议没有可提炼标题的转录内容")
        # 提炼标题优先使用前部核心转录材料，避免超长会议转录产生不必要的 token 消耗
        if len(transcript) > 8000:
            transcript = transcript[:8000] + "\n...(后文转录略)"
        instructions = title_instructions()
        payload = self._build_payload(
            instructions,
            transcript,
            max_output_tokens=max_output_tokens or self.title_max_output_tokens,
        )
        raw = await self._stream_text(payload, stage="title")
        prefixes = (
            "会议标题：",
            "会议主题：",
            "会议纪要：",
            "标题：",
            "主题：",
            "Title:",
            "Topic:",
        )
        cleaned = raw.strip().strip("`'\"“”#* ")
        for prefix in prefixes:
            if cleaned.startswith(prefix):
                cleaned = cleaned.removeprefix(prefix).strip()
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        candidate = lines[0] if lines else ""
        candidate = candidate.strip("`'\"“”#* ")
        for prefix in prefixes:
            if candidate.startswith(prefix):
                candidate = candidate.removeprefix(prefix).strip()
        candidate = re.sub(r"^《(.*?)》(.*)$", r"\1\2", candidate).strip()
        candidate = re.sub(r"^“(.*?)”(.*)$", r"\1\2", candidate).strip()
        candidate = re.sub(r'^"(.*?)"(.*)$', r"\1\2", candidate).strip()
        if not candidate:
            candidate = "AI 会议纪要"
        return candidate[:64]

    async def reduce(
        self,
        results: Sequence[MinutesContent],
        document: Any,
        speakers: Any,
    ) -> MinutesContent:
        """对 map 结果做一次保守归并；输出仍由同一 schema 校验。"""

        # 归并输入只包含已验证的 JSON，避免再次把整场转录送入模型。
        merged = json.dumps([item.model_dump(mode="json") for item in results], ensure_ascii=False)
        instructions = reduce_instructions()
        references = _segment_references(document)
        try:
            stats_start = len(self.call_stats)
            raw = await self._stream_text(
                self._build_payload(
                    instructions,
                    merged,
                    max_output_tokens=self.reduce_max_output_tokens,
                ),
                stage="reduce",
            )
            return self._parse_bounded_output(
                raw,
                references,
                stage="reduce",
                max_output_tokens=self.reduce_max_output_tokens,
                stats_start=stats_start,
            )
        except SummaryValidationError as exc:
            raw_output = getattr(exc, "raw_output", None)
            if not raw_output:
                raise
            return await self.repair_output(
                raw_output,
                document,
                speakers,
                max_output_tokens=self.reduce_max_output_tokens,
                for_map=False,
            )

    async def repair_output(
        self,
        raw_output: str,
        document: Any,
        speakers: Any = None,
        *,
        max_output_tokens: int | None = None,
        for_map: bool = False,
    ) -> MinutesContent:
        """仅修复失败 JSON，不重新发送会议转录。"""

        del speakers
        references = _segment_references(document)
        allowed_refs = ",".join(references)
        instructions = repair_instructions(allowed_refs, for_map=for_map)
        resolved_max_output_tokens = max_output_tokens or self.map_max_output_tokens
        stats_start = len(self.call_stats)
        raw = await self._stream_text(
            self._build_payload(
                instructions,
                raw_output[: self.max_output_chars],
                max_output_tokens=resolved_max_output_tokens,
            ),
            stage="repair",
        )
        return self._parse_bounded_output(
            raw,
            references,
            stage="repair",
            max_output_tokens=resolved_max_output_tokens,
            stats_start=stats_start,
            for_map=for_map,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._http.aclose()
