# LM Studio Context Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 LM Studio 原生有状态对话链上实现后台结构化摘要、预热新链和原子切换，使长语音会话保持角色/对象记忆并把实际模型输入控制在实时延迟预算内。

**Architecture:** Pipecat `LLMContext` 继续保存完整角色历史；`LmStudioNativeLLMService` 从真实 `chat.end.stats` 判断压缩水位，冻结已提交轮次并调用原生 `store:false` 摘要，再用结构化记忆包预热 `store:true` 新链。候选仅在 generation、turn 边界、确认文本、reasoning stats 和 response ID 全部有效时提交；当前用户输入始终作为独立的下一条原生 user turn。

**Tech Stack:** Python 3.12、Pydantic v2、asyncio、httpx、Pipecat 1.7、LM Studio `/api/v1/chat`、pytest/pytest-asyncio、mypy strict、ruff。

**Spec:** `docs/superpowers/specs/2026-08-21-lm-studio-context-compaction-design.md`

## Global Constraints

- 必须继续使用 LM Studio 原生 `POST /api/v1/chat`；正常对话和摘要均发送 `reasoning: "off"`。
- 摘要请求必须 `store:false`；正常对话和新链预热使用 `store:true`。
- Pipecat 完整历史不能被摘要替换、不能插入第二条 system 消息、不能把 memory packet 计为真实 user turn。
- 不持久化交互原文、摘要或记忆包；日志不得包含正文、完整 prompt 或完整 response ID。
- 不新增依赖、不升级模型、不启用远程服务、不修改数据库、会议助手或前端协议。
- 保留工作区已有声学/UI 暂存改动；每次提交使用精确文件列表，禁止纳入无关 diff。
- 默认软水位 6000 tokens、硬水位 10000、目标 2500、最近四组问答、40 条未压缩消息、TTFT 1.5 秒、摘要上限 1024 tokens、摘要超时 20 秒。
- 所有模型生成的记忆必须先经过 Pydantic `extra="forbid"`、来源 turn 校验和大小上限校验。

---

## File Map

- `src/voice_realtime/interaction/context_memory.py`：记忆 schema、轮次标准化、滚动压缩窗口、策略判断、摘要/预热 prompt 和 packet 序列化。
- `src/voice_realtime/interaction/reasoning.py`：原生响应解析、usage stats、非流式摘要/预热、后台任务、原子 response chain 切换和断链恢复。
- `src/voice_realtime/config.py`：用户可配置的压缩水位与跨字段约束。
- `src/voice_realtime/interaction/pipeline.py`：固定记忆协议 system prompt 与 `ContextCompactionConfig` 注入；不得启用 Pipecat 默认摘要。
- `src/voice_realtime/interaction/session.py`：clear/stop/cancel 生命周期调用压缩状态清理；合并现有 echo state 改动。
- `tests/test_context_memory.py`：纯 schema、窗口和策略测试。
- `tests/test_reasoning.py`：原生 payload、stats、摘要、预热、并发、恢复测试。
- `tests/test_config.py`、`tests/test_pipeline.py`：配置与装配契约。
- `tests/test_interaction_context.py`、`tests/test_interaction_session.py`：clear/stop 生命周期与 Pipecat 完整历史不变。
- `docs/decisions/0003-lm-studio-context-compaction.md`：架构决策。
- `AGENTS.md`、`docs/实时语音交互与字幕-方案与最佳实践.md`、`docs/架构图与流程图.md`：权威契约更新。

### Task 1: Structured Conversation Memory and Compaction Policy

**Files:**
- Create: `src/voice_realtime/interaction/context_memory.py`
- Create: `tests/test_context_memory.py`

**Interfaces:**
- Produces: `ConversationTurn`, `ConversationMemorySnapshot`, `ConversationMemoryPacket`, `CompactionWindow`, `ContextCompactionConfig`, `CompactionDecision`。
- Produces: `normalize_completed_turns(messages, assistant_text=None) -> list[ConversationTurn]`。
- Produces: `build_compaction_window(turns, previous_snapshot, recent_turn_pairs) -> CompactionWindow | None`。
- Produces: `should_compact(config, input_tokens, ttft_seconds, ttft_soft_hits, unsummarized_messages, model_context_length) -> CompactionDecision`。
- Produces: `empty_memory_snapshot() -> ConversationMemorySnapshot`、`parse_snapshot(raw, expected_start, expected_end) -> ConversationMemorySnapshot`、`build_memory_packet(snapshot, recent_turns) -> ConversationMemoryPacket`、`fit_memory_packet(snapshot, recent_turns, max_bytes) -> ConversationMemoryPacket`。
- Produces: `SUMMARY_SYSTEM_PROMPT`, `MEMORY_PROTOCOL`, `MEMORY_READY`。

- [ ] **Step 1: Add exact test helpers, then write failing schema and source-boundary tests**

