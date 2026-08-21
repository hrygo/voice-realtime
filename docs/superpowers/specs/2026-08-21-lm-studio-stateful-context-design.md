# LM Studio 角色感知上下文设计

## 目标

修复语音交互的 LLM 上下文传输，使模型明确知道：

1. 哪一段是受信的系统指令与当前 persona。
2. 历史每一轮分别由 user 还是 assistant 发出。
3. 哪一段是本轮需要执行的用户指令。

同时保留本机离线优先、原生 `/api/v1/chat`、`reasoning: "off"`、SSE 流式输出和现有
Pipecat 聚合器链。

## 已确认事实

- Pipecat `LLMContext` 已正确保存带 `role` 的完整历史。
- 现有适配器把所有消息转换为无角色 text items，角色边界在传输层丢失。
- LM Studio `/api/v1/chat` 把 `input` 定义为本轮 user message，把系统指令定义为独立
  `system_prompt`，且不支持在请求中直接包含 assistant 消息。
- `/api/v1/chat` 默认有状态；成功响应返回 `response_id`，后续用 `previous_response_id` 续接。
- SSE `chat.end` 携带与非流式响应等价的最终结果及可选 `response_id`。
- 2026-08-21 本机 LM Studio 0.4.21+2 实测：使用 `previous_response_id` 的第二轮能准确识别
  第一轮 user 内容，两轮 `reasoning_output_tokens` 均为 0。
- 2026-08-21 实现后真实 SSE 冒烟：首轮回复“收到”，续轮能回答“用户：青竹”；调用
  `reset_conversation()` 后新链回答“未知”，三个请求均取得新的 `resp_` response ID。

## 非目标

- 不改变 STT、VAD、回声抑制、TTS 或 UI 控制协议。
- 不把交互历史写入 PostgreSQL、文件或会议数据域。
- 不为多人会议引入 speaker identity；本设计只处理单人语音助手的 system/user/assistant 角色。
- 不切换模型、不升级依赖、不启用远程服务或 MCP。

## 双层上下文模型

### 本地事实视图

Pipecat `LLMContext` 继续作为应用内可观察的对话历史：

```json
[
  {"role": "system", "content": "默认系统提示 + persona"},
  {"role": "user", "content": "第一轮用户文本"},
  {"role": "assistant", "content": "第一轮助手文本"},
  {"role": "user", "content": "本轮用户指令"}
]
```

该视图用于聚合器、UI、测试、调试和重置判定；它不再被整体序列化到原生 `input`。

### 模型会话视图

LM Studio 保存经过 chat template 正确编码的角色链：

```text
system_prompt → user input → assistant output → user input → ...
```

首轮请求：

```json
{
  "model": "qwen/qwen3.6-35b-a3b",
  "system_prompt": "默认系统提示 + persona",
  "input": "第一轮用户文本",
  "reasoning": "off",
  "temperature": 0.7,
  "store": true,
  "stream": true
}
```

后续请求：

```json
{
  "model": "qwen/qwen3.6-35b-a3b",
  "input": "本轮用户指令",
  "previous_response_id": "resp_...",
  "reasoning": "off",
  "temperature": 0.7,
  "store": true,
  "stream": true
}
```

## 原生会话状态

`LmStudioNativeLLMService` 持有最小运行态：

- `previous_response_id: str | None`：最近一次已提交的 LM Studio 响应。
- `system_prompt: str | None`：当前链使用的系统提示词，用于识别 persona 变化。
- `completed_user_turns: int`：当前链已完成的用户轮次数，用于识别本地上下文回退。
- `request_generation: int`：请求代次，用于阻止迟到响应回写新状态。

只有当前代次的有效 `chat.end` 才能原子提交这组状态。`message.delta` 只用于实时输出，不能推进历史。

## 请求拼装规则

1. 从标准化后的 `LLMContext` 中验证第一条非空文本消息为 system、最后一条为 user。
2. `system_prompt` 只取 system 文本；`input` 只取最后一条 user 文本。
3. 正常续轮时附带已提交的 `previous_response_id`，不重复发送 system 或历史消息。
4. 没有已提交 ID、系统提示变化或本地 user 轮次回退时启动新链，重新发送 `system_prompt`。
5. 空文本、非文本本轮输入、缺少 system 或最后一条不是 user 时立即失败，不猜测角色。
6. 不在 payload 中发送 `role`、assistant 历史、`max_tokens` 或手工角色标签。

## 流式提交与并发

- 接收并转发非空 `message.delta`。
- 校验 `error` 事件并终止本轮。
- 解析 `chat.end.result.response_id`，同时校验其类型和 `resp_` 前缀。
- 流结束时必须同时满足“至少一个正文 delta”和“有效 chat.end”；否则本轮失败且状态不变。
- 每次请求取得单调递增 generation；仅 generation 仍为当前值时允许提交 ID。
- 新请求开始后，即使旧流迟到并收到 `chat.end`，也不得覆盖新请求状态。

