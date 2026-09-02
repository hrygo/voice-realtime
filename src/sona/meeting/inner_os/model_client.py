"""Inner OS model policy built on the shared LM Studio protocol client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

import httpx
from pydantic import ValidationError

from sona.inference.scheduler import LocalInferenceScheduler, WorkloadKind
from sona.lm_studio import (
    LMStudioClient,
    LMStudioOutputLimitError,
    LMStudioProtocolError,
    LMStudioResponseError,
    NativeChatRequest,
)
from sona.meeting.inner_os.context import InnerOSContextSnapshot
from sona.meeting.inner_os.contracts import InnerOSAnswer

_PROMPT_VERSION = "inner-os-v1"
_SYSTEM_PROMPT = (
    "仅依据证据回答，严格只输出一个 JSON 对象，不要 Markdown 代码围栏。"
    "JSON 必须严格包含 intent、evidence、facts、judgements、draft、limitations。"
    "facts 每项必须含 text 和 evidence_segment_ids；judgements 每项必须含 text、"
    "basis_segment_ids、uncertainty、uncertainty_reason；uncertainty 只能是 low、medium、high，"
    "uncertainty_reason 必须是非空字符串；draft 无法提供时为 null；"
    "draft 有内容时必须是 {\"text\":\"...\"} 对象；"
    "limitations 每项含 code 和 message。事实引用只能使用证据别名（如 S0001）；"
    "证据不足时使用空 facts、draft 为 null，并加入 code=insufficient_evidence 的 limitation。"
)
_REPAIR_PROMPT = (
    "把输入修复成严格符合约定结构的单个 JSON 对象。不得增加新事实或证据，"
    "不得输出 Markdown、解释、推理过程或证据别名之外的引用。"
    "根对象必须包含 intent、evidence、facts、judgements、draft、limitations；"
    "facts 的 evidence_segment_ids 和 judgements 的 basis_segment_ids 只能引用 Sxxxx；"
    "uncertainty 只能是 low、medium、high，uncertainty_reason 必须是非空字符串；"
    "draft 只能是 null 或 {\"text\":\"非空文本\"}。"
)


class NativeChatCompleter(Protocol):
    async def complete_chat(
        self,
        request: NativeChatRequest,
        *,
        max_output_chars: int | None = None,
        on_text_delta: Any = None,
    ) -> Any: ...

    async def aclose(self) -> None: ...


class InnerOSModel(Protocol):
    model: str
    prompt_version: str

    async def generate(
        self,
        *,
        snapshot: InnerOSContextSnapshot,
        question: str,
        intent: str,
        ephemeral_context: Mapping[str, str] | None = None,
    ) -> InnerOSAnswer: ...

    async def close(self) -> None: ...


class InnerOSModelError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InnerOSModelClient:
    """Owns Inner OS prompts, structured answer validation and one repair attempt."""

    def __init__(
        self,
        client: NativeChatCompleter | LMStudioClient,
        scheduler: LocalInferenceScheduler,
        *,
        model: str,
        max_output_chars: int = 65_536,
        max_output_tokens: int | None = None,
        fact_timeout_secs: float = 15.0,
        analysis_timeout_secs: float = 35.0,
        acquire_timeout_secs: float = 2.0,
    ) -> None:
        self._client = client
        self._scheduler = scheduler
        self.model = model
        self.prompt_version = _PROMPT_VERSION
        self._max_output_chars = max_output_chars
        self._max_output_tokens = max_output_tokens
        self._fact_timeout_secs = fact_timeout_secs
        self._analysis_timeout_secs = analysis_timeout_secs
        self._acquire_timeout_secs = acquire_timeout_secs

    async def generate(
        self,
        *,
        snapshot: InnerOSContextSnapshot,
        question: str,
        intent: str,
        ephemeral_context: Mapping[str, str] | None = None,
    ) -> InnerOSAnswer:
        request = NativeChatRequest(
            model=self.model,
            system_prompt=_SYSTEM_PROMPT,
            input=_build_input(snapshot, intent, question, ephemeral_context),
            reasoning="on" if intent in {"analysis", "mixed"} else "off",
            stream=True,
            store=False,
            max_output_tokens=self._max_output_tokens,
        )
        try:
            async with self._scheduler.lease(
                workload=WorkloadKind.INNER_OS,
                acquire_timeout_secs=self._acquire_timeout_secs,
            ):
                timeout_secs = (
                    self._analysis_timeout_secs
                    if intent in {"analysis", "mixed"}
                    else self._fact_timeout_secs
                )
                raw_text = await self._complete(request, timeout_secs=timeout_secs)
                try:
                    return _parse_answer(raw_text, snapshot, expected_intent=intent)
                except (ValueError, TypeError, json.JSONDecodeError, ValidationError):
                    repair = NativeChatRequest(
                        model=self.model,
                        system_prompt=_REPAIR_PROMPT,
                        input=raw_text,
                        reasoning="off",
                        stream=True,
                        store=False,
                        max_output_tokens=self._max_output_tokens,
                    )
                    repaired_text = await self._complete(repair, timeout_secs=timeout_secs)
                    try:
                        return _parse_answer(
                            repaired_text, snapshot, expected_intent=intent
                        )
                    except (ValueError, TypeError, json.JSONDecodeError, ValidationError) as exc:
                        raise InnerOSModelError(
                            "inner_os_invalid_answer", "模型返回无法校验的答案"
                        ) from exc
        except InnerOSModelError:
            raise
        except LMStudioOutputLimitError as exc:
            raise InnerOSModelError(
                "inner_os_output_limit", "模型答案超过安全输出上限"
            ) from exc
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise InnerOSModelError("inner_os_timeout", "模型响应超时") from exc
        except (LMStudioProtocolError, LMStudioResponseError, httpx.HTTPError, OSError) as exc:
            raise InnerOSModelError(
                "inner_os_model_unavailable", "本地模型服务暂不可用"
            ) from exc

    async def _complete(self, request: NativeChatRequest, *, timeout_secs: float) -> str:
        async with asyncio.timeout(timeout_secs):
            result = await self._client.complete_chat(
                request, max_output_chars=self._max_output_chars
            )
        return str(result.text).strip()

    async def close(self) -> None:
        await self._client.aclose()


def _build_input(
    snapshot: InnerOSContextSnapshot,
    intent: str,
    question: str,
    ephemeral_context: Mapping[str, str] | None,
) -> str:
    context = ""
    if ephemeral_context:
        values = "\n".join(
            f"{key}: {value}" for key, value in ephemeral_context.items() if value
        )
        if values:
            context = "\n临时背景（仅本次请求使用，不属于会议事实）：\n" + values
    evidence = "\n".join(
        (
            f"[{item.alias}][{_format_ms(item.start_ms)}-{_format_ms(item.end_ms)}]"
            f"[{item.speaker_name}] {item.text}"
        )
        for item in snapshot.evidence
    )
    return f"intent={intent}\n问题：{question}{context}\n证据：\n{evidence}"


def _parse_answer(
    raw_text: str,
    snapshot: InnerOSContextSnapshot,
    *,
    expected_intent: str,
) -> InnerOSAnswer:
    normalized = _strip_json_fence(raw_text)
    raw = json.loads(normalized)
    if not isinstance(raw, dict):
        raise ValueError("answer must be an object")
    alias_map = {item.alias: item.segment_id for item in snapshot.evidence}
    facts = raw.get("facts", [])
    judgements = raw.get("judgements", [])
    if not isinstance(facts, list) or not isinstance(judgements, list):
        raise ValueError("facts and judgements must be arrays")
    for item in facts:
        if not isinstance(item, dict):
            raise ValueError("fact must be an object")
        item["evidence_segment_ids"] = _resolve_aliases(
            item.get("evidence_segment_ids"), alias_map
        )
    for item in judgements:
        if not isinstance(item, dict):
            raise ValueError("judgement must be an object")
        item["basis_segment_ids"] = _resolve_aliases(
            item.get("basis_segment_ids"), alias_map
        )
    draft = raw.get("draft")
    if isinstance(draft, str):
        normalized_draft = draft.strip()
        raw["draft"] = {"text": normalized_draft} if normalized_draft else None
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
    if answer.intent != expected_intent:
        raise ValueError("answer intent does not match request")
    return answer


def _resolve_aliases(values: Any, alias_map: Mapping[str, UUID]) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("evidence references must be an array")
    resolved: list[str] = []
    for value in values:
        if not isinstance(value, str) or value not in alias_map:
            raise ValueError("answer references unknown evidence alias")
        resolved.append(str(alias_map[value]))
    return resolved


def _strip_json_fence(value: str) -> str:
    normalized = value.strip()
    if not normalized.startswith("```"):
        return normalized
    first_newline = normalized.find("\n")
    if first_newline < 0:
        return normalized
    normalized = normalized[first_newline + 1 :]
    if normalized.rstrip().endswith("```"):
        normalized = normalized.rstrip()[:-3]
    return normalized.strip()


def _format_ms(value: int) -> str:
    total_seconds, milliseconds = divmod(value, 1_000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
