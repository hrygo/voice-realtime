# LM Studio Stateful Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让语音助手通过 LM Studio 原生有状态会话链准确保留 system/user/assistant 角色，并只把本轮用户指令作为当前 input。

**Architecture:** Pipecat 继续保存应用内带角色历史；`LmStudioNativeLLMService` 只提取 system prompt 与最后一条 user 文本，首轮创建 native response chain，后续用 `previous_response_id` 续接。只有当前请求代次收到有效 `chat.end` 后才原子提交链状态；clear/persona/上下文回退会新建链。

**Tech Stack:** Python 3.12、Pipecat 1.7、httpx、LM Studio 0.4 `/api/v1/chat`、pytest、mypy strict、ruff

**Spec:** `docs/superpowers/specs/2026-08-21-lm-studio-stateful-context-design.md`

## Global Constraints

- 严格使用 Python 3.12，不新增或升级依赖。
- LLM 请求继续走 `POST /api/v1/chat`，固定支持 `reasoning: "off"`。
- 不把历史 assistant 消息、`role`、`max_tokens` 或手工角色标签放入 native `input`。
- 对话正文不得写日志、数据库或项目文件。
- 只有有效 `chat.end.result.response_id` 才能推进会话链。
- 保留现有 Pipecat、STT、回声抑制、TTS 和 UI 控制协议。

---

### Task 1: 原生角色上下文与会话状态

**Files:**
- Modify: `src/voice_realtime/interaction/reasoning.py`
- Test: `tests/test_reasoning.py`

**Interfaces:**
- Consumes: Pipecat `LLMContext` 和其 adapter 产生的 `ChatCompletionMessageParam` 列表。
- Produces: `LmStudioNativeLLMService.reset_conversation() -> None`；首轮/续轮 native payload；只读测试属性 `_previous_response_id`、`_completed_user_turns` 和 `_request_generation`。

- [ ] **Step 1: 将旧 payload 测试改成角色感知的失败测试**

```python
assert payload["system_prompt"] == "你是一个中文语音助手"
assert payload["input"] == "你好"
assert payload["store"] is True
assert "previous_response_id" not in payload
assert all(message["content"] not in str(payload) for message in historical_assistant)
```

测试 SSE 必须加入：

```python
'data: {"type":"chat.end","result":{"response_id":"resp_first"}}'
```

- [ ] **Step 2: 运行目标测试并确认旧实现失败**

Run: `uv run pytest tests/test_reasoning.py::TestLmStudioNativeLLMService::test_native_request_payload_and_sse_conversion -q`

Expected: FAIL，旧 payload 的 `input` 是无角色 text items，且没有 `system_prompt` / `store`。

- [ ] **Step 3: 添加上下文提取和最小会话状态**

在 `reasoning.py` 中加入严格提取函数：

```python
def _conversation_input(messages: list[ChatCompletionMessageParam]) -> tuple[str, str, int]:
    system = [m for m in messages if m.get("role") == "system"]
    if len(system) != 1 or not isinstance(system[0].get("content"), str):
        raise ValueError("LM context must contain exactly one text system message")
    if not messages or messages[-1].get("role") != "user":
        raise ValueError("LM context must end with a text user message")
    user_input = _text_content(messages[-1])
    if not user_input:
        raise ValueError("LM context user message must contain text")
    user_turns = sum(1 for m in messages if m.get("role") == "user" and _text_content(m))
    return cast(str, system[0]["content"]), user_input, user_turns
```

服务初始化并维护：

```python
self._previous_response_id: str | None = None
self._system_prompt: str | None = None
self._completed_user_turns = 0
self._request_generation = 0
```

`reset_conversation()` 增加 generation 并清空其余字段。新链 payload 使用
`system_prompt + input + store`；正常续轮使用 `input + previous_response_id + store`。

- [ ] **Step 4: 运行目标测试并确认转绿**

Run: `uv run pytest tests/test_reasoning.py::TestLmStudioNativeLLMService::test_native_request_payload_and_sse_conversion -q`