```python
def valid_snapshot_payload(source_turn_start: int, source_turn_end: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_turn_start": source_turn_start,
        "source_turn_end": source_turn_end,
        "participants": [
            {"id": "local_user", "role": "user", "names": ["用户"]},
            {"id": "voice_assistant", "role": "assistant", "names": ["助手"]},
        ],
        "entities": [
            {
                "id": "project_voice",
                "type": "project",
                "name": "voice-realtime",
                "aliases": [],
                "facts": [
                    {
                        "value": "使用本地 LM Studio",
                        "status": "active",
                        "source_turn_ids": [source_turn_end],
                    }
                ],
            }
        ],
        "user_preferences": [],
        "goals_and_constraints": [],
        "decisions": [],
        "open_items": [],
        "conversation_summary": "用户正在设计上下文压缩。",
    }


def empty_snapshot() -> ConversationMemorySnapshot:
    return empty_memory_snapshot()


def alternating_turns(count: int) -> list[ConversationTurn]:
    return [
        ConversationTurn(
            turn_id=index,
            role="user" if index % 2 else "assistant",
            content=f"第{index}条消息",
        )
        for index in range(1, count + 1)
    ]


def test_snapshot_rejects_extra_fields_and_out_of_range_sources() -> None:
    payload = valid_snapshot_payload(source_turn_start=1, source_turn_end=4)
    payload["unexpected"] = "拒绝"
    with pytest.raises(ValidationError):
        ConversationMemorySnapshot.model_validate(payload)

    payload = valid_snapshot_payload(source_turn_start=1, source_turn_end=4)
    payload["entities"][0]["facts"][0]["source_turn_ids"] = [5]
    with pytest.raises(ValueError, match="source_turn_ids"):
        ConversationMemorySnapshot.model_validate(payload)


def test_packet_rejects_non_alternating_recent_turns() -> None:
    snapshot = ConversationMemorySnapshot.model_validate(
        valid_snapshot_payload(source_turn_start=1, source_turn_end=2)
    )
    recent = [
        ConversationTurn(turn_id=3, role="user", content="问题一"),
        ConversationTurn(turn_id=4, role="user", content="问题二"),
    ]
    with pytest.raises(ValueError, match="alternate"):
        build_memory_packet(snapshot, recent)


def test_fit_packet_drops_oldest_complete_pairs_but_keeps_latest_pair() -> None:
    snapshot = empty_memory_snapshot()
    recent = alternating_turns(8)
    packet = fit_memory_packet(snapshot, recent, max_bytes=500)
    assert len(packet.recent_turns) >= 2
    assert len(packet.recent_turns) % 2 == 0
    assert packet.recent_turns[-2:] == recent[-2:]
    assert packet.recent_turns[0].role == "user"
    assert packet.recent_turns[-1].role == "assistant"
```

- [ ] **Step 2: Run the new test module and verify RED**

Run: `uv run pytest tests/test_context_memory.py -q`

Expected: collection fails because `voice_realtime.interaction.context_memory` does not exist.

- [ ] **Step 3: Implement strict Pydantic models**

```python
Role = Literal["user", "assistant"]
EntityType = Literal[
    "person", "organization", "project", "file", "service", "place", "concept", "other"
]
MemoryStatus = Literal["active", "superseded", "uncertain"]


class StrictMemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConversationTurn(StrictMemoryModel):
    turn_id: int = Field(ge=1)
    role: Role
    content: str = Field(min_length=1, max_length=8000)


class MemoryFact(StrictMemoryModel):
    value: str = Field(min_length=1, max_length=1000)
    status: MemoryStatus
    source_turn_ids: list[int] = Field(min_length=1, max_length=16)


class MemoryEntity(StrictMemoryModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    type: EntityType
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=16)
    facts: list[MemoryFact] = Field(default_factory=list, max_length=32)


class SourcedMemoryItem(StrictMemoryModel):
    value: str = Field(min_length=1, max_length=1000)
    status: MemoryStatus = "active"
    source_turn_ids: list[int] = Field(min_length=1, max_length=16)


class MemoryParticipant(StrictMemoryModel):
    id: Literal["local_user", "voice_assistant"]
    role: Role
    names: list[str] = Field(default_factory=list, max_length=8)


class ConversationMemorySnapshot(StrictMemoryModel):
    schema_version: Literal[1] = 1
    source_turn_start: int = Field(ge=0)
    source_turn_end: int = Field(ge=0)
    participants: list[MemoryParticipant] = Field(min_length=2, max_length=2)
    entities: list[MemoryEntity] = Field(default_factory=list, max_length=64)
    user_preferences: list[SourcedMemoryItem] = Field(default_factory=list, max_length=64)
    goals_and_constraints: list[SourcedMemoryItem] = Field(default_factory=list, max_length=64)
    decisions: list[SourcedMemoryItem] = Field(default_factory=list, max_length=64)
    open_items: list[SourcedMemoryItem] = Field(default_factory=list, max_length=64)
    conversation_summary: str = Field(default="", max_length=4000)
```

Add a model validator that requires `(source_turn_start, source_turn_end) == (0, 0)` for an empty snapshot, otherwise `1 <= start <= end`, enforces participant-role mapping, and checks every source ID lies within `[start, end]`.

- [ ] **Step 4: Write failing turn/window/policy tests**

```python
def test_window_rolls_forward_from_previous_snapshot_and_keeps_four_pairs() -> None:
    turns = alternating_turns(12)
    previous = empty_snapshot()
    window = build_compaction_window(turns, previous, recent_turn_pairs=4)
    assert window is not None
    assert [turn.turn_id for turn in window.turns_to_summarize] == [1, 2, 3, 4]
    assert [turn.turn_id for turn in window.recent_turns] == list(range(5, 13))
    assert window.expected_snapshot_start == 1
    assert window.expected_snapshot_end == 4


def test_policy_uses_real_tokens_and_requires_two_ttft_hits() -> None:
    config = ContextCompactionConfig()
    assert should_compact(config, 6000, 0.4, 0, 4, 262144).triggered
    assert not should_compact(config, 2000, 1.6, 1, 4, 262144).triggered
    decision = should_compact(config, 2000, 1.6, 2, 4, 262144)
    assert decision.triggered
    assert decision.reason == "ttft"
```

- [ ] **Step 5: Implement turn selection and policy**

