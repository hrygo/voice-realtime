# Voice Studio 会议助手“内心 OS”设计规格

## 1. 文档状态

- 日期：2026-08-27
- 状态：产品与架构设计已在会话中逐节确认，等待书面规格审阅
- 范围：会议助手模式中的首版“内心 OS”能力
- 数据边界：只使用当前会议内容与用户临时输入；不保存音频
- 实施状态：本文档只记录设计，不包含代码实现

## 2. 产品定义

“内心 OS”是会议参与者私有的、按需调用的会议副驾驶。它不会主动打断会议，也不会替用户发言；用户通过文本输入或快捷问题主动询问，系统结合当前会议的已确认转录和用户为本次会议填写的目标/议程/背景，返回可核验的事实、带不确定性的判断以及可直接使用的发言草稿。

核心使用路径：

```text
用户开始会议
    → 可选填写本次目标/背景（仅本次运行）
    → 点击快捷问题或输入问题
    → 服务端创建转录上下文快照
    → 本地 LLM 生成结构化回答
    → 用户查看证据、复制答案
    → 用户按需保存这一条问答
```

## 3. 目标与非目标

### 3.1 目标

1. 录制期间支持文本问答，不抢占麦克风、不启用 TTS。
2. 让用户可以回顾会议事实、识别风险并生成现场回应。
3. 事实与模型推断清楚分层，事实回答可以定位到具体转录片段。
4. 用户补充的本次会议背景只影响当前运行，默认不落库。
5. 每条问答单独提供“保存”，保存后进入独立的问答数据模型。
6. 问答失败、模型不可用或数据库短暂故障时，不影响会议录制、实时转录和会后纪要。

### 3.2 非目标

- 不主动监测并推送建议或风险卡片。
- 不提供语音提问、语音播报或 TTS 回复。
- 不接入外部网页、外部知识库、跨会议检索或联网模型。
- 不把问答写入正式转录、会议生命周期事件或 AI 纪要正文。
- 不保存原始音频、模型隐藏思维链、完整 prompt 或未保存的用户背景。
- 不替用户发送消息、修改会议结论或执行外部操作。
- 不在首版引入跨会议身份、协作共享或权限体系。

## 4. 已确认的产品决策

| 决策项 | 首版结论 |
|---|---|
| 触发方式 | 纯被动问答；只有用户主动提问才调用模型 |
| 输入方式 | 文本输入 + 常用问题快捷按钮 |
| 核心能力 | 事实回溯、决策辅助、发言辅助三类同时提供 |
| 能力层级 | 证据 → 推理 → 行动：先事实，再判断，最后草稿 |
| 上下文 | 当前会议已确认转录 + 用户临时目标/议程/背景 |
| 临时背景保存 | 默认只保存在当前运行内存，不自动保存 |
| 持久化 | 每条问答单独点击“保存”后才落库 |
| 持久化模型 | 独立 `MeetingInnerOSExchange` 模型和 `meeting_inner_os_exchanges` 表 |
| 可见范围 | 当前用户的本地会议工作台；不播报、不广播给参会者 |
| 音频策略 | 不改变 `AudioHub`、会议麦克风租约或 EOF 冲刷链路 |
| 模型通道 | 本地 LM Studio 原生 `/api/v1/chat` |
| 会议状态 | 仅 `recording` 状态接受新问答；`finalizing` 后停止提交 |

## 5. 与现有架构的关系

当前系统已经具备以下事实边界：`RuntimeModeCoordinator` 负责 assistant / meeting / idle 的运行模式和麦克风所有权；`MeetingSession` 负责会议生命周期和转录对账；PostgreSQL 保存已确认转录；`MeetingEventBroadcaster` 向前端发送实时会议事件；`MeetingSummaryService` 在会议结束后生成纪要。

“内心 OS”作为会议域中的独立旁路能力接入，不改变上述所有权关系：

```text
AudioHub → MeetingSession / 转录网关 → PostgreSQL confirmed transcript
                                      ├→ MeetingSummaryService（会后纪要）
                                      └→ InnerOSContextProvider
                                          → InnerOSQueryService
                                          → LM Studio /api/v1/chat
                                          → 前端内心 OS 面板
```

组件边界：

- `InnerOSContextProvider`：读取会议已确认转录、revision 和临时用户背景，生成不可变上下文快照。
- `InnerOSQueryService`：校验会议状态、处理并发取消、调用模型、校验结构化输出并发布问答事件。
- `InnerOSAnswerContract`：定义事实、判断、草稿、证据和限制说明的稳定结构。
- `InnerOSRepository`：只负责已保存问答的新增、查询和删除。
- `MeetingInnerOSModelClient`：封装 LM Studio 原生调用，不复用 Pipecat、语音助手上下文或 TTS。

