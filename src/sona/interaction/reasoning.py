"""LM Studio 适配：原生 /api/v1/chat 端点与有状态对话链。

QA 验证结论（2026-08-17，本机实测）：
- LM Studio 的 OpenAI 兼容端点 **忽略** `reasoning` 参数：模型始终思考
  （`reasoning_content` 占满 token、`content` 为空、TTFT 被思考拉长）。
- reasoning 开关只在 **原生 `/api/v1/chat`** 端点生效：
  `reasoning:"off"` 时 `reasoning_output_tokens=0`、TTFT 0.261s（实测）。
- 原生端点以 `system_prompt` 表示系统角色、`input` 表示本轮用户消息，
  通过 `response_id` / `previous_response_id` 保留历史 user/assistant 角色；
  支持 `stream:true` 的 SSE（`message.delta` 事件逐字输出）。

本服务子类化 `OpenAILLMService`（复用适配器、上下文、指标、中断机制），
仅覆写 `get_chat_completions` 的传输层走原生端点。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import httpx
from openai import AsyncStream
from openai.types.chat import ChatCompletionChunk, ChatCompletionMessageParam
from pipecat.frames.frames import CancelFrame, EndFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.llm import OpenAILLMService

from sona.interaction.context_memory import (
    MEMORY_READY,
    SUMMARY_SYSTEM_PROMPT,
    CompactionWindow,
    ContextCompactionConfig,
    ConversationMemoryPacket,
    ConversationMemorySnapshot,
    build_compaction_window,
    build_memory_packet,
    empty_memory_snapshot,
    fit_compaction_window,
    normalize_completed_turns,
    parse_snapshot,
    serialize_memory_packet,
    should_compact,
)
from sona.lm_studio import (
    LM_STUDIO_NATIVE_CHAT_PATH,
    LMStudioClient,
    NativeChatRequest,
    lm_studio_auth_headers,
    lm_studio_root_url,
)
from sona.network import local_async_client

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "# 角色\n"
    "你是一个中文语音助手，通过语音与用户自然对话，像面对面聊天一样口语化，"
    "语句短、用词简单。\n"
    "\n"
    "# 输出规则\n"
    "- 回答简短精炼：通常一到两句，一般不超过三句；只有用户要求展开时才多说。\n"
    "- 用口语表达，避免书面连接词（如“此外”“综上所述”）和冗长从句。\n"
    "- 数字、日期、单位写成读出来的样子（把“95%”读作“百分之九十五”），方便语音合成。\n"
    "\n"
    "# 禁止\n"
    "- 不要使用 markdown、列表符号、括号或表情符号，它们会被逐字朗读出来。\n"
    "- 不要重复用户的话，不要复述问题。\n"
    "- 不要机械客套收尾（如“还有什么需要帮忙的吗”），不要过度道歉。\n"
    "- 一次只问一个问题。\n"
    "\n"
    "# 容错与多轮对话\n"
    "- 用户的话可能被语音识别转错：意思不通时先推测真实意图，结合上下文理解，不要抓住字面追问。\n"
    "- 用户短暂停顿或话未说完时，简短倾听回应，不要过早机械打断或二选一澄清。\n"
    "- 确实无法确定时，用一句话自然询问，避免反复重复相同的澄清选项。\n"
    "- 不知道或不方便回答时坦诚说明，不要编造。\n"
)

_NATIVE_CHAT_PATH = LM_STUDIO_NATIVE_CHAT_PATH
_NATIVE_MODELS_PATH = "/api/v1/models"
_RESPONSE_ID_RE = re.compile(r"^resp_[A-Za-z0-9_-]+$")
_RECOVERY_RECENT_TURN_PAIRS = 4
_NATIVE_STREAM_READ_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class NativeChatStats:
    """LM Studio 在 chat.end 中返回的可信用量与延迟统计。"""

    input_tokens: int
    total_output_tokens: int
    reasoning_output_tokens: int
    ttft_seconds: float


@dataclass(frozen=True, slots=True)
class NativeChatResult:
    """经过结构、内容、统计和响应 ID 校验的原生响应。"""

    content: str
    response_id: str | None
    stats: NativeChatStats


def _nonnegative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"LM Studio stats.{field} must be a non-negative integer")
    return value


def _parse_native_result(
    data: object, *, require_response_id: bool
) -> NativeChatResult:
    """严格解析原生最终响应，避免提交残缺或不可度量的上下文链。"""
    if not isinstance(data, dict):
        raise TypeError("LM Studio native result must be an object")

    output = data.get("output")
    if not isinstance(output, list):
        raise TypeError("LM Studio native result output must be a list")
    contents: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, str):
            raise TypeError("LM Studio message content must be a string")
        if content:
            contents.append(content)
    if not contents:
        raise ValueError("LM Studio native result contains no message content")

    stats = data.get("stats")
    if not isinstance(stats, dict):
        raise ValueError("LM Studio stats must be an object")
    input_tokens = _nonnegative_int(stats.get("input_tokens"), field="input_tokens")
    total_output_tokens = _nonnegative_int(
        stats.get("total_output_tokens"), field="total_output_tokens"
    )
    reasoning_output_tokens = _nonnegative_int(
        stats.get("reasoning_output_tokens"), field="reasoning_output_tokens"
    )
    raw_ttft = stats.get("time_to_first_token_seconds")
    if (
        isinstance(raw_ttft, bool)
        or not isinstance(raw_ttft, (int, float))
        or not math.isfinite(raw_ttft)
        or raw_ttft < 0
    ):
        raise ValueError(
            "LM Studio stats.time_to_first_token_seconds must be non-negative"
        )

    response_id = data.get("response_id")
    if response_id is None:
        if require_response_id:
            raise ValueError("LM Studio native result response_id is required")
    elif not isinstance(response_id, str) or not _RESPONSE_ID_RE.fullmatch(response_id):
        raise ValueError("LM Studio native result response_id is invalid")

    return NativeChatResult(
        content="".join(contents),
        response_id=response_id,
        stats=NativeChatStats(
            input_tokens=input_tokens,
            total_output_tokens=total_output_tokens,
            reasoning_output_tokens=reasoning_output_tokens,
            ttft_seconds=float(raw_ttft),
        ),
    )


def _text_content(message: ChatCompletionMessageParam) -> str | None:
    """提取文本消息内容；非文本（多模态）或空内容返回 None。"""
    content = message.get("content")
    if isinstance(content, str) and content:
        return content
    return None


def _conversation_input(
    messages: list[ChatCompletionMessageParam],
) -> tuple[str, str, int]:
    """从带角色历史中提取系统提示、本轮用户输入和用户轮次数。"""
    system_messages = [message for message in messages if message.get("role") == "system"]
    if len(system_messages) != 1:
        raise ValueError("LM context must contain exactly one text system message")
    system_prompt = _text_content(system_messages[0])
    if system_prompt is None:
        raise ValueError("LM context must contain exactly one text system message")
    if not messages or messages[-1].get("role") != "user":
        raise ValueError("LM context must end with a text user message")
    user_input = _text_content(messages[-1])
    if user_input is None:
        raise ValueError("LM context user message must contain text")
    user_turns = sum(
        1
        for message in messages
        if message.get("role") == "user" and _text_content(message) is not None
    )
    return system_prompt, user_input, user_turns


def _is_invalid_previous_response_id(exc: httpx.HTTPStatusError) -> bool:
    """仅识别 LM Studio 明确指向 previous_response_id 的 400/404。"""
    if exc.response.status_code not in {400, 404}:
        return False
    try:
        body = exc.response.json()
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    if not isinstance(error, dict):
        return False
    if error.get("param") == "previous_response_id":
        return True
    message = error.get("message")
    return isinstance(message, str) and "previous_response_id" in message


class LmStudioNativeLLMService(OpenAILLMService):
    """调用 LM Studio 原生 /api/v1/chat 端点，注入有效的 reasoning 开关。

    OpenAI 兼容端点忽略 reasoning 参数会导致模型始终思考（content 为空、
    token 耗尽），因此必须走原生端点。实测 `reasoning:"off"` 时
    reasoning_output_tokens=0，TTFT≈260ms，满足对话实时性预算。
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        reasoning: str = "off",
        temperature: float = 0.7,
        api_key: str = "lm-studio",
        compaction_config: ContextCompactionConfig | None = None,
        **kwargs: Any,
    ) -> None:
        normalized_api_key = api_key.strip()
        auth_headers = lm_studio_auth_headers(normalized_api_key)
        super().__init__(
            base_url=base_url,
            api_key=normalized_api_key,
            settings=self.Settings(
                model=model,
                temperature=temperature,
            ),
            **kwargs,
        )
        self._reasoning = reasoning
        self._model = model
        self._temperature = temperature
        self._compaction_config = compaction_config or ContextCompactionConfig(enabled=False)
        # 原生端点挂在 LM Studio 根路径下；兼容配置里带 "/v1" 的写法。
        root_url = lm_studio_root_url(base_url)
        self._http = local_async_client(
            base_url=root_url,
            headers=auth_headers,
            timeout=httpx.Timeout(
                connect=5.0,
                read=_NATIVE_STREAM_READ_TIMEOUT_SECONDS,
                write=10.0,
                pool=5.0,
            ),
        )
        self._lm_studio = LMStudioClient(
            base_url=root_url, api_key=normalized_api_key, http_client=self._http
        )
        self._native_client_closed = False
        self._previous_response_id: str | None = None
        self._system_prompt: str | None = None
        self._completed_user_turns = 0
        self._request_generation = 0
        self._last_chat_stats: NativeChatStats | None = None
        self._last_assistant_text: str | None = None
        self._model_context_length: int | None = None
        self._model_context_length_discovered = False
        self._memory_packet: ConversationMemoryPacket | None = None
        self._compaction_task: asyncio.Task[None] | None = None
        self._ttft_soft_hits = 0

    @property
    def last_chat_stats(self) -> NativeChatStats | None:
        return self._last_chat_stats

    @property
    def last_assistant_text(self) -> str | None:
        return self._last_assistant_text

    @property
    def memory_packet(self) -> ConversationMemoryPacket | None:
        return self._memory_packet

    @property
    def compaction_task(self) -> asyncio.Task[None] | None:
        return self._compaction_task

    def reset_conversation(self) -> None:
        """使在途请求失效并开启新的 LM Studio 对话链。"""
        self._request_generation += 1
        task = self._compaction_task
        self._compaction_task = None
        if task is not None and not task.done():
            task.cancel()
        self._previous_response_id = None
        self._system_prompt = None
        self._completed_user_turns = 0
        self._last_chat_stats = None
        self._last_assistant_text = None
        self._memory_packet = None
        self._ttft_soft_hits = 0

    async def _native_chat_once(
        self, payload: dict[str, Any], *, timeout_seconds: float
    ) -> NativeChatResult:
        """执行有界非流式原生请求，并严格校验完整响应。"""
        response = await self._http.post(
            _NATIVE_CHAT_PATH,
            json=payload,
            timeout=httpx.Timeout(
                connect=5.0,
                read=timeout_seconds,
                write=10.0,
                pool=5.0,
            ),
        )
        response.raise_for_status()
        return _parse_native_result(
            response.json(), require_response_id=payload.get("store") is True
        )

    async def _get_model_context_length(self) -> int | None:
        """尽力读取当前模型实例的上下文容量；失败结果也缓存。"""
        if self._model_context_length_discovered:
            return self._model_context_length

        discovered: int | None = None
        try:
            response = await self._http.get(
                _NATIVE_MODELS_PATH,
                timeout=httpx.Timeout(5.0),
            )
            response.raise_for_status()
            body = response.json()
            models = body.get("models") if isinstance(body, dict) else None
            if isinstance(models, list):
                model = next(
                    (
                        item
                        for item in models
                        if isinstance(item, dict) and item.get("key") == self._model
                    ),
                    None,
                )
                if isinstance(model, dict):
                    loaded_instances = model.get("loaded_instances")
                    if isinstance(loaded_instances, list):
                        ordered_instances = sorted(
                            (item for item in loaded_instances if isinstance(item, dict)),
                            key=lambda item: item.get("id") != self._model,
                        )
                        for instance in ordered_instances:
                            config = instance.get("config")
                            context_length = (
                                config.get("context_length")
                                if isinstance(config, dict)
                                else None
                            )
                            if (
                                isinstance(context_length, int)
                                and not isinstance(context_length, bool)
                                and context_length > 0
                            ):
                                discovered = context_length
                                break
                    if discovered is None:
                        maximum = model.get("max_context_length")
                        if (
                            isinstance(maximum, int)
                            and not isinstance(maximum, bool)
                            and maximum > 0
                        ):
                            discovered = maximum
        except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError):
            logger.debug("LM Studio 模型上下文容量当前不可用", exc_info=True)

        self._model_context_length = discovered
        self._model_context_length_discovered = True
        return discovered

    async def _summarize_window(
        self, window: CompactionWindow
    ) -> ConversationMemorySnapshot:
        """把冻结的旧历史压缩为严格、带来源范围的结构化快照。"""
        transcript = {
            "previous_snapshot": window.previous_snapshot.model_dump(),
            "turns_to_summarize": [
                turn.model_dump() for turn in window.turns_to_summarize
            ],
            "required_source_range": {
                "start": window.expected_snapshot_start,
                "end": window.expected_snapshot_end,
            },
            "required_json_schema": ConversationMemorySnapshot.model_json_schema(),
        }
        last_error: ValueError | None = None
        for attempt in range(2):
            request_data = dict(transcript)
            if attempt:
                request_data["validation_feedback"] = {
                    "category": "schema_validation_failed",
                    "instruction": "仅修正 JSON 结构、枚举、额外字段和来源范围。",
                }
            result = await self._native_chat_once(
                {
                    "model": self._model,
                    "system_prompt": SUMMARY_SYSTEM_PROMPT,
                    "input": json.dumps(
                        request_data,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "reasoning": "off",
                    "temperature": 0,
                    "max_output_tokens": self._compaction_config.summary_max_output_tokens,
                    "store": False,
                    "stream": False,
                },
                timeout_seconds=self._compaction_config.summary_timeout_seconds,
            )
            if result.stats.reasoning_output_tokens != 0:
                raise ValueError("LM Studio summary reasoning output must be zero")
            try:
                return parse_snapshot(
                    result.content,
                    expected_start=window.expected_snapshot_start,
                    expected_end=window.expected_snapshot_end,
                )
            except ValueError as exc:
                last_error = exc
        if last_error is None:  # pragma: no cover - 循环结构的静态兜底
            raise RuntimeError("LM Studio summary validation did not run")
        raise last_error

    async def _prewarm_chain(
        self,
        packet: ConversationMemoryPacket,
        system_prompt: str,
    ) -> NativeChatResult:
        """把历史记忆作为数据预热新链，但不混入下一条真实用户指令。"""
        result = await self._native_chat_once(
            {
                "model": self._model,
                "system_prompt": system_prompt,
                "input": serialize_memory_packet(packet) + "\n仅回复 MEMORY_READY",
                "reasoning": "off",
                "temperature": 0,
                "max_output_tokens": 16,
                "store": True,
                "stream": False,
            },
            timeout_seconds=self._compaction_config.summary_timeout_seconds,
        )
        if result.content.strip() != MEMORY_READY:
            raise ValueError("LM Studio prewarm acknowledgement is invalid")
        if result.stats.reasoning_output_tokens != 0:
            raise ValueError("LM Studio prewarm reasoning output must be zero")
        if result.response_id is None or not _RESPONSE_ID_RE.fullmatch(result.response_id):
            raise ValueError("LM Studio prewarm response_id is invalid")
        if result.stats.input_tokens > self._compaction_config.target_input_tokens:
            logger.info(
                "LM Studio 压缩预热超过目标水位（input_tokens=%s target=%s recent_turns=%s）",
                result.stats.input_tokens,
                self._compaction_config.target_input_tokens,
                len(packet.recent_turns),
            )
        return result

    async def _recover_invalid_chain(
        self,
        messages: list[ChatCompletionMessageParam],
        current_user: str,
        system_prompt: str,
        generation: int,
    ) -> str:
        """用已验证的完整历史预热替代链，绝不把当前指令当成历史。"""
        try:
            _, parsed_current_user, _ = _conversation_input(messages)
            if parsed_current_user != current_user:
                raise ValueError("current user input changed during recovery")
            turns = normalize_completed_turns(messages)
            if not turns:
                raise ValueError("no complete history is available for recovery")

            previous_snapshot = (
                self._memory_packet.snapshot
                if self._memory_packet is not None
                else empty_memory_snapshot()
            )
            window = build_compaction_window(
                turns,
                previous_snapshot,
                _RECOVERY_RECENT_TURN_PAIRS,
            )
            if window is None:
                recent_turns = turns[previous_snapshot.source_turn_end :]
                if not recent_turns:
                    raise ValueError("no recent complete history is available for recovery")
                packet = build_memory_packet(previous_snapshot, recent_turns)
            else:
                fitted_window = fit_compaction_window(
                    window,
                    max_recent_bytes=self._compaction_config.target_input_tokens * 4,
                )
                snapshot = await self._summarize_window(fitted_window)
                packet = build_memory_packet(snapshot, fitted_window.recent_turns)

            candidate = await self._prewarm_chain(packet, system_prompt)
            if generation != self._request_generation or self._native_client_closed:
                raise ValueError("recovery candidate became stale")
            if candidate.response_id is None:
                raise ValueError("recovery response_id is missing")
            self._memory_packet = packet
            return candidate.response_id
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            raise RuntimeError("上下文恢复失败") from exc

    async def _run_compaction(
        self,
        window: CompactionWindow,
        system_prompt: str,
        *,
        generation: int,
        user_turns: int,
        source_response_id: str | None = None,
    ) -> None:
        """在后台构造候选链，并仅在原会话边界未变化时原子提交。"""
        captured_response_id = source_response_id or self._previous_response_id
        if captured_response_id is None:
            return
        try:
            fitted_window = fit_compaction_window(
                window,
                max_recent_bytes=self._compaction_config.target_input_tokens * 4,
            )
            snapshot = await self._summarize_window(fitted_window)
            packet = build_memory_packet(snapshot, fitted_window.recent_turns)
            candidate = await self._prewarm_chain(packet, system_prompt)
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            logger.warning(
                "LM Studio 上下文压缩候选失败"
                "（reason=%s generation=%s summarize_turns=%s recent_turns=%s）",
                type(exc).__name__,
                generation,
                len(window.turns_to_summarize),
                len(window.recent_turns),
            )
            return

        still_current = (
            generation == self._request_generation
            and user_turns == self._completed_user_turns
            and not self._native_client_closed
            and self._previous_response_id == captured_response_id
        )
        if not still_current:
            logger.info(
                "丢弃已过期的 LM Studio 上下文压缩候选（generation=%s）",
                generation,
            )
            return

        self._previous_response_id = candidate.response_id
        self._system_prompt = system_prompt
        self._memory_packet = packet

    async def _evaluate_capacity_and_compact(
        self,
        window: CompactionWindow,
        system_prompt: str,
        *,
        generation: int,
        user_turns: int,
        source_response_id: str,
        stats: NativeChatStats,
        unsummarized_messages: int,
    ) -> None:
        model_context_length = await self._get_model_context_length()
        decision = should_compact(
            self._compaction_config,
            input_tokens=stats.input_tokens,
            ttft_seconds=stats.ttft_seconds,
            ttft_soft_hits=self._ttft_soft_hits,
            unsummarized_messages=unsummarized_messages,
            model_context_length=model_context_length,
        )
        if decision.triggered:
            await self._run_compaction(
                window,
                system_prompt,
                generation=generation,
                user_turns=user_turns,
                source_response_id=source_response_id,
            )

    @staticmethod
    def _observe_compaction_task(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("LM Studio 上下文压缩后台任务异常")

    def _schedule_compaction(
        self,
        messages: list[ChatCompletionMessageParam],
        assistant_text: str,
        system_prompt: str,
        generation: int,
    ) -> None:
        """在有效用户响应提交后按水位调度至多一个后台候选。"""
        stats = self._last_chat_stats
        if not self._compaction_config.enabled or stats is None or self._native_client_closed:
            return
        if stats.ttft_seconds >= self._compaction_config.ttft_soft_seconds:
            self._ttft_soft_hits += 1
        else:
            self._ttft_soft_hits = 0
        if self._compaction_task is not None and not self._compaction_task.done():
            return

        try:
            turns = normalize_completed_turns(messages, assistant_text=assistant_text)
            previous_snapshot = (
                self._memory_packet.snapshot
                if self._memory_packet is not None
                else empty_memory_snapshot()
            )
            window = build_compaction_window(
                turns,
                previous_snapshot,
                self._compaction_config.recent_turn_pairs,
            )
        except ValueError:
            logger.warning("LM Studio 上下文压缩跳过非法角色历史")
            return
        if window is None or self._previous_response_id is None:
            return

        user_turns = len(turns) // 2
        unsummarized_messages = len(turns) - previous_snapshot.source_turn_end
        decision = should_compact(
            self._compaction_config,
            input_tokens=stats.input_tokens,
            ttft_seconds=stats.ttft_seconds,
            ttft_soft_hits=self._ttft_soft_hits,
            unsummarized_messages=unsummarized_messages,
            model_context_length=(
                self._model_context_length
                if self._model_context_length_discovered
                else None
            ),
        )
        source_response_id = self._previous_response_id
        if decision.triggered:
            coroutine = self._run_compaction(
                window,
                system_prompt,
                generation=generation,
                user_turns=user_turns,
                source_response_id=source_response_id,
            )
        elif not self._model_context_length_discovered:
            coroutine = self._evaluate_capacity_and_compact(
                window,
                system_prompt,
                generation=generation,
                user_turns=user_turns,
                source_response_id=source_response_id,
                stats=stats,
                unsummarized_messages=unsummarized_messages,
            )
        else:
            return
        task = asyncio.create_task(coroutine, name=f"lm-context-compact-{generation}")
        task.add_done_callback(self._observe_compaction_task)
        self._compaction_task = task

    async def _native_completions(
        self,
        payload: dict[str, Any],
        *,
        generation: int,
        system_prompt: str,
        user_turns: int,
        messages: list[ChatCompletionMessageParam],
        allow_chain_retry: bool = True,
    ) -> AsyncIterator[ChatCompletionChunk]:
        """对流式读取施加熔断；仅当前请求可原子废弃会话链。"""
        try:
            async for chunk in self._consume_native_completions(
                payload,
                generation=generation,
                system_prompt=system_prompt,
                user_turns=user_turns,
                messages=messages,
                allow_chain_retry=allow_chain_retry,
            ):
                yield chunk
        except httpx.ReadTimeout:
            if generation == self._request_generation:
                self.reset_conversation()
                logger.warning(
                    "LM Studio 流式读取超时，已废弃当前会话链（generation=%s）",
                    generation,
                )
            else:
                logger.info(
                    "LM Studio 旧流式请求读取超时，保留当前会话链（generation=%s）",
                    generation,
                )
            raise

    async def _consume_native_completions(
        self,
        payload: dict[str, Any],
        *,
        generation: int,
        system_prompt: str,
        user_turns: int,
        messages: list[ChatCompletionMessageParam],
        allow_chain_retry: bool = True,
    ) -> AsyncIterator[ChatCompletionChunk]:
        """SSE 消费原生端点，把 message.delta 转成 OpenAI 兼容 chunk。"""
        saw_content = False
        content_parts: list[str] = []
        final_result: NativeChatResult | None = None
        try:
            async for event in self._lm_studio.stream_chat(
                NativeChatRequest.from_payload(payload)
            ):
                if event.type == "error" or event.type.endswith(".error"):
                    raise RuntimeError("LM Studio stream reported an error")
                if event.type == "chat.end":
                    if event.result is None:
                        raise TypeError("LM Studio chat.end result must be an object")
                    if final_result is not None:
                        raise RuntimeError("LM Studio stream returned duplicate chat.end")
                    stream_result = dict(event.result)
                    stream_result["output"] = [
                        {"type": "message", "content": "".join(content_parts)}
                    ]
                    final_result = _parse_native_result(
                        stream_result, require_response_id=True
                    )
                    continue
                if event.type != "message.delta":
                    continue
                if event.content is None:
                    raise TypeError("LM Studio delta content must be a string")
                if not event.content:
                    continue
                saw_content = True
                content_parts.append(event.content)
                chunk = SimpleNamespace(
                    usage=None,
                    model=None,
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=event.content, tool_calls=None)
                        )
                    ],
                )
                # SimpleNamespace 与 ChatCompletionChunk 无类型重叠，先经 Any 中转
                yield cast(ChatCompletionChunk, cast(Any, chunk))
        except httpx.HTTPStatusError as exc:
            can_rebuild = (
                allow_chain_retry
                and "previous_response_id" in payload
                and generation == self._request_generation
                and _is_invalid_previous_response_id(exc)
            )
            if not can_rebuild:
                raise
            self._previous_response_id = None
            self._system_prompt = None
            self._completed_user_turns = 0
            self._last_chat_stats = None
            self._last_assistant_text = None
            retry_payload = dict(payload)
            retry_payload.pop("previous_response_id", None)
            current_user = retry_payload.get("input")
            if not isinstance(current_user, str) or not current_user:
                raise ValueError("LM Studio current user input is invalid") from exc
            seed_response_id = await self._recover_invalid_chain(
                messages,
                current_user,
                system_prompt,
                generation,
            )
            retry_payload.pop("system_prompt", None)
            retry_payload["previous_response_id"] = seed_response_id
            logger.warning(
                "LM Studio 上下文链失效，已从验证记忆恢复（generation=%s）",
                generation,
            )
            async for retry_chunk in self._consume_native_completions(
                retry_payload,
                generation=generation,
                system_prompt=system_prompt,
                user_turns=user_turns,
                messages=messages,
                allow_chain_retry=False,
            ):
                yield retry_chunk
            return
        if not saw_content:
            raise RuntimeError("LM Studio returned no message content")
        if final_result is None:
            raise RuntimeError("LM Studio stream ended without a valid chat.end")
        if generation == self._request_generation:
            logger.info(
                "LM Studio: 流式推理完成 (input_tokens=%d, output_tokens=%d, ttft=%.2fs)",
                final_result.stats.input_tokens,
                final_result.stats.total_output_tokens,
                final_result.stats.ttft_seconds,
            )
            self._previous_response_id = final_result.response_id
            self._system_prompt = system_prompt
            self._completed_user_turns = user_turns
            self._last_chat_stats = final_result.stats
            self._last_assistant_text = final_result.content
            self._schedule_compaction(
                messages,
                final_result.content,
                system_prompt,
                generation,
            )

    async def _cancel_compaction(self) -> None:
        task = self._compaction_task
        self._compaction_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("LM Studio 上下文压缩任务清理失败")

    async def close(self) -> None:
        """取消后台候选并关闭原生 HTTP 客户端；允许重复调用。"""
        await self._cancel_compaction()
        if self._native_client_closed:
            return
        await self._http.aclose()
        self._native_client_closed = True

    async def stop(self, frame: EndFrame) -> None:
        """停止处理器并释放原生 HTTP 连接池。"""
        try:
            await super().stop(frame)
        finally:
            await self.close()

    async def cancel(self, frame: CancelFrame) -> None:
        """取消处理器并释放原生 HTTP 连接池。"""
        try:
            await super().cancel(frame)
        finally:
            await self.close()

    async def cleanup(self) -> None:
        """在管道清理兜底中释放原生 HTTP 连接池。"""
        try:
            await super().cleanup()  # type: ignore[no-untyped-call]
        finally:
            await self.close()

    async def get_chat_completions(self, context: LLMContext) -> AsyncStream[ChatCompletionChunk]:
        """覆写传输层：OpenAI 格式消息 → 原生端点 input items → SSE 流。"""
        adapter = self.get_llm_adapter()
        params_from_context = adapter.get_llm_invocation_params(
            context,
            system_instruction=None,
            convert_developer_to_user=False,
        )
        messages = params_from_context["messages"]
        frozen_messages = [
            cast(ChatCompletionMessageParam, dict(message)) for message in messages
        ]
        system_prompt, user_input, user_turns = _conversation_input(messages)
        must_reset_chain = self._previous_response_id is not None and (
            self._system_prompt != system_prompt
            or user_turns <= self._completed_user_turns
        )
        if must_reset_chain:
            self.reset_conversation()
        self._request_generation += 1
        generation = self._request_generation
        if self._previous_response_id is None and user_turns > 1:
            seed_response_id = await self._recover_invalid_chain(
                frozen_messages,
                user_input,
                system_prompt,
                generation,
            )
            self._previous_response_id = seed_response_id
            self._system_prompt = system_prompt
            self._completed_user_turns = user_turns - 1
        payload: dict[str, Any] = {
            "model": self._model,
            "input": user_input,
            "reasoning": self._reasoning,
            "temperature": self._temperature,
            "store": True,
            "stream": True,
        }
        if self._previous_response_id is None:
            payload["system_prompt"] = system_prompt
        else:
            payload["previous_response_id"] = self._previous_response_id
        return cast(
            AsyncStream[ChatCompletionChunk],
            self._native_completions(
                payload,
                generation=generation,
                system_prompt=system_prompt,
                user_turns=user_turns,
                messages=frozen_messages,
            ),
        )