```python
@dataclass(frozen=True, slots=True)
class ContextCompactionConfig:
    enabled: bool = True
    soft_input_tokens: int = 6000
    hard_input_tokens: int = 10000
    target_input_tokens: int = 2500
    recent_turn_pairs: int = 4
    max_unsummarized_messages: int = 40
    ttft_soft_seconds: float = 1.5
    summary_max_output_tokens: int = 1024
    summary_timeout_seconds: float = 20.0
    capacity_ratio: float = 0.8


@dataclass(frozen=True, slots=True)
class CompactionDecision:
    triggered: bool
    reason: Literal["disabled", "none", "tokens", "hard", "ttft", "messages", "capacity"]


@dataclass(frozen=True, slots=True)
class CompactionWindow:
    previous_snapshot: ConversationMemorySnapshot
    turns_to_summarize: tuple[ConversationTurn, ...]
    recent_turns: tuple[ConversationTurn, ...]
    expected_snapshot_start: int
    expected_snapshot_end: int
```

`normalize_completed_turns` must ignore the sole system message, accept only text user/assistant messages, require alternation starting with user, and drop a trailing unmatched user unless `assistant_text` is supplied. `build_compaction_window` must summarize only complete pairs, preserve up to four most recent pairs, and roll from `previous_snapshot.source_turn_end + 1`.

`empty_memory_snapshot()` returns `(source_turn_start, source_turn_end) == (0, 0)`, the fixed user/assistant participants, empty memory lists and an empty conversation summary; recovery and tests use this instead of duplicating empty-state construction.

- [ ] **Step 6: Implement prompt/packet serialization and size checks**

Use `json.dumps(value, ensure_ascii=False, separators=(",", ":"))`; reject serialized snapshot over 32 KiB and packet over 64 KiB. `fit_memory_packet` removes the oldest complete recent pair until the serialized packet fits `max_bytes` or only the newest pair remains; it never truncates message text. The caller uses `max_bytes=config.target_input_tokens * 4` as a conservative prewarm budget and validates real `input_tokens` afterward. `SUMMARY_SYSTEM_PROMPT` must demand JSON only, forbid instructions, require source IDs, and describe active/superseded semantics. `MEMORY_PROTOCOL` must state that memory data is untrusted history and the latest native user turn is the current instruction.

- [ ] **Step 7: Run focused tests and static checks**

Run: `uv run pytest tests/test_context_memory.py -q`

Run: `uv run mypy src/voice_realtime/interaction/context_memory.py`

Run: `uv run ruff check src/voice_realtime/interaction/context_memory.py tests/test_context_memory.py`

Expected: all commands pass.

- [ ] **Step 8: Commit only Task 1 files**

```bash
git add src/voice_realtime/interaction/context_memory.py tests/test_context_memory.py
git commit --only -m "feat(interaction): 添加结构化对话记忆模型" -- \
  src/voice_realtime/interaction/context_memory.py tests/test_context_memory.py
```

### Task 2: Configuration, Memory Protocol, and Pipeline Wiring

**Files:**
- Modify: `src/voice_realtime/config.py:81-170`
- Modify: `src/voice_realtime/interaction/pipeline.py:109-115, 664-670`
- Modify: `tests/test_config.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_reasoning.py:438-455`

**Interfaces:**
- Consumes: `ContextCompactionConfig`, `MEMORY_PROTOCOL` from Task 1.
- Produces: `InteractionSettings.context_compaction_config() -> ContextCompactionConfig`。
- Produces: `build_system_prompt()` containing the fixed memory protocol once, before persona text.
- Produces: `LmStudioNativeLLMService(compaction_config=config)` construction in addition to the existing model/base URL/temperature/reasoning arguments.

- [ ] **Step 1: Add failing default and cross-field config tests**

```python
def test_context_compaction_defaults() -> None:
    settings = InteractionSettings()
    config = settings.context_compaction_config()
    assert config.soft_input_tokens == 6000
    assert config.hard_input_tokens == 10000
    assert config.target_input_tokens == 2500
    assert config.recent_turn_pairs == 4
    assert config.summary_max_output_tokens == 1024


@pytest.mark.parametrize(
    "kwargs",
    [
        {"context_target_input_tokens": 6000},
        {"context_hard_input_tokens": 5999},
        {"context_recent_turn_pairs": 0},
    ],
)
def test_context_compaction_rejects_invalid_ranges(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        InteractionSettings(**kwargs)
```

- [ ] **Step 2: Run config tests and verify RED**

Run: `uv run pytest tests/test_config.py -q`

Expected: failures report missing context compaction fields/method.

- [ ] **Step 3: Add exact settings fields and model validator**

Add fields named exactly:

```python
context_compaction_enabled: bool = True
context_soft_input_tokens: int = Field(default=6000, ge=512)
context_hard_input_tokens: int = Field(default=10000, ge=1024)
context_target_input_tokens: int = Field(default=2500, ge=256)
context_recent_turn_pairs: int = Field(default=4, ge=1, le=16)
context_max_unsummarized_messages: int = Field(default=40, ge=4, le=1000)
context_ttft_soft_seconds: float = Field(default=1.5, ge=0.1, le=30.0)
context_summary_max_output_tokens: int = Field(default=1024, ge=128, le=4096)
context_summary_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
context_capacity_ratio: float = Field(default=0.8, ge=0.5, le=0.95)
```

The model validator must require `target < soft < hard`. `context_compaction_config()` returns the frozen dataclass with the same values.

- [ ] **Step 4: Add failing system-prompt and pipeline-construction tests**

```python
def test_system_prompt_contains_memory_protocol_once() -> None:
    prompt = build_system_prompt("你是一位耐心顾问。")
    assert prompt.count("conversation_memory_data") == 1
    assert "最新原生 user turn" in prompt
    assert prompt.endswith("你是一位耐心顾问。")


def test_pipeline_passes_compaction_config(
    settings: InteractionSettings,
    mock_transport: MagicMock,
    mock_services: list[MagicMock],
) -> None:
    build_pipeline(settings, transport=mock_transport)
    service = mock_services[1]
    assert service.call_args.kwargs["compaction_config"] == settings.context_compaction_config()
```