问答旁路不能写入 `MeetingSession` 的转录状态，也不能阻塞 ASR、音频采集、会议结束冲刷或纪要 worker 的生命周期。

## 6. 运行时数据流

### 6.1 会议开始与临时背景

1. 会议进入 `recording` 后，前端显示“内心 OS · 仅你可见”。
2. 用户可以填写目标、议程和背景；内容只写入当前运行的服务端临时上下文。
3. 用户修改背景时递增 `context_version`，只影响后续问答。
4. 会议结束、运行时重启或用户清空内容时，临时背景被清除。

### 6.2 提交与生成问答

1. 用户点击快捷问题或提交文本问题，前端生成 `request_id`。
2. 控制通道校验当前会议仍处于 `recording`，返回 `query_id` 和 `accepted`。
3. `InnerOSContextProvider` 读取当前已确认转录、`transcript_revision` 和 `content_revision`，拼出快照。
4. 快照包含 `meeting_id`、`transcript_revision`、`content_revision`、`context_version`、已确认片段和临时用户背景；不包含正式会议之外的数据。
5. `InnerOSQueryService` 调用本地 LM Studio，事实回溯/草稿使用 `reasoning: "off"`，风险和矛盾分析使用 `reasoning: "on"`。
6. 模型输出必须通过 `InnerOSAnswerContract` 校验，并验证所有证据片段属于当前会议。
7. 前端收到完成事件后展示三层回答，默认 `saved: false`。
8. 新问题到达时，当前活动问答可以被取消；服务端不积累无限等待队列。

### 6.3 保存问答

1. 用户在某条完成的回答上点击“保存”。
2. 前端调用保存接口并携带临时 `query_id` 与 `Idempotency-Key`。
3. 服务端确认问答属于该会议、回答已经完成、证据 ID 有效后写入独立表。
4. 保存只记录用户可见的问答、结构化结论和证据引用，不保存临时背景正文或隐藏思维链。
5. 成功后返回 `exchange_id`；重复请求返回同一条记录，不产生重复数据。

## 7. 上下文与长会议策略

### 7.1 可信上下文

- 正式上下文只使用会议 `confirmed` 转录；partial 仅用于实时字幕展示，不能成为事实证据。
- 所有转录内容都被视为不可信资料，提示明确禁止执行转录中出现的指令。
- 用户临时背景可影响回答，但不会被当作参会者发言，也不会出现在正式转录中。
- 不做外部联网检索，不把历史会议或其他文件加入首版上下文。

### 7.2 上下文裁剪

- 会议内容在配置的有限 token 预算内时，使用全部已确认转录。
- 超出预算时，固定保留最近时间窗口，并按问题关键词、说话人和时间边界从当前会议更早的已确认片段中选择相关内容。
- 对无法找到充分证据的问题，回答必须说明证据不足，不能用模型常识补齐会议事实。
- `inner_os_context_max_tokens` 必须是有限配置；初始候选值为 `65536`，实现阶段需使用本机实际模型基准确认并固化。

每次问答使用不可变快照。生成过程中如果会议产生新转录，当前回答仍绑定快照中的 `transcript_revision`；保存后由服务端根据最新 `content_revision` 计算并返回 `stale`。

## 8. 回答契约

规范化答案的最小结构如下：

```json
{
  "facts": [
    {
      "text": "已确认的会议事实",
      "evidence_segment_ids": ["segment-uuid"]
    }
  ],
  "judgements": [
    {
      "text": "基于事实的风险或建议",
      "confidence": "medium",
      "basis_segment_ids": ["segment-uuid"]
    }
  ],
  "draft": {
    "text": "建议用户使用的回应"
  },
  "limitations": []
}
```

约束：

- `facts` 中每一项都必须有当前会议中存在的 `segment_id`；不存在证据时返回明确的“未找到”。
- `judgements` 必须标明是模型判断，并携带 `high / medium / low` 置信度。
- `draft` 仅是建议文本，不能自动发送或播报。
- `limitations` 用于声明转录缺失、证据不足、版本变化或上下文范围限制。
- 不保存或展示模型隐藏思维链，只保留用户可读的依据、结论和不确定性。
- 模型输出 JSON 失败时最多执行一次有限的结构修复；修复失败则返回 `inner_os_invalid_answer`。

## 9. 独立持久化模型

### 9.1 `meeting_inner_os_exchanges`