Expected: PASS。

- [ ] **Step 5: 添加续轮、历史 assistant 不重发、非法上下文和 reset 测试**

测试必须覆盖：

```python
assert second_payload["input"] == "第二轮用户指令"
assert second_payload["previous_response_id"] == "resp_first"
assert "system_prompt" not in second_payload
assert "第一轮助手回答" not in str(second_payload)
svc.reset_conversation()
assert svc._previous_response_id is None
```

并参数化验证：缺少 system、多个 system、最后一条不是 user、user content 非文本均抛
`ValueError`。

- [ ] **Step 6: 运行 reasoning 测试文件**

Run: `uv run pytest tests/test_reasoning.py -q`

Expected: PASS。

- [ ] **Step 7: 提交原生上下文切片**

```bash
git add src/voice_realtime/interaction/reasoning.py tests/test_reasoning.py
git commit -m "fix(interaction): 保留 LM Studio 原生对话角色"
```

### Task 2: SSE 原子提交、竞态与断链恢复

**Files:**
- Modify: `src/voice_realtime/interaction/reasoning.py`
- Test: `tests/test_reasoning.py`

**Interfaces:**
- Consumes: Task 1 的 native payload、request generation 和上下文元数据。
- Produces: `_native_completions(payload, *, generation, system_prompt, user_turns, allow_chain_retry=True)`；有效 `chat.end` 原子提交；失效 previous ID 最多重建一次。

- [ ] **Step 1: 添加缺失 final、错误、中断和迟到提交的失败测试**

测试断言：

```python
with pytest.raises(RuntimeError, match="chat.end"):
    await consume(lines_with_delta_only)
assert svc._previous_response_id is None

svc.reset_conversation()  # 使在途 generation 过期
await consume(old_stream)
assert svc._previous_response_id is None
```

另测无效 `response_id`、非对象 `chat.end.result`、显式 error event 都不提交状态。

- [ ] **Step 2: 运行新增测试并确认失败**

Run: `uv run pytest tests/test_reasoning.py -q`

Expected: FAIL，旧流消费器不要求 `chat.end`、不保存 ID、无 generation 防护。

- [ ] **Step 3: 实现严格 chat.end 与代次提交**

核心提交条件：

```python
if not saw_content:
    raise RuntimeError("LM Studio returned no message content")
if response_id is None:
    raise RuntimeError("LM Studio stream ended without a valid chat.end")
if generation == self._request_generation:
    self._previous_response_id = response_id
    self._system_prompt = system_prompt
    self._completed_user_turns = user_turns
```

`response_id` 必须匹配 `^resp_[A-Za-z0-9_-]+$`。任何 error、异常、取消或过期 generation
都不得推进状态。

- [ ] **Step 4: 实现 previous_response_id 失效的一次性新链重试**

只对 HTTP 400/404 且结构化 error 的 `param == "previous_response_id"`，或 error message
明确包含该字段时执行：清空失效链、移除 `previous_response_id`、加入当前 `system_prompt`，用同一
generation 重试一次。其他状态码和第二次失败原样抛出。

- [ ] **Step 5: 运行 reasoning 测试并确认转绿**

Run: `uv run pytest tests/test_reasoning.py -q`

Expected: PASS。

- [ ] **Step 6: 提交流式状态切片**

```bash
git add src/voice_realtime/interaction/reasoning.py tests/test_reasoning.py
git commit -m "fix(interaction): 原子提交 LM Studio 会话状态"
```

### Task 3: clear/persona 生命周期同步重置

**Files:**
- Modify: `src/voice_realtime/interaction/session.py`
- Test: `tests/test_interaction_session.py`
- Test: `tests/test_runtime.py`

**Interfaces:**
- Consumes: Task 1 的 `LmStudioNativeLLMService.reset_conversation()`。
- Produces: `InteractionSession._llm_service: LmStudioNativeLLMService | None`；clear 时同时重置 native chain 和 Pipecat messages。