- [ ] **Step 5: Wire protocol and config without disturbing echo changes**

`build_system_prompt()` returns `DEFAULT_SYSTEM_PROMPT + "\n" + MEMORY_PROTOCOL`, then appends persona. `build_pipeline()` passes `compaction_config=settings.context_compaction_config()` while preserving the existing injected `echo_state` and `echo_buffer` parameters in the dirty worktree.

- [ ] **Step 6: Run focused tests and static checks**

Run: `uv run pytest tests/test_config.py tests/test_pipeline.py tests/test_reasoning.py::TestSystemPrompt -q`

Run: `uv run mypy src/voice_realtime/config.py src/voice_realtime/interaction/pipeline.py`

Run: `uv run ruff check src/voice_realtime/config.py src/voice_realtime/interaction/pipeline.py tests/test_config.py tests/test_pipeline.py tests/test_reasoning.py`

Expected: all commands pass.

- [ ] **Step 7: Commit only Task 2 files**

Use `git diff HEAD -- src/voice_realtime/interaction/pipeline.py` first and verify the existing echo-state diff remains present. Then:

```bash
git add src/voice_realtime/config.py src/voice_realtime/interaction/pipeline.py \
  tests/test_config.py tests/test_pipeline.py tests/test_reasoning.py
git commit --only -m "feat(interaction): 配置原生上下文压缩" -- \
  src/voice_realtime/config.py src/voice_realtime/interaction/pipeline.py \
  tests/test_config.py tests/test_pipeline.py tests/test_reasoning.py
```

### Task 3: Native Usage Stats and Bounded One-Shot Calls

**Files:**
- Modify: `src/voice_realtime/interaction/reasoning.py:1-330`
- Modify: `tests/test_reasoning.py`

**Interfaces:**
- Produces: `NativeChatStats(input_tokens, total_output_tokens, reasoning_output_tokens, ttft_seconds)`。
- Produces: `NativeChatResult(content, response_id, stats)`。
- Produces: `_parse_native_result(data, require_response_id) -> NativeChatResult`。
- Produces: `LmStudioNativeLLMService._native_chat_once(payload, timeout_seconds) -> NativeChatResult`。
- Produces: `LmStudioNativeLLMService._get_model_context_length() -> int | None`，从原生 `/api/v1/models` 读取并缓存当前 loaded instance 的 context length。
- Stream commit records `last_chat_stats: NativeChatStats | None` and full assistant text.

- [ ] **Step 1: Add native-result test helpers, update fake SSE finals, and write failing stats tests**

```python
SSE_STATS = {
    "input_tokens": 6100,
    "total_output_tokens": 12,
    "reasoning_output_tokens": 0,
    "time_to_first_token_seconds": 1.7,
}


def native_result_json(
    content: str,
    response_id: str | None,
    *,
    stats: dict[str, int | float] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "model_instance_id": "m",
        "output": [{"type": "message", "content": content}],
        "stats": stats or {
            "input_tokens": 100,
            "total_output_tokens": 10,
            "reasoning_output_tokens": 0,
            "time_to_first_token_seconds": 0.2,
        },
    }
    if response_id is not None:
        body["response_id"] = response_id
    return body


def fake_json_response(data: dict[str, object], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        request=httpx.Request("POST", "http://localhost:1234/api/v1/chat"),
        json=data,
    )


def stream_lines(content: str, response_id: str, input_tokens: int) -> list[str]:
    result = native_result_json(
        content,
        response_id,
        stats={
            "input_tokens": input_tokens,
            "total_output_tokens": 8,
            "reasoning_output_tokens": 0,
            "time_to_first_token_seconds": 0.2,
        },
    )
    return [
        f'data: {json.dumps({"type": "message.delta", "content": content}, ensure_ascii=False)}',
        f'data: {json.dumps({"type": "chat.end", "result": result}, ensure_ascii=False)}',
    ]


def fake_stream_with_stats(
    stats: dict[str, int | float], response_id: str
) -> Callable[..., AsyncContextManager[FakeSSEResponse]]:
    @asynccontextmanager
    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[FakeSSEResponse]:
        result = native_result_json("你好", response_id, stats=stats)
        lines = [
            'data: {"type":"message.delta","content":"你好"}',
            f'data: {json.dumps({"type": "chat.end", "result": result}, ensure_ascii=False)}',
        ]
        yield FakeSSEResponse(lines)

    return stream


async def test_chat_end_commits_usage_stats_with_response_id() -> None:
    svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
    svc._http.stream = fake_stream_with_stats(SSE_STATS, "resp_first")
    _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]
    assert svc.last_chat_stats == NativeChatStats(6100, 12, 0, 1.7)


async def test_invalid_stats_prevents_chain_commit() -> None:
    svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
    svc._http.stream = fake_stream_with_stats({"input_tokens": -1}, "resp_bad")
    with pytest.raises(ValueError, match="stats"):
        _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]
    assert svc._previous_response_id is None
```

- [ ] **Step 2: Run stats tests and verify RED**

Run: `uv run pytest tests/test_reasoning.py -q`

Expected: failures report missing `NativeChatStats`/`last_chat_stats` and invalid final parsing.

- [ ] **Step 3: Implement strict final-result parsing**

```python
@dataclass(frozen=True, slots=True)
class NativeChatStats:
    input_tokens: int
    total_output_tokens: int
    reasoning_output_tokens: int
    ttft_seconds: float


@dataclass(frozen=True, slots=True)
class NativeChatResult:
    content: str
    response_id: str | None
    stats: NativeChatStats
```

`_parse_native_result` accepts only dict `output` with text message content, non-negative integer token fields, non-negative numeric TTFT, and optional `resp_` ID. Streaming `chat.end` uses the same parser after replacing content with accumulated deltas, so ID and stats commit atomically.