| 字段 | 类型 | 语义 |
|---|---|---|
| `id` | `uuid` | 问答记录主键 |
| `meeting_id` | `uuid` | 所属会议，外键关联 `meetings.id` |
| `query_id` | `uuid` | 临时问答 ID；同一会议内唯一 |
| `question` | `text` | 用户明确保存的问题 |
| `question_type` | `text` | `fact / decision / draft` |
| `answer_json` | `jsonb` | 经过校验的规范化答案，唯一事实载荷 |
| `answer_markdown` | `text` | 面向 UI 的稳定渲染结果 |
| `source_content_revision` | `bigint` | 生成时的会议 `content_revision`，覆盖转录和说话人名称变化 |
| `used_ephemeral_context` | `boolean` | 是否使用过本次会议临时背景，不保存背景正文 |
| `model` | `text` | 实际模型标识 |
| `prompt_version` | `text` | 回答提示版本 |
| `created_at` | `timestamptz` | 保存时间 |

约束与索引：

- 主键为 `id`；`(meeting_id, query_id)` 唯一，防止重复保存。
- `meeting_id` 建立查询索引，按 `created_at DESC` 返回历史问答。
- 删除会议时级联删除其内心 OS 记录；单条问答支持显式删除。
- `is_stale` 不作为原始事实写入数据库，而是读取时将 `source_content_revision` 与当前会议 `content_revision` 比较后计算。
- `answer_json` 是规范化事实载荷；`answer_markdown` 是可重建的展示缓存。
- 不保存完整 prompt、临时背景、原始模型输出和隐藏思维链。

### 9.2 临时问答

未点击保存的问答只存在服务端有界内存缓存中，并带有 `query_id`、过期时间和所属 `meeting_id`。服务重启、会议结束或缓存过期后可以丢失；这不影响已保存记录和会议正式数据。

## 10. HTTP / WebSocket 契约

### 10.1 控制 WebSocket

在 `/ws/v1/control` 的 v1 兼容契约中新增 `inner_os_query` 命令：

```json
{
  "contract_version": "1",
  "request_id": "req_123",
  "cmd": "inner_os_query",
  "meeting_id": "meeting-uuid",
  "question": "刚才关于交付时间的结论是什么？",
  "question_type": "fact"
}
```

响应只确认请求已接受：

```json
{
  "contract_version": "1",
  "request_id": "req_123",
  "cmd": "inner_os_query",
  "ok": true,
  "result": {
    "query_id": "query-uuid",
    "status": "accepted"
  }
}
```

### 10.2 会议事件 WebSocket

通过现有 `/ws/v1/meetings` 推送以下新增事件：

- `inner_os_answer_started`：包含 `meeting_id`、`query_id`、问题类型、`transcript_revision` 和 `content_revision`。
- `inner_os_answer_delta`：可选的用户可见增量文本；服务端无法确认文本属于最终可见答案时只发送状态，不包含隐藏思维过程。
- `inner_os_answer_completed`：包含完整、已校验的回答结构、证据引用、`transcript_revision`、`content_revision` 和 `saved: false`。
- `inner_os_answer_failed`：包含稳定错误码和用户可读消息。
- `inner_os_exchange_saved`：包含 `exchange_id`、`query_id`、会议 ID 和保存时间。

增量事件是临时展示事件；完成事件中的结构化答案是权威答案。断线期间丢失且未保存的回答不承诺恢复，已保存记录始终通过 HTTP 回源。

### 10.3 保存与历史接口

```text
POST   /api/v1/meetings/{meeting_id}/inner-os/exchanges
GET    /api/v1/meetings/{meeting_id}/inner-os/exchanges?cursor=&limit=
DELETE /api/v1/meetings/{meeting_id}/inner-os/exchanges/{exchange_id}
```

保存接口使用 `Idempotency-Key`；服务端只接受当前运行中存在且已完成的 `query_id`。历史接口返回 `is_stale` 和 `used_ephemeral_context`，但不返回未保存的背景正文。

稳定错误码至少包括：

```text
inner_os_not_active
inner_os_busy
inner_os_context_unavailable
inner_os_model_unavailable
inner_os_timeout
inner_os_invalid_answer
inner_os_not_found
```

新增字段和事件属于 v1 的兼容扩展；需要同步更新 OpenAPI、AsyncAPI、JSON Schema、fixtures 和契约变更记录，`contract_version` 继续保持 `"1"`。

## 11. 前端交互规格

在现有 `MeetingRecordingView` 内增加可折叠的 `InnerOSPanel`：