## 重置与中断语义

- `clear_context`：本地消息替换为当前 system，同时显式重置原生会话状态。
- `set_persona`：沿用现有“设置 persona 后 clear”流程，因此创建使用新 system_prompt 的新链。
- 会话 stop/restart：LLM 服务实例随管道释放，新实例天然从空链开始。
- 用户插话或 Cancel：未收到有效 `chat.end` 时保持最近一次已提交 ID；残缺 assistant 输出不进入模型历史。
- 下一轮可从最近一次完整响应继续，当前被中断的未完成轮次视为撤销，不伪造 assistant 历史。

## 服务端状态失效

若 LM Studio 明确报告 `previous_response_id` 无效：

1. 不把现有完整历史压成普通文本重放。
2. 记录包含错误码但不包含私人对话正文的 warning。
3. 清空失效 ID，并把当前 user 指令作为新链首轮最多重试一次，携带当前 system_prompt。
4. warning 日志明确标记这是上下文断链恢复；新链不宣称保留失效链中的历史。

其他 HTTP/SSE 错误不自动重试，避免重复生成和状态分叉。

## 隐私与安全

- 会话内容只保存在本机 Pipecat 内存和 LM Studio 本地 response store，不写项目日志。
- 日志只记录 generation、是否续接、错误类型和脱敏后的 response ID 前缀。
- user 内容永远只进入 user input，不拼接进 system_prompt，维持提示词信任边界。
- 外部 SSE 数据必须验证对象类型、事件类型、正文类型和 response ID 格式。

## 可观察性

调试日志应能区分：新链、正常续轮、显式重置、中断未提交、断链重建、迟到提交被丢弃。
日志不得输出完整 prompt、用户文本或 assistant 文本。

## 修改范围

- `src/voice_realtime/interaction/reasoning.py`
  - 新增原生会话状态与严格请求拼装。
  - 完整消费 `chat.end` 并执行原子提交。
- `src/voice_realtime/interaction/session.py`
  - 保存当前管道中的 LLM 服务引用。
  - `clear_context` 时同步调用会话重置。
- `tests/test_reasoning.py`
  - 覆盖首轮、续轮、角色提取、chat.end、错误、中断、迟到流和失效 ID。
- `tests/test_runtime.py` / `tests/test_interaction_session.py`
  - 覆盖 clear/persona/stop 的重置语义。
- `AGENTS.md` 与权威方案文档
  - 替换旧的“无 role text items 按顺序隐式推断”约束。

## 迁移与回退

这是进程内状态变更，无数据库迁移。部署后新建交互管道即使用新协议；旧 orphan response chains
不再引用，可由 LM Studio 自身存储策略管理。

回退只需恢复适配器和文档提交，不涉及用户数据转换。若真实冒烟未证明角色链、清空和中断语义，
不得保留部分启用状态。

## 验收标准

1. 单元测试证明首轮 payload 只含 system_prompt 与当前 user，后续只含当前 user 与 previous ID。
2. 测试证明任何历史 assistant 文本都不会作为 user input 重发。
3. 测试证明只有有效 `chat.end` 提交 ID；中断、错误、缺失 final event 均不推进状态。
4. 测试证明 clear、persona 和上下文回退创建新链。
5. 测试证明迟到响应不能覆盖新 generation。
6. 本机真实两轮对话能识别首轮 user 信息，且 reasoning token 为 0。
7. 本机真实 clear 后无法引用旧轮内容，persona 在新链生效。
8. 后端测试、mypy strict、ruff、前端测试与生产构建全部通过。

## 实施验收（2026-08-21）

- 真实 LM Studio SSE：首轮“收到”，续轮“用户：青竹”，reset 后“未知”；三轮 response ID
  均按新链/续链语义推进。
- `VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/`：505 passed，分支覆盖率
  84.20%。
- `uv run mypy src/`：43 source files clean；`uv run ruff check src/ tests/`：clean。
- `npm test -- --run`：52 passed；当前 HEAD 的临时隔离工作区 `npm run build`：通过。
- 共享工作区的生产构建另被用户未提交的 `AssistantPanel.tsx` 文字输入事件字段错误阻塞；该文件不在
  本设计实施范围内，也未纳入本任务提交。

## 参考

- https://lmstudio.ai/docs/developer/rest/chat
- https://lmstudio.ai/docs/developer/rest/stateful-chats
- https://lmstudio.ai/docs/developer/rest/streaming-events
- https://lmstudio.ai/docs/developer/rest
- `docs/decisions/0002-lm-studio-stateful-chat-context.md`