- [ ] **Step 4: Write failing one-shot payload and timeout tests**

```python
async def test_native_chat_once_returns_validated_result() -> None:
    svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
    svc._http.post = AsyncMock(return_value=fake_json_response(native_result_json("摘要", None)))
    result = await svc._native_chat_once(
        {"model": "m", "input": "历史", "store": False}, timeout_seconds=20.0
    )
    assert result.content == "摘要"
    svc._http.post.assert_awaited_once()


async def test_native_chat_once_propagates_timeout() -> None:
    svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
    svc._http.post = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    with pytest.raises(httpx.ReadTimeout):
        await svc._native_chat_once(
            {"model": "m", "input": "历史", "store": False}, timeout_seconds=1.0
        )
```

- [ ] **Step 5: Implement bounded non-streaming native call**

Use `await self._http.post(_NATIVE_CHAT_PATH, json=payload, timeout=httpx.Timeout(connect=5, read=timeout_seconds, write=10, pool=5))`, call `raise_for_status()`, parse JSON with `_parse_native_result`, and never log payload or content.

- [ ] **Step 6: Add and implement loaded-model context length discovery**

```python
async def test_model_context_length_uses_loaded_instance_and_caches() -> None:
    svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
    svc._http.get = AsyncMock(
        return_value=httpx.Response(
            200,
            request=httpx.Request("GET", "http://localhost:1234/api/v1/models"),
            json={
                "models": [
                    {
                        "key": "m",
                        "loaded_instances": [{"id": "m", "config": {"context_length": 262144}}],
                        "max_context_length": 262144,
                    }
                ]
            },
        )
    )
    assert await svc._get_model_context_length() == 262144
    assert await svc._get_model_context_length() == 262144
    svc._http.get.assert_awaited_once()
```

Implement a five-second bounded GET. Match `model.key == self._model`, prefer the matching loaded instance `config.context_length`, fall back to positive `max_context_length`, cache `None` on malformed/unavailable metadata, and never make model discovery a normal-chat failure.

- [ ] **Step 7: Run focused tests and static checks**

Run: `uv run pytest tests/test_reasoning.py -q`

Run: `uv run mypy src/voice_realtime/interaction/reasoning.py`

Run: `uv run ruff check src/voice_realtime/interaction/reasoning.py tests/test_reasoning.py`

Expected: all commands pass.

- [ ] **Step 8: Commit Task 3 files**

```bash
git add src/voice_realtime/interaction/reasoning.py tests/test_reasoning.py
git commit --only -m "feat(interaction): 采集 LM Studio 上下文用量" -- \
  src/voice_realtime/interaction/reasoning.py tests/test_reasoning.py
```

### Task 4: Background Summarization, Chain Prewarming, and Atomic Swap

**Files:**
- Modify: `src/voice_realtime/interaction/context_memory.py`
- Modify: `src/voice_realtime/interaction/reasoning.py`
- Modify: `tests/test_context_memory.py`
- Modify: `tests/test_reasoning.py`

**Interfaces:**
- Consumes: Task 1 models/policy and Task 3 `_native_chat_once`/stats.
- Produces: `_schedule_compaction(messages, assistant_text, system_prompt, generation) -> None`。
- Produces: `_run_compaction(window, system_prompt, generation, user_turns) -> None`。
- Produces: `_summarize_window(window) -> ConversationMemorySnapshot`。
- Produces: `_prewarm_chain(packet, system_prompt) -> NativeChatResult`。
- Produces: `memory_packet: ConversationMemoryPacket | None`, `compaction_task: asyncio.Task[None] | None` read-only properties for lifecycle/observability tests.

- [ ] **Step 1: Add compaction test helpers, then write failing summary payload and validation tests**

```python
def snapshot_json(start: int, end: int) -> str:
    payload = {
        "schema_version": 1,
        "source_turn_start": start,
        "source_turn_end": end,
        "participants": [
            {"id": "local_user", "role": "user", "names": ["用户"]},
            {"id": "voice_assistant", "role": "assistant", "names": ["助手"]},
        ],
        "entities": [],
        "user_preferences": [],
        "goals_and_constraints": [],
        "decisions": [],
        "open_items": [],
        "conversation_summary": "压缩后的历史。",
    }
    return json.dumps(payload, ensure_ascii=False)


def native_result(
    content: str, response_id: str | None, input_tokens: int
) -> NativeChatResult:
    return NativeChatResult(
        content=content,
        response_id=response_id,
        stats=NativeChatStats(
            input_tokens=input_tokens,
            total_output_tokens=8,
            reasoning_output_tokens=0,
            ttft_seconds=0.2,
        ),
    )


def compaction_service(soft_input_tokens: int = 512) -> LmStudioNativeLLMService:
    config = ContextCompactionConfig(
        soft_input_tokens=soft_input_tokens,
        hard_input_tokens=max(soft_input_tokens + 1, 10000),
        target_input_tokens=256,
    )
    return LmStudioNativeLLMService(
        model="m", base_url="http://localhost:1234", compaction_config=config
    )


def two_pair_window() -> CompactionWindow:
    snapshot = empty_memory_snapshot()
    return CompactionWindow(
        previous_snapshot=snapshot,
        turns_to_summarize=(
            ConversationTurn(turn_id=1, role="user", content="用户叫青竹"),
            ConversationTurn(turn_id=2, role="assistant", content="已经记住"),
        ),
        recent_turns=(
            ConversationTurn(turn_id=3, role="user", content="项目叫声流"),
            ConversationTurn(turn_id=4, role="assistant", content="项目名称已确认"),
        ),
        expected_snapshot_start=1,
        expected_snapshot_end=2,
    )


async def test_summary_uses_native_stateless_reasoning_off_payload() -> None:
    svc = compaction_service(soft_input_tokens=512)
    svc._native_chat_once = AsyncMock(
        return_value=native_result(snapshot_json(1, 2), response_id=None, input_tokens=300)
    )
    snapshot = await svc._summarize_window(two_pair_window())
    payload = svc._native_chat_once.await_args.args[0]
    assert payload["store"] is False
    assert payload["reasoning"] == "off"
    assert payload["temperature"] == 0
    assert payload["max_output_tokens"] == 1024
    assert payload["stream"] is False
    assert snapshot.source_turn_end == 2


async def test_summary_retries_invalid_json_once() -> None:
    svc = compaction_service(soft_input_tokens=512)
    svc._native_chat_once = AsyncMock(
        side_effect=[
            native_result("不是 JSON", None, 200),
            native_result(snapshot_json(1, 2), None, 220),
        ]
    )
    snapshot = await svc._summarize_window(two_pair_window())
    assert snapshot.source_turn_end == 2
    assert svc._native_chat_once.await_count == 2
```