- 左侧保持实时转录，右侧为“内心 OS · 仅你可见”面板。
- 顶部提供可选的本次会议目标/背景输入，并显示“不保存背景正文”的提示。
- 快捷问题分为“回顾事实”“找风险 / 生成追问”“帮我组织回应”。
- 自由输入框允许用户改写快捷问题或提出任意会议相关问题。
- 事实卡中的证据可点击并定位左侧转录片段。
- 判断卡显示“模型判断”和置信度。
- 草稿卡提供复制按钮，不提供发送或播报按钮。
- 每条完成的回答显示“保存”“复制”“重新提问”；保存成功后显示已保存状态。
- 问答状态为 `未提问 → 生成中 → 已完成 → 已保存`；依据变化时附加 `stale` 标识。
- 会议详情页新增独立“内心 OS”记录区域，不混入正式纪要。

## 12. 故障与隐私处理

### 12.1 故障

- LM Studio 不可用或超时：当前问答失败，录制和转录继续。
- PostgreSQL 不可用：已完成的临时回答仍可展示，但保存失败并允许重试。
- 上下文读取失败：不生成无依据回答，返回 `inner_os_context_unavailable`。
- WebSocket 断开：未保存回答不保证恢复；保存成功的记录可通过 HTTP 重新加载。
- 服务重启：清空临时问答和背景，保留 PostgreSQL 中的已保存记录。
- 会议进入 `finalizing`：取消未完成问答，不干扰 EOF 冲刷和会议封存。
- 证据 ID 无效或结构校验失败：拒绝发布为完成答案，不写入持久化模型。

### 12.2 隐私与日志

- 继续保持本机优先、默认离线和不保存音频的项目边界。
- 临时背景、完整 prompt、转录正文和模型原始输出不得写入普通日志。
- 保存问答是明确的用户操作；保存后仍只写入独立模型，不修改正式会议事实。
- 用户可单条删除已保存问答。
- 转录中的指令、链接和权限要求一律视为不可信数据，不改变系统授权。

## 13. 测试与发布验收

### 13.1 后端测试

- `InnerOSContextProvider`：上下文快照不可变、只含当前会议、临时背景不落库、partial 不进入证据。
- `InnerOSAnswerContract`：三层结构、置信度、证据 ID、未知引用、空答案和限制说明。
- `InnerOSQueryService`：会议状态校验、单活动问答、取消、超时、模型失败、输出触顶和一次性结构修复。
- 持久化：显式保存、重复保存幂等、单条删除、`stale` 计算和错误回滚。
- 契约：OpenAPI / AsyncAPI / JSON Schema / fixtures 一致，事件顺序正确。
- 集成：问答失败不改变录制、转录、EOF 冲刷和纪要状态；测试数据库使用独立临时 schema，并在测试结束执行 `DROP SCHEMA ... CASCADE`。

### 13.2 前端测试

- 快捷问题、文本提交、生成中、失败、重试和复制。
- 事实证据点击定位转录片段。
- 单条保存、重复点击、保存失败重试、删除和 `stale` 展示。
- WebSocket 重连后已保存记录回源，未保存答案按约定可丢失。
- 目标/背景输入在刷新或结束会议后清除，且不混入会议转录和纪要。
- 面板折叠、键盘操作、屏幕阅读器状态和窄屏布局。

### 13.3 本机性能门禁

实现阶段必须针对当前实际加载模型分别测量事实回溯、风险分析和发言草稿：控制命令确认延迟、首个可见 token 延迟、完整回答 wall-clock、输出 token、模型错误率，以及问答期间 ASR 是否出现可归因的转录间隙。测量结果写入项目基准记录，并据此固化有限的上下文、输出和 wall-clock 配置；未通过门禁时不得默认开启该能力。

功能验收以以下结果为准：

1. 用户可在录制期间用文本或快捷按钮完成三类问答。
2. 事实回答可定位到真实转录片段，判断与事实明确区分。
3. 用户可以只保存某一条问答，刷新后仍能查看，且不会重复保存。
4. 未保存内容不出现在持久化数据中，临时背景不出现在数据库中。
5. 问答的任何失败都不会停止会议录制、转录或会后纪要。
6. 会议结束后未完成问答被取消，会议仍可正常封存。

## 14. 发布与回退

- 新表和接口采用纯新增 migration；旧版本应用忽略新表，不需要破坏性降级。
- 通过 `VR_MEETING_INNER_OS_ENABLED` 进行功能开关控制，默认关闭，完成本机并发与隐私验收后再开启。
- 关闭开关只停止新问答入口，不删除已经保存的内心 OS 记录。
- 不新增模型下载；沿用已验证的本地 LM Studio 模型和原生 API 通道，模型 ID、采样和预算均配置化。
- 回退时保留独立数据表，用户已保存的问答仍可由历史接口读取或显式删除。