- [ ] **Step 1: 添加 clear_context 会重置原生链的失败测试**

构造 `pipeline.processors=[MagicMock(spec=LmStudioNativeLLMService)]`，启动 session 后执行：

```python
await session.clear_context()
native.reset_conversation.assert_called_once_with()
worker.queue_frame.assert_awaited_once()
```

并验证 stop 后 `_llm_service is None`，没有 worker 时 clear 仍为幂等 no-op。

- [ ] **Step 2: 运行 session/runtime 目标测试并确认失败**

Run: `uv run pytest tests/test_interaction_session.py tests/test_runtime.py::TestControlCommands -q`

Expected: FAIL，session 尚未保存或重置 LLM 服务引用。

- [ ] **Step 3: 实现服务发现、重置和引用清理**

在 pipeline 装配后查找唯一 `LmStudioNativeLLMService`：

```python
self._llm_service = next(
    (p for p in pipeline.processors if isinstance(p, LmStudioNativeLLMService)),
    None,
)
```

`clear_context()` 在排队 `LLMMessagesUpdateFrame` 前使在途 generation 失效并重置 native chain；
`_clear_runtime_references()` 释放引用。persona 继续复用 control 层现有 set-then-clear 流程。

- [ ] **Step 4: 运行 session/runtime 目标测试并确认转绿**

Run: `uv run pytest tests/test_interaction_session.py tests/test_runtime.py::TestControlCommands -q`

Expected: PASS。

- [ ] **Step 5: 提交生命周期切片**

```bash
git add src/voice_realtime/interaction/session.py tests/test_interaction_session.py tests/test_runtime.py
git commit -m "fix(interaction): 清空上下文时重置原生会话"
```

### Task 4: 契约文档、真实冒烟与全量验证

**Files:**
- Modify: `AGENTS.md`
- Modify: `docs/实时语音交互与字幕-方案与最佳实践.md`
- Modify: `docs/架构图与流程图.md`
- Modify: `docs/superpowers/specs/2026-08-21-lm-studio-stateful-context-design.md`（只回填实测结果）

**Interfaces:**
- Consumes: Tasks 1-3 的最终协议和测试结果。
- Produces: 与实现一致的权威运行约束和可复现验收记录。

- [ ] **Step 1: 更新协议文档**

删除“无 role text items 按顺序隐式推断角色”，替换为：首轮 `system_prompt + input`，后续
`previous_response_id + input`，只在 `chat.end` 提交 ID，clear/persona 创建新链。

- [ ] **Step 2: 运行后端目标质量门禁**

Run: `uv run pytest tests/test_reasoning.py tests/test_interaction_session.py tests/test_runtime.py -q`

Run: `uv run mypy src/`

Run: `uv run ruff check src/ tests/`

Expected: 全部退出码 0。

- [ ] **Step 3: 运行本机真实 LM Studio 冒烟**

使用 `qwen/qwen3.6-35b-a3b` 验证：首轮保存一个用户属性，第二轮通过 previous ID 回答该属性；
两轮 `reasoning_output_tokens == 0`。随后创建新链，验证旧属性不可引用且新 persona 生效。不得把
测试正文写入日志或项目文件。

- [ ] **Step 4: 运行完整项目门禁**

Run: `VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/`

Run: `uv run mypy src/`

Run: `uv run ruff check src/ tests/`

Run: `cd ui && npm test -- --run`

Run: `cd ui && npm run build`

Expected: 后端覆盖率达到配置阈值且全部门禁退出码 0。

- [ ] **Step 5: 自审 diff 并提交文档与验收结果**

```bash
git diff --check
git diff --stat HEAD~3
git add AGENTS.md docs/
git commit -m "docs: 更新 LM Studio 有状态上下文契约"
```

- [ ] **Step 6: 最终审查**

按正确性、可读性、架构、安全、性能五个维度复核全部提交；确认无角色压平、正文日志、状态竞态、
无界重试、依赖升级或无关重构。