- [ ] **Step 2: Run new tests and verify RED**

Run: `uv run pytest tests/test_reasoning.py -q -k 'summary or compaction or prewarm'`

Expected: failures report missing compaction methods/state.

- [ ] **Step 3: Implement summary and prewarm calls**

Summary payload uses `SUMMARY_SYSTEM_PROMPT`, compact JSON transcript input, current model, `reasoning:"off"`, `temperature:0`, configured `max_output_tokens`, `store:false`, `stream:false`. Parse with `parse_snapshot(expected_start, expected_end)`; one correction retry may include only the schema error category and the original bounded transcript.

Reject a summary result when `reasoning_output_tokens != 0`; a model response produced through an accidentally thinking path cannot become memory. Prewarm calls `fit_memory_packet(snapshot, recent_turns, max_bytes=config.target_input_tokens * 4)`, then sends the normal system prompt, compact packet JSON followed by the fixed instruction `仅回复 MEMORY_READY`, `reasoning:"off"`, `temperature:0`, `max_output_tokens:16`, `store:true`, `stream:false`. Accept only exact stripped content `MEMORY_READY`, reasoning tokens zero, valid response ID, and non-negative stats. If real prewarm `input_tokens` exceeds the target with only the newest pair retained, keep semantic integrity, commit the valid candidate, and emit a content-free target-miss metric.

- [ ] **Step 4: Write failing atomic-swap and stale-candidate tests**

```python
async def test_compaction_prewarm_atomically_replaces_chain() -> None:
    svc = compaction_service(soft_input_tokens=512)
    svc._previous_response_id = "resp_old"
    svc._native_chat_once = AsyncMock(
        side_effect=[
            native_result(snapshot_json(1, 2), None, 300),
            native_result("MEMORY_READY", "resp_compacted", 1800),
        ]
    )
    await svc._run_compaction(two_pair_window(), "系统", generation=0, user_turns=2)
    assert svc._previous_response_id == "resp_compacted"
    assert svc.memory_packet is not None
    assert svc.memory_packet.snapshot.source_turn_end == 2


async def test_new_request_invalidates_late_compaction_candidate() -> None:
    svc = compaction_service(soft_input_tokens=512)
    gate = asyncio.Event()

    async def delayed_call(
        payload: dict[str, Any], *, timeout_seconds: float
    ) -> NativeChatResult:
        if payload["store"] is False:
            return native_result(snapshot_json(1, 2), None, 300)
        await gate.wait()
        return native_result("MEMORY_READY", "resp_compacted", 1800)

    svc._native_chat_once = AsyncMock(side_effect=delayed_call)
    task = asyncio.create_task(
        svc._run_compaction(two_pair_window(), "系统", generation=0, user_turns=2)
    )
    svc._request_generation = 1
    gate.set()
    await task
    assert svc._previous_response_id != "resp_compacted"
    assert svc.memory_packet is None
```

- [ ] **Step 5: Schedule only after valid streamed commit**

Accumulate assistant deltas in `_native_completions`. After valid `chat.end` atomically commits normal response ID/stats, build completed turns from the frozen input messages plus accumulated assistant text, update two-consecutive-TTFT counter, call `should_compact`, and create at most one background task. Do not schedule on error, cancellation, missing final event, disabled config, insufficient complete pairs, or while a live task exists.

- [ ] **Step 6: Implement generation-safe commit and task error containment**

`_run_compaction` catches timeout/HTTP/JSON/schema errors, logs only reason/generation/counts, and leaves the old chain untouched. Commit only when captured generation equals `_request_generation`, captured `user_turns == _completed_user_turns`, service is not closed, and the current response ID still equals the response ID captured at task creation.

- [ ] **Step 7: Run focused tests and static checks**

Run: `uv run pytest tests/test_context_memory.py tests/test_reasoning.py -q`

Run: `uv run mypy src/voice_realtime/interaction/context_memory.py src/voice_realtime/interaction/reasoning.py`

Run: `uv run ruff check src/voice_realtime/interaction/context_memory.py src/voice_realtime/interaction/reasoning.py tests/test_context_memory.py tests/test_reasoning.py`

Expected: all commands pass.

- [ ] **Step 8: Commit Task 4 files**

```bash
git add src/voice_realtime/interaction/context_memory.py \
  src/voice_realtime/interaction/reasoning.py tests/test_context_memory.py tests/test_reasoning.py
git commit --only -m "feat(interaction): 原子压缩 LM Studio 对话链" -- \
  src/voice_realtime/interaction/context_memory.py \
  src/voice_realtime/interaction/reasoning.py \
  tests/test_context_memory.py tests/test_reasoning.py
```

### Task 5: Lossless Chain Recovery and Session Lifecycle

**Files:**
- Modify: `src/voice_realtime/interaction/reasoning.py`
- Modify: `src/voice_realtime/interaction/session.py`
- Modify: `tests/test_reasoning.py`
- Modify: `tests/test_interaction_context.py`
- Modify: `tests/test_interaction_session.py`

**Interfaces:**
- Produces: `reset_conversation()` cancels candidate and clears chain, snapshot, packet, stats and counters synchronously.
- Produces: `async close()` cancels/awaits candidate before closing HTTP client.
- Produces: `_recover_invalid_chain(messages, current_user, system_prompt, generation) -> str` returning a seeded response ID or raising.
- Changes: invalid `previous_response_id` retry must seed verified memory first; no empty-chain fallback when history exists.

- [ ] **Step 1: Replace the old lossy-retry test with failing memory-recovery tests**

```python
async def test_invalid_previous_id_prewarm_history_before_retrying_current_user() -> None:
    svc = compaction_service(soft_input_tokens=6000)
    payloads: list[dict[str, Any]] = []
    call_count = 0

    @asynccontextmanager
    async def fake_stream(
        _method: str, _url: str, json: dict[str, Any] | None = None, **_kwargs: Any
    ) -> AsyncIterator[FakeSSEResponse]:
        nonlocal call_count
        call_count += 1
        payloads.append(json or {})
        if call_count == 1:
            yield FakeSSEResponse(stream_lines("收到", "resp_first", input_tokens=100))
        elif call_count == 2:
            yield FakeSSEResponse(
                [],
                status_code=400,
                error_body={
                    "error": {
                        "param": "previous_response_id",
                        "message": "previous_response_id was not found",
                    }
                },
            )
        else:
            yield FakeSSEResponse(stream_lines("恢复", "resp_recovered", input_tokens=120))

    svc._http.stream = fake_stream
    svc._recover_invalid_chain = AsyncMock(return_value="resp_recovered_seed")
    _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]
    chunks = [
        chunk async for chunk in await svc.get_chat_completions(make_second_turn_context())
    ]
    assert [chunk.choices[0].delta.content for chunk in chunks] == ["恢复"]
    svc._recover_invalid_chain.assert_awaited_once()
    assert payloads[2]["previous_response_id"] == "resp_recovered_seed"
    assert payloads[2]["input"] == "第二轮用户指令"


async def test_failed_recovery_never_silently_starts_empty_chain() -> None:
    svc = compaction_service(soft_input_tokens=6000)
    call_count = 0

    @asynccontextmanager
    async def fake_stream(*_args: Any, **_kwargs: Any) -> AsyncIterator[FakeSSEResponse]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield FakeSSEResponse(stream_lines("收到", "resp_first", input_tokens=100))
        else:
            yield FakeSSEResponse(
                [],
                status_code=400,
                error_body={
                    "error": {
                        "param": "previous_response_id",
                        "message": "previous_response_id was not found",
                    }
                },
            )

    svc._http.stream = fake_stream
    svc._recover_invalid_chain = AsyncMock(side_effect=RuntimeError("上下文恢复失败"))
    _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]
    with pytest.raises(RuntimeError, match="上下文恢复"):
        _ = [
            chunk async for chunk in await svc.get_chat_completions(make_second_turn_context())
        ]
    assert svc._previous_response_id is None
```

- [ ] **Step 2: Run recovery tests and verify RED**

Run: `uv run pytest tests/test_reasoning.py -q -k 'invalid_previous or recover'`

Expected: the old implementation sends current user as an empty new chain, causing assertions to fail.

- [ ] **Step 3: Implement exact recent-turn seed and synchronous recovery**

If a committed packet exists, roll it forward with complete turns after `snapshot.source_turn_end`; otherwise create an empty `(0,0)` snapshot and keep up to four complete recent pairs, summarizing older pairs when present. Prewarm a chain, then retry the unchanged current user once with the seed response ID. Never include current user in the recovery packet, never retry another invalid ID, and never log content.

- [ ] **Step 4: Write failing reset/close cancellation tests**

```python
def valid_packet() -> ConversationMemoryPacket:
    return build_memory_packet(
        empty_memory_snapshot(),
        [
            ConversationTurn(turn_id=1, role="user", content="你好"),
            ConversationTurn(turn_id=2, role="assistant", content="你好"),
        ],
    )


async def wait_forever() -> None:
    await asyncio.Event().wait()


def test_reset_cancels_compaction_and_clears_memory() -> None:
    svc = compaction_service()
    task = MagicMock()
    task.done.return_value = False
    svc._compaction_task = task
    svc._memory_packet = valid_packet()
    svc.reset_conversation()
    task.cancel.assert_called_once_with()
    assert svc.memory_packet is None
    assert svc.last_chat_stats is None


async def test_close_awaits_cancelled_compaction_before_http_close() -> None:
    svc = compaction_service()
    svc._compaction_task = asyncio.create_task(wait_forever())
    svc._http.aclose = AsyncMock()
    await svc.close()
    assert svc._compaction_task is None
    svc._http.aclose.assert_awaited_once()
```

- [ ] **Step 5: Implement lifecycle cleanup and preserve user echo changes**

Add a private async `_cancel_compaction()` used by close/stop/cancel/cleanup. `reset_conversation()` cancels without awaiting and increments generation so late results cannot commit. `InteractionSession.clear_context()` continues to call `reset_conversation()` before queuing system-only messages. Do not remove the dirty-worktree `EchoState`, `is_echo_suppressing`, injected `echo_state`, or stop-time echo reset changes.

- [ ] **Step 6: Run lifecycle and regression tests**

Run: `uv run pytest tests/test_reasoning.py tests/test_interaction_context.py tests/test_interaction_session.py -q`

Run: `uv run mypy src/voice_realtime/interaction/reasoning.py src/voice_realtime/interaction/session.py`

Run: `uv run ruff check src/voice_realtime/interaction/reasoning.py src/voice_realtime/interaction/session.py tests/test_reasoning.py tests/test_interaction_context.py tests/test_interaction_session.py`

Expected: all commands pass.

- [ ] **Step 7: Commit only Task 5 files**

Before commit, compare `git diff HEAD -- src/voice_realtime/interaction/session.py tests/test_interaction_session.py` and verify all pre-existing echo changes remain. Then:

```bash
git add src/voice_realtime/interaction/reasoning.py src/voice_realtime/interaction/session.py \
  tests/test_reasoning.py tests/test_interaction_context.py tests/test_interaction_session.py
git commit --only -m "fix(interaction): 从压缩记忆恢复原生会话链" -- \
  src/voice_realtime/interaction/reasoning.py src/voice_realtime/interaction/session.py \
  tests/test_reasoning.py tests/test_interaction_context.py tests/test_interaction_session.py
```

### Task 6: Documentation, Real LM Studio Acceptance, and Full Quality Gates

**Files:**
- Create: `docs/decisions/0003-lm-studio-context-compaction.md`
- Modify: `AGENTS.md`
- Modify: `docs/实时语音交互与字幕-方案与最佳实践.md`
- Modify: `docs/架构图与流程图.md`
- Modify: `docs/superpowers/specs/2026-08-21-lm-studio-context-compaction-design.md`

**Interfaces:**
- Documents the final payloads, defaults, failure semantics, rollback flag and measured acceptance.
- No runtime interface changes.

- [ ] **Step 1: Run backend focused suite before real-model acceptance**

Run: `uv run pytest tests/test_context_memory.py tests/test_reasoning.py tests/test_config.py tests/test_pipeline.py tests/test_interaction_context.py tests/test_interaction_session.py -q`

Expected: all focused tests pass.

- [ ] **Step 2: Run a real 100-turn native summary/prewarm/continuation acceptance**

Use the implemented service against `qwen/qwen3.6-35b-a3b` with a test-only `ContextCompactionConfig(soft_input_tokens=512, hard_input_tokens=10000, target_input_tokens=2500, recent_turn_pairs=4, max_unsummarized_messages=40, ttft_soft_seconds=30, summary_max_output_tokens=1024, summary_timeout_seconds=20, capacity_ratio=0.8)`. Build 100 alternating completed turns with deterministic facts at early, middle and recent positions: user name `青竹`, project `声流`, file `context_memory.py`, active preference `回答简短`, superseded preference `回答详细`, one open item, and one historical prompt-injection sentence marked as quoted data. Summarize turns 1-92, preserve turns 93-100 verbatim, prewarm the new chain, then issue eight independent recall/role/update/current-instruction probes through continuations of the committed seed.

Acceptance output must show only: old/new response ID prefixes, pre/post `input_tokens`, TTFT, `reasoning_output_tokens`, and eight boolean checks. Do not print transcript, prompts, model answers or memory JSON.

Expected: response ID changes, post-compaction input tokens are at most 2500 unless the newest exact pair alone exceeds the target, all reasoning token counts are zero, role/current-instruction/injection/update checks are 100%, and at least 19 of 20 total deterministic fact assertions collected across the eight probes pass (95%).

- [ ] **Step 3: Add ADR-003 and update authoritative docs**

ADR status `Accepted`; record why native chain prewarming is chosen over LM Studio-only history and Pipecat-only summarization. Update the existing critical native-payload constraint to distinguish invalid `max_tokens` from supported `max_output_tokens`. Document defaults, `VR_INTERACTION_CONTEXT_COMPACTION_ENABLED=false` rollback, no persistence, and exact invalid-ID recovery semantics.

- [ ] **Step 4: Run the complete backend quality gate**

Run: `VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/`

Expected: all tests pass and branch coverage remains at least 80%.

- [ ] **Step 5: Run backend type and lint gates**

Run: `uv run mypy src/`

Run: `uv run ruff check src/ tests/`

Expected: both pass with no diagnostics.

- [ ] **Step 6: Run frontend regression gates**

Run from `ui/`: `npm test -- --run`

Run from `ui/`: `npm run build`

Expected: both pass. If the shared dirty worktree build fails only in unrelated user UI files, create a temporary detached worktree at current HEAD, reuse the installed dependency directory without modifying the repository, run the build there, remove only that explicit temporary worktree, and report both results.

- [ ] **Step 7: Verify diff boundaries and documentation**

Run: `git diff --check HEAD~5..HEAD`

Run: `git status --short`

Run: `git diff --cached --name-only`

Expected: no whitespace errors in task commits; all pre-existing user staged paths remain staged and are not present in this task's documentation commit unless explicitly listed.

- [ ] **Step 8: Commit documentation only**

```bash
git add docs/decisions/0003-lm-studio-context-compaction.md AGENTS.md \
  docs/实时语音交互与字幕-方案与最佳实践.md docs/架构图与流程图.md \
  docs/superpowers/specs/2026-08-21-lm-studio-context-compaction-design.md
git commit --only -m "docs: 更新 LM Studio 上下文压缩契约" -- \
  docs/decisions/0003-lm-studio-context-compaction.md AGENTS.md \
  docs/实时语音交互与字幕-方案与最佳实践.md docs/架构图与流程图.md \
  docs/superpowers/specs/2026-08-21-lm-studio-context-compaction-design.md
```

- [ ] **Step 9: Final verification snapshot**

Run: `git log -8 --oneline --decorate`

Run: `git show --stat --oneline HEAD`

Report exact test counts, coverage, real LM Studio measurements, any unrelated dirty-worktree blockers, commits created, and retained user changes.
