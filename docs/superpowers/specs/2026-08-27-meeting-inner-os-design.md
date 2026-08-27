---
title: "Voice Studio 会议助手‘内心 OS’设计规格"
description: "本地私密会议副驾驶的证据问答、发言草稿、持久化契约、产品门禁与迭代路线设计"
status: under_review
type: technical_spec
category: meeting
version: "v1.0.0"
date: 2026-08-27
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-core"
tags:
  - meeting-assistant
  - inner-os
  - local-llm
  - privacy
  - evidence-grounded-ai
scope:
  - "voice_realtime.meeting"
  - "voice_realtime.ui"
  - "ui.features.innerOS"
related_documents:
  - "docs/superpowers/specs/2026-08-21-meeting-assistant-design.md"
  - "docs/architecture/全链路语音交互与会议助手-技术方案与实施方案.md"
  - "docs/manuals/Voice-Studio-UI-设计方案.md"
  - "docs/decisions/0005-server-side-runtime-workload-arbitration.md"
  - "docs/decisions/0007-bounded-meeting-summary-generation.md"
contracts:
  - "contracts/meeting-assistant/v1/"
---

# Voice Studio 会议助手“内心 OS”设计规格

## 1. 文档状态

- 日期：2026-08-27
- 状态：已完成第二轮产品与架构联合审查，等待用户确认后编写实施计划
- 范围：会议助手模式中的首版“内心 OS”能力
- 数据边界：只使用当前会议内容与用户临时输入；不保存音频
- 实施状态：本文档只记录设计，不包含代码实现

## 2. 产品定义

“内心 OS”是会议参与者私有的、按需调用的会议副驾驶。它不会主动打断会议，也不会替用户发言；用户通过文本输入或快捷问题主动询问，系统结合当前会议的已确认转录和用户为本次会议填写的目标/议程/背景，返回可核验的事实、带不确定性的判断以及可直接使用的发言草稿。

### 2.1 首发用户与核心场景

首发服务于在本机参加中文产品评审、技术方案评审、需求澄清和项目决策会议的单一操作者。典型情境不是“会后再看一份摘要”，而是会议仍在快速推进时，用户需要在不打断发言人的前提下完成以下任务：

1. **跟上事实**：快速确认刚才的结论、承诺、负责人、时间点和未决问题。
2. **准备回应**：基于会议已经说过的内容，组织一段可直接复制但必须由用户自行决定是否说出的回应。
3. **检查风险**：在事实证据充分时识别分歧、依赖、遗漏和可追问点；这一能力风险与时延更高，不作为首发默认主路径。

核心 JTBD：**当会议讨论快速推进、我需要确认事实或立即回应时，帮助我在约 10 秒内获得一份仅基于当前会议、可定位证据、不会替我行动的私密答案。**

### 2.2 产品定位与差异化

首版不以“功能数量最多”为目标，而以以下组合形成差异化：

- **本地与私密**：默认离线、本机推理、无会议机器人加入、未保存问答不形成长期记录。
- **证据优先**：事实、判断和草稿分层；用户可以从答案跳回原始 confirmed 转录。
- **克制的副驾驶**：不主动打断、不自动发送、不改写正式纪要，不把模型建议伪装为会议事实。
- **与现有工作台连续**：复用当前已有的重点片段、说话人命名、会议历史、证据定位、复制与导出能力，而不是建立第二套会议产品。

### 2.3 第二轮产品审查结论

| 审查维度 | 结论 | 方案调整 |
|---|---|---|
| 用户价值 | “会议内即时确认与回应”比泛化聊天更聚焦 | 首发默认开放 `fact` 与 `draft`，`analysis / mixed` 先作为评测能力 |
| 信任 | 仅展示免责声明不足以建立信任 | 保留逐条证据、事实/判断分层、限制码和证据变化状态 |
| 发现性 | 空白输入框会提高首次使用成本 | 提供基于会议阶段的少量快捷问题，不自动调用模型 |
| 既有资产 | 当前 UI 已支持重点标记、说话人命名、证据定位、纪要和导出 | 重点片段作为显式相关度信号；说话人映射沿用事实源；后续问答进入现有详情页 |
| 范围 | 三类能力同日默认发布会放大时延与错误面 | 契约一次设计完整，产品能力按评测门禁逐步启用 |
| 闭环 | “生成完成”不等于用户获得价值 | 用复制、保存、明确有用反馈和再次提问衡量有效回答 |
| 演进 | 会中问答之后自然需求是会后追问、场景模板和主动提醒 | 明确阶段路线及每阶段准入/退出条件，不直接跳到跨会议 RAG |

### 2.4 市场最佳实践校准

截至 2026-08-27，主流会议助手的官方产品资料共同覆盖侧边栏私密问答、建议问题、来源引用、会中/会后连续问答、可复用模板、多会议分析和外部工作流。基于这些能力的依赖关系，本文推断更稳妥的演进顺序是先建立私密问答与引用信任，再扩展连续使用、模板、多会议和外部行动。本文只吸收适合本项目数据边界的产品模式，不复制其云端、协作或外部连接器方案。

- Zoom AI Companion 将会中问题放在侧边面板，并为回答提供带时间戳的转录引用：[Zoom Meetings AI Companion](https://library.zoom.com/zoom-workplace/artificial-intelligence/artificial-intelligence-bluepaper/ai-companion/ai-companion-features/zoom-meetings)。
- Otter 强调私密会话、当前视图上下文、可点击来源及会中/会后连续使用：[Otter AI Chat Overview](https://help.otter.ai/hc/en-us/articles/19682180167575-Otter-AI-Chat-Overview)。
- Granola 按会前、会中、会后和跨会议组织用例，并通过明确作用域控制上下文：[Granola Chat](https://docs.granola.ai/help-center/getting-more-from-your-notes/chatting-with-your-meetings)。
- Fireflies 使用建议问题、按任务组织的 AI Skills、复制和显式反馈降低使用门槛：[AskFred](https://guide.fireflies.ai/articles/6556345325-askfred-ask-fred-questions-from-your-meetings-in-fireflies-and-get-answers)。
- Microsoft Teams Copilot 提供私密会中问答、提示建议、会后继续追问和来源引用：[Copilot in Teams meetings](https://support.microsoft.com/en-us/teams/copilot/catch-up-on-meetings-with-microsoft-365-copilot-in-teams)。

由此得出的产品原则是：**首发竞争力来自私密、速度、证据和克制，而不是会话历史长度、模型模式数量或连接器数量。**

核心使用路径：

```text
用户开始会议
    → 可选填写本次目标/背景（仅保存在当前浏览器组件内存）
    → 点击快捷问题或输入问题
    → 通过连接私有的 Inner OS WebSocket 提交问题和临时背景
    → 服务端创建转录与证据映射的不可变上下文快照
    → 本地 LLM 生成结构化回答
    → 用户查看证据、复制答案
    → 用户按需保存这一条问答
```

## 3. 目标与非目标

### 3.1 目标

1. 录制期间支持文本问答，不抢占麦克风、不启用 TTS。
2. 让用户可以回顾会议事实、识别风险并生成现场回应。
3. 事实与模型推断清楚分层，事实回答可以定位到具体转录片段。
4. 用户补充的本次会议背景只影响当前问答，由前端内存持有，不落库、不进入服务端可变会话状态。
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
| 核心能力 | 契约覆盖事实回溯、决策辅助、发言辅助；首发默认开放 `fact / draft`，`analysis / mixed` 通过独立实验开关准入 |
| 能力层级 | 证据 → 推理 → 行动：先事实，再判断，最后草稿 |
| 上下文 | 当前会议已确认转录 + 当前请求携带的临时目标/议程/背景 |
| 临时背景保存 | 只保存在当前浏览器组件内存；随问答请求提交，刷新、清空或会议结束即丢弃 |
| 持久化 | 每条问答单独点击“保存”后才落库 |
| 持久化模型 | 独立 `MeetingInnerOSExchange` 模型和 `meeting_inner_os_exchanges` 表 |
| 可见范围 | 首版仅允许 loopback 模式启用；未保存问答只在发起连接可见，不进入全局会议广播 |
| 音频策略 | 不改变 `AudioHub`、会议麦克风租约或 EOF 冲刷链路 |
| 模型通道 | 本地 LM Studio 原生 `/api/v1/chat`，固定 `store: false`，不使用 response chain 或 integrations |
| 会议状态 | 仅 `recording` 状态接受新问答；`finalizing` 后停止提交 |
| 问答通道 | 独立 `/ws/v1/meetings/{meeting_id}/inner-os`，不复用全局会议事件广播 |
| 重点片段 | 当前会议已标记的 confirmed 片段可随请求作为检索优先信号，但不自动成为事实或扩大数据范围 |
| 模型仲裁 | 通过进程级 `LocalLLMWorkloadGate` 与标题、纪要任务共享有界推理资源 |

## 5. 与现有架构的关系

当前系统已经具备以下事实边界：`RuntimeModeCoordinator` 负责 assistant / meeting / idle 的运行模式和麦克风所有权；`MeetingSession` 负责会议生命周期和转录对账；PostgreSQL 保存已确认转录；`MeetingEventBroadcaster` 向前端发送实时会议事件；`MeetingSummaryService` 在会议结束后生成纪要。

当前实现也已经具备可直接复用的产品资产：[`MeetingRecordingView.tsx`](../../../ui/src/components/meeting/MeetingRecordingView.tsx) 提供重点标记、片段复制、时序/阅读视图和说话人命名入口；[`meetingStore.ts`](../../../ui/src/stores/meetingStore.ts) 按会议在浏览器本地保存重点片段 ID；[`MeetingDetailView.tsx`](../../../ui/src/components/meeting/MeetingDetailView.tsx) 已有历史双栏、证据定位、汇报/Checklist 复制与多格式导出；[`MeetingMinutesViewer.tsx`](../../../ui/src/components/meeting/MeetingMinutesViewer.tsx) 已按事实类型展示纪要证据。Inner OS 应以只读方式组合这些能力，不重新实现转录浏览器、重点系统或导出中心。

“内心 OS”作为会议域中的独立旁路能力接入，不改变上述所有权关系：

```text
AudioHub → MeetingSession / 转录网关 → PostgreSQL confirmed transcript
                                      ├→ MeetingSummaryService（会后纪要）
                                      └→ InnerOSContextProvider
                                          → 不可变快照 + 证据别名映射
                                          → InnerOSQueryService
                                          → LocalLLMWorkloadGate
                                          → LM Studio /api/v1/chat

InnerOSPanel ──连接私有 WS──► InnerOSQueryService ──同一连接──► 回答事件
      └──显式保存──HTTP PUT──► InnerOSRepository ────────────► PostgreSQL
```

组件边界：

- `InnerOSContextProvider`：读取会议已确认转录和 revision，将当前请求的临时背景与合法重点片段 ID 复制进不可变上下文快照，并生成短证据别名映射。
- `InnerOSQueryService`：校验会议状态、管理连接级单活动任务、处理取消、调用模型、校验结构化输出并向发起连接发布问答事件。
- `InnerOSAnswerContract`：定义事实、判断、草稿、证据和限制说明的稳定结构。
- `InnerOSRepository`：只负责已保存问答的新增、查询和删除。
- `MeetingInnerOSModelClient`：封装 Inner OS 提示与答案解析；复用经过验证的原生 SSE 传输基础设施，不复用 Pipecat、语音助手上下文或 TTS。
- `LocalLLMWorkloadGate`：在进程内为 Inner OS、标题和会后纪要提供单槽基线、有界等待和可取消的推理仲裁。优先级只作用于尚未进入 LM Studio 的请求，不强杀已接纳请求；会议录制期间暂停接纳新的后台纪要模型调用，已完成/失败的任务状态仍由原纪要 worker 管理。
- `InnerOSPrivateChannel`：为单个浏览器连接转发命令和回答事件，连接断开时取消其活动问答，绝不向其他会议订阅者扇出。

问答旁路不能写入 `MeetingSession` 的转录状态，也不能阻塞 ASR、音频采集、会议结束冲刷或纪要 worker 的生命周期。会议进入 `finalizing` 时，由服务器装配层的组合事件发布器先取消并等待活动 Inner OS 模型流关闭，再继续 EOF 冲刷；不得把 Inner OS 专有条件写入 `MeetingSession` 或 `RuntimeModeCoordinator`。

## 6. 运行时数据流

### 6.1 会议开始与临时背景

1. 会议进入 `recording` 后，前端显示“内心 OS · 仅你可见”。
2. “仅你可见”的承诺只在 loopback 单用户边界内成立；`VR_BIND_HOST=lan` 或 `0.0.0.0` 时服务端拒绝启用首版 Inner OS，并返回稳定配置错误。
3. 用户可以填写目标、议程和背景；内容只保存在当前 `InnerOSPanel` 组件内存，不写入 localStorage、Zustand persist、服务端会话或数据库。
4. 用户修改背景时由前端递增 `context_version`；提交问题时把规范化背景、版本和是否为空随请求发送，服务端只复制到该次不可变快照。
5. 刷新页面、会议结束或用户清空内容时，临时背景被清除；服务端不提供恢复接口。

### 6.2 提交与生成问答

1. 用户点击快捷问题或提交文本问题，前端生成 `request_id`，并通过连接私有的 Inner OS WebSocket 发送问题、`intent`、`context_version`、临时背景和可选的 `focus_segment_ids`。
2. 服务端在同一连接上严格校验字段、长度、会议 ID、intent 能力开关、重点片段归属、loopback 边界和 `recording` 状态；创建 `query_id` 后立即返回 `inner_os_query_accepted`，确认过程不得等待模型生成。
3. `InnerOSContextProvider` 在一次 Repository 读取中获得当前 confirmed 转录、speaker 映射、`transcript_revision` 和 `content_revision`，并拼出不可变快照。
4. 快照包含 `meeting_id`、两个 revision、`context_version`、问题、临时背景、合法重点片段 ID、被选中的 confirmed 片段，以及 `S0001` 形式的短证据别名映射；不包含 partial、其他会议或外部数据。
5. `InnerOSQueryService` 通过 `LocalLLMWorkloadGate` 调用本地 LM Studio。`fact` / `draft` 默认 `reasoning: "off"`，`analysis` / `mixed` 默认 `reasoning: "on"`；实际路由和预算均配置化并受本机门禁约束。
6. 原生请求固定 `stream: true`、`store: false`，不发送 `previous_response_id`、integrations、tools 或完整历史；只消费 `message.delta`，reasoning/tool 类型不得进入回答、日志或持久化。
7. 模型输出必须通过 `InnerOSAnswerContract` 校验；模型引用的每个证据别名必须存在于该快照的 `included_evidence`，不能只校验“当前会议中曾存在”。
8. 服务端把证据别名规范化为用户可见证据快照后，才发送 `inner_os_answer_completed`，默认 `saved: false`；首版不发送答案正文 delta。
9. 每个私有连接最多一个活动问答。新问题默认返回 `inner_os_busy`；只有显式 `inner_os_cancel` 成功后才能提交下一问，避免隐式取消造成用户误解。
10. 会议进入 `finalizing` 或私有连接断开时，服务端取消活动 task、关闭 LM Studio HTTP 流并等待资源释放；完成但未保存的回答保留到 TTL 到期，仍允许保存。

### 6.3 保存问答

1. 用户在某条完成的回答上点击“保存”。
2. 前端使用 `query_id` 作为最终 `exchange_id` 调用幂等 `PUT` 保存接口；同一个 UUID 贯穿临时问答与保存资源，不再引入第二套 ID 或 `Idempotency-Key`。
3. 服务端确认问答属于该会议、回答已经完成、证据 ID 有效后写入独立表。
4. 保存记录用户可见的问题、结构化结论和生成时的证据快照；不保存临时背景正文、完整 prompt、模型原始输出或隐藏思维链。
5. 首次保存返回 `201`；同一 `exchange_id` 重试返回 `200` 和同一条记录；如果路径会议 ID 与缓存归属不一致则返回 `404`，不泄露其他会议信息。

## 7. 上下文与长会议策略

### 7.1 可信上下文

- 正式上下文只使用会议 `confirmed` 转录；partial 仅用于实时字幕展示，不能成为事实证据。
- 所有转录内容都被视为不可信资料，提示明确禁止执行转录中出现的指令。
- 用户临时背景可影响回答，但不会被当作参会者发言，也不会出现在正式转录中。
- 不做外部联网检索，不把历史会议或其他文件加入首版上下文。
- Prompt 将系统指令、用户问题、临时背景和转录数据放入明确分隔的结构段；转录中的指令不能改变权限、数据源、输出结构或工具边界。

### 7.2 上下文裁剪

- 会议内容在配置的有限 token 预算内时，使用全部已确认转录，但仍为每段分配短证据别名。
- 超出预算时，固定保留最近时间窗口，并使用当前会议内的确定性轻量词法相关度、问题中显式说话人和时间边界，从更早 confirmed 片段中选择候选；首版不加载新的嵌入模型。
- `focus_segment_ids` 只接受当前会议中仍存在的 confirmed 片段，作为相关度加权和裁剪保留信号；无效、跨会议或 partial ID 在边界处拒绝，重点标记本身不证明片段内容正确，也不要求答案必须引用该片段。
- Prompt 布局为“高相关证据置前、最近时间窗口置后”，避免把最相关信息长期埋在超长上下文中间。
- 对无法找到充分证据的问题，回答必须说明证据不足，不能用模型常识补齐会议事实。
- `inner_os_context_length`、`inner_os_context_max_tokens`、`inner_os_max_question_chars`、`inner_os_max_ephemeral_context_chars` 和 `inner_os_max_output_tokens` 都必须是有限配置。`65536` 只作为 LM Studio `context_length` 的初始候选，不是已验收默认值；实现阶段必须用实际加载模型和真实中文会议基准确认。
- 快照记录 `total_confirmed_segments`、`included_segment_count`、`included_time_ranges` 和裁剪原因；若发生裁剪，答案必须包含稳定限制码 `context_truncated`。

每次问答使用不可变快照。生成过程中如果会议产生新转录，当前回答仍绑定快照中的 revision。读取已保存问答时分别计算：

- `context_advanced`：当前 `content_revision` 大于生成 revision，仅表示会议后来有新内容。
- `evidence_invalidated`：引用片段不存在，或其文本、时间、speaker 映射与保存的证据快照哈希不一致；只有这一状态表示原证据已变化。

不得把正常的会议继续进行统一渲染为 `stale`。

## 8. 回答契约

规范化答案的最小结构如下：

```json
{
  "intent": "mixed",
  "evidence": [
    {
      "segment_id": "segment-uuid",
      "start_ms": 12000,
      "end_ms": 16800,
      "speaker_key": "epoch-0:speaker-0",
      "speaker_name": "发言人 1",
      "text": "生成时实际提供给模型的证据文本",
      "content_hash": "sha256:..."
    }
  ],
  "facts": [
    {
      "text": "已确认的会议事实",
      "evidence_segment_ids": ["segment-uuid"]
    }
  ],
  "judgements": [
    {
      "text": "基于事实的风险或建议",
      "basis_segment_ids": ["segment-uuid"],
      "uncertainty": "medium",
      "uncertainty_reason": "会议中尚未明确负责人和验收条件"
    }
  ],
  "draft": {
    "text": "建议用户使用的回应"
  },
  "limitations": [
    {
      "code": "context_truncated",
      "message": "仅检索了当前会议的一部分早期内容"
    }
  ]
}
```

约束：

- `intent` 为 `fact / analysis / draft / mixed`；自由输入默认 `mixed`，快捷按钮提供显式 intent。
- 模型侧只返回 `S0001` 形式的 evidence key；服务端校验后映射为顶层去重的用户可见证据快照，并把事实/判断中的引用规范化为真实 `segment_id`。模型不能自行生成 segment UUID、时间或证据正文。
- `facts` 中每一项都必须至少引用一个本次快照实际包含的证据；不存在证据时返回 `facts: []` 和 `insufficient_evidence` limitation，不能生成一条无证据的“未找到事实”。
- `judgements` 必须标明是模型判断，并携带 `low / medium / high` 不确定性等级和用户可读的 `uncertainty_reason`。该等级描述证据缺口，不表示统计概率，也不得在没有校准集的情况下渲染为“置信度百分比”。
- `draft` 可以为 `null`；非空时仅是建议文本，不能自动发送或播报。
- `limitations` 是带稳定 `code` 的对象数组，用于声明转录缺失、证据不足、上下文裁剪或版本变化；用户文案可演进，code 保持稳定。
- 不保存或展示模型隐藏思维链，只保留用户可读的依据、结论和不确定性。
- 模型输出 JSON 失败时最多执行一次有限的结构修复；只有未触发 token/字符上限的语法或结构错误允许 repair。超时、取消、传输失败和输出触顶直接返回相应错误，不进入 repair。

## 9. 独立持久化模型

### 9.1 `meeting_inner_os_exchanges`

| 字段 | 类型 | 语义 |
|---|---|---|
| `id` | `uuid` | 问答与保存记录的统一主键；在 query accepted 时生成并作为 `query_id` 返回 |
| `meeting_id` | `uuid` | 所属会议，外键关联 `meetings.id` |
| `question` | `text` | 用户明确保存的问题 |
| `intent` | `text` | `fact / analysis / draft / mixed` |
| `answer_json` | `jsonb` | 经过校验的规范化答案与用户可见证据快照，唯一事实载荷 |
| `source_transcript_revision` | `bigint` | 生成时的会议 `transcript_revision` |
| `source_content_revision` | `bigint` | 生成时的会议 `content_revision`，覆盖转录和说话人名称变化 |
| `used_ephemeral_context` | `boolean` | 是否使用过本次会议临时背景，不保存背景正文 |
| `model` | `text` | 实际模型标识 |
| `reasoning` | `text` | 实际使用的 `off / on` 路由结果，不保存 reasoning 内容 |
| `prompt_version` | `text` | 回答提示版本 |
| `created_at` | `timestamptz` | 保存时间 |

约束与索引：

- 主键为 `id`；临时阶段称 `query_id`，持久化后称 `exchange_id`，两者值相同。数据库主键原子防止重复保存。
- 历史查询使用稳定 keyset cursor `(created_at, id)`，建立 `(meeting_id, created_at DESC, id DESC)` 复合索引。
- 删除会议时级联删除其内心 OS 记录；单条问答支持显式删除。
- `context_advanced` 和 `evidence_invalidated` 都不写入数据库，读取时分别根据当前 revision 与保存的证据快照计算。
- `answer_json` 是唯一事实载荷；首版不保存 `answer_markdown`，由前端基于稳定结构渲染，避免双写漂移。
- 不保存完整 prompt、临时背景、原始模型输出和隐藏思维链。
- migration 必须为 `intent`、revision、问题长度、模型和 prompt 版本增加必要的 `NOT NULL` / `CHECK` 约束；跨行唯一性由数据库约束保证，不依赖应用层先查后写。

### 9.2 临时问答

未点击保存的问答只存在服务端有界内存缓存中，并带有 `query_id`、状态、所属连接、所属 `meeting_id`、过期时间和估算字节数。缓存同时受最大条数、最大总字节数和 TTL 限制：

- 活动任务每个私有连接最多 1 条；连接断开或进入 `finalizing` 时取消。
- 已完成回答默认保留 30 分钟，会议结束后仍可在原页面显式保存，TTL 由配置限定。
- 淘汰优先删除最旧的已完成未保存回答，绝不淘汰活动任务来伪装成功。
- 服务重启或缓存过期后可以丢失；这不影响已保存记录和会议正式数据。

## 10. HTTP / WebSocket 契约

### 10.1 私有 Inner OS WebSocket

新增 `/ws/v1/meetings/{meeting_id}/inner-os`。它复用现有 WebSocket 的同源、Origin 和严格 JSON 校验策略，但每个连接拥有独立的回答事件队列，不注册到 `MeetingEventBroadcaster`。连接握手时服务端必须复核 loopback Host、功能开关、会议 ID 和会议状态。

查询命令：

```json
{
  "contract_version": "1",
  "request_id": "req_123",
  "cmd": "query",
  "meeting_id": "meeting-uuid",
  "question": "刚才关于交付时间的结论是什么？",
  "intent": "fact",
  "context_version": 3,
  "ephemeral_context": {
    "goal": "确认交付承诺",
    "agenda": "交付时间和依赖",
    "background": "客户希望本周拿到灰度版本"
  },
  "focus_segment_ids": ["segment-uuid"]
}
```

服务端在同一连接上返回：

```json
{
  "contract_version": "1",
  "type": "inner_os_query_accepted",
  "event_id": "event-uuid",
  "meeting_id": "meeting-uuid",
  "query_id": "query-uuid",
  "request_id": "req_123",
  "occurred_at": "2026-08-27T14:00:00Z",
  "payload": {
    "status": "accepted"
  }
}
```

取消命令为 `{"contract_version":"1","request_id":"...","cmd":"cancel","query_id":"..."}`。取消是幂等操作：活动任务返回 `cancelled`，已经终态的任务返回其当前状态。

### 10.2 私有回答事件

通过同一私有 Inner OS WebSocket 推送以下事件：

- `inner_os_answer_started`：包含 `meeting_id`、`query_id`、`intent`、`transcript_revision` 和 `content_revision`。
- `inner_os_answer_completed`：包含完整、已校验的回答结构、证据快照、`transcript_revision`、`content_revision`、`context_advanced` 和 `saved: false`。
- `inner_os_answer_failed`：包含稳定错误码和用户可读消息。
- `inner_os_answer_cancelled`：包含取消原因 `user_cancelled / connection_closed / meeting_finalizing`。

所有服务端事件统一使用 `contract_version / type / event_id / meeting_id / query_id / occurred_at / payload` envelope；命令直接响应额外携带 `request_id`，异步后续事件不重复携带。失败事件的 `payload.error` 沿用项目稳定结构 `{code, message, details}`，不得暴露异常类型、prompt 或模型原始输出。

首版不发送正文 delta；完成事件中的结构化答案是唯一权威答案。断线期间丢失且未保存的回答不承诺恢复，已保存记录始终通过 HTTP 回源。保存成功由 PUT 响应确认，不再通过全局会议事件广播。

### 10.3 保存与历史接口

```text
PUT    /api/v1/meetings/{meeting_id}/inner-os/exchanges/{exchange_id}
GET    /api/v1/meetings/{meeting_id}/inner-os/exchanges?cursor=&limit=
GET    /api/v1/meetings/{meeting_id}/inner-os/exchanges/{exchange_id}
DELETE /api/v1/meetings/{meeting_id}/inner-os/exchanges/{exchange_id}
```

保存接口要求路径 `exchange_id` 等于已完成临时问答的 `query_id`，以统一资源 ID 实现天然幂等；服务端只接受缓存中存在且已完成的问答，并返回该 canonical resource 的完整表示。历史接口返回 `context_advanced`、`evidence_invalidated` 和 `used_ephemeral_context`，但不返回未保存的背景正文。列表 `limit` 范围为 1–100，cursor 是不透明的 `(created_at, id)` 编码，排序固定为新到旧。单条 DELETE 为幂等删除：目标已经不存在时仍返回 `204`，但不得泄露其他会议是否存在同 ID 记录。

稳定错误码至少包括：

```text
inner_os_not_active
inner_os_intent_disabled
inner_os_busy
inner_os_context_unavailable
inner_os_invalid_focus_segment
inner_os_model_unavailable
inner_os_timeout
inner_os_cancelled
inner_os_output_limit
inner_os_invalid_answer
inner_os_not_found
inner_os_private_channel_required
```

`focus_segment_ids` 是可选、有限长度的附加字段；省略时语义与未选择重点片段完全一致。新通道和新资源属于 v1 的纯新增能力，不改变现有控制 WS 和会议事件语义；需要同步更新 OpenAPI、AsyncAPI、JSON Schema、fixtures 和契约变更记录，`contract_version` 保持 `"1"`。旧客户端不会收到未知 Inner OS 事件。

## 11. 前端交互规格

在现有 `MeetingRecordingView` 内增加可折叠的 `InnerOSPanel`：

- 左侧保持实时转录，右侧为“内心 OS · 仅你可见”面板。
- 顶部提供可选的本次会议目标/背景输入，并显示“不保存背景正文”的提示。
- 首发快捷问题优先展示“回顾刚才结论”“有哪些明确承诺”“帮我组织回应”；“找风险 / 生成追问”仅在 `analysis / mixed` 实验开关通过门禁后显示。
- 自由输入框允许用户改写快捷问题或提出任意会议相关问题。
- 事实卡中的证据可点击并定位左侧转录片段。
- 用户已标记的重点片段默认作为本次提问的检索优先信号，并允许在发送前一键取消；界面必须说明“重点仅影响检索优先级”。
- 判断卡显示“模型判断”“不确定性”和原因，不展示未经校准的置信度百分比。
- 草稿卡提供复制按钮，不提供发送或播报按钮。
- 每条完成的回答显示“保存”“复制”“重新提问”；保存成功后显示已保存状态。
- 问答状态为 `未提问 → 已接受 → 生成中 → 已完成 / 已取消 / 失败 → 已保存`；会议继续产生内容时显示“有新内容”，证据本身变化时才显示“原证据已变化”。
- 会议详情页新增独立“内心 OS”记录区域，不混入正式纪要。

前端实现必须建立独立 `ui/src/features/innerOS/` 边界，包含 `contracts`、`api`、`store`、`useInnerOSSocket` 和组件；不得把完整问答状态继续堆入已有 `meetingStore.ts`。Inner OS 只通过只读 selector 获取活动会议 ID、confirmed segments 和证据定位能力。

## 12. 故障与隐私处理

### 12.1 故障

- LM Studio 不可用或超时：当前问答失败，录制和转录继续。
- PostgreSQL 不可用：已完成的临时回答仍可展示，但保存失败并允许重试。
- 上下文读取失败：不生成无依据回答，返回 `inner_os_context_unavailable`。
- WebSocket 断开：未保存回答不保证恢复；保存成功的记录可通过 HTTP 重新加载。
- 服务重启：清空临时问答和背景，保留 PostgreSQL 中的已保存记录。
- 会议进入 `finalizing`：先取消并等待未完成模型流关闭，再继续 EOF 冲刷；取消超时只记录稳定诊断并继续会议封存，不能反向阻塞停止会议。
- 证据 ID 无效或结构校验失败：拒绝发布为完成答案，不写入持久化模型。
- LM Studio 输出 `reasoning`、`tool_call`、`invalid_tool_call` 或未知正文类型：不得转发；若没有合法 message 正文则返回 `inner_os_invalid_answer`。

### 12.2 隐私与日志

- 继续保持本机优先、默认离线和不保存音频的项目边界。
- 临时背景、完整 prompt、转录正文和模型原始输出不得写入普通日志。
- 保存问答是明确的用户操作；保存后仍只写入独立模型，不修改正式会议事实。
- 用户可单条删除已保存问答。
- 转录中的指令、链接和权限要求一律视为不可信数据，不改变系统授权。
- LM Studio 请求必须显式 `store: false`；不得依赖其默认值。`reasoning` 内容即使由模型生成也只能丢弃，不进入异常详情或调试日志。
- 首版功能开关在非 loopback 绑定下 fail-closed；同源校验只能降低跨站调用风险，不能替代用户身份与授权。

## 13. 测试与发布验收

### 13.1 后端测试

- `InnerOSContextProvider`：上下文快照不可变、只含当前会议、临时背景不落库、partial 不进入证据、重点片段 ID 归属与状态校验、证据别名只映射 included segments、裁剪元数据完整、相关证据置前且最近窗口置后。
- `InnerOSAnswerContract`：intent、可空 draft、结构化 limitation、证据别名映射、未知引用、空答案、证据快照及不确定性原因。
- `InnerOSQueryService`：会议状态与 intent 能力开关校验、连接级单活动问答、显式取消、断线取消、finalizing 取消、超时、模型失败、输出触顶和一次性结构修复。
- `LocalLLMWorkloadGate`：优先级、公平性、有界等待、取消释放、Inner OS 与标题/纪要不发生无界并发。
- 原生模型客户端：固定 `store: false`，只接受 message，reasoning/tool 不泄漏；输出触顶和取消不执行 repair。
- 私密性：Inner OS 事件不进入任意其他会议订阅者；非 loopback 启用失败；旧会议客户端不收到未知事件。
- 持久化：幂等 PUT、重复保存、路径归属校验、单条删除、稳定 keyset 分页、`context_advanced` 和 `evidence_invalidated` 独立计算及错误回滚。
- 契约：OpenAPI / AsyncAPI / JSON Schema / fixtures 一致，事件顺序、错误 envelope 和断线语义正确。
- 集成：问答失败不改变录制、转录、EOF 冲刷和纪要状态；测试数据库使用独立临时 schema，并在测试结束执行 `DROP SCHEMA ... CASCADE`。

### 13.2 前端测试

- 快捷问题按能力开关显示、文本提交、重点片段选择、生成中、失败、重试和复制。
- 事实证据点击定位转录片段。
- 单条保存、重复点击、保存失败重试、删除、“有新内容”和“原证据已变化”分别展示。
- WebSocket 重连后已保存记录回源，未保存答案按约定可丢失。
- 目标/背景输入在刷新或结束会议后清除，且不混入会议转录和纪要。
- 面板折叠、键盘操作、屏幕阅读器状态和窄屏布局。
- 所有异步状态通过 `role="status"` 或等价 polite live region 通知，不抢夺键盘焦点；复制、保存和取消均有可访问名称。

### 13.3 本机性能门禁

实现阶段必须针对当前实际加载模型分别测量事实回溯、风险分析和发言草稿：私有命令确认延迟、LM Studio TTFT、完整回答 wall-clock、输入/输出/reasoning token、模型错误率、取消释放时间，以及问答期间 ASR 是否出现可归因的转录间隙。测量结果写入项目基准记录，并据此固化有限的上下文、输出和 wall-clock 配置。

初始候选门禁如下，正式值以同一基准流程回填并写入配置文档：

| 指标 | 候选门禁 |
|---|---|
| query accepted 延迟 | 本机 p95 ≤ 150 ms，且不等待模型 |
| fact / draft 完整回答 | p95 ≤ 10 s |
| analysis / mixed 完整回答 | p95 ≤ 30 s |
| finalizing / 断线取消释放 | p95 ≤ 500 ms；硬超时 2 s 后会议停止链继续 |
| 输出结构有效率 | 固定评测集一次生成成功率 ≥ 95%，repair 后 ≥ 99% |
| ASR 非劣 | 不新增 transcription gap，confirmed 延迟 p95 相对基线退化 ≤ 10% |
| 缓存 | 条数、总字节和 TTL 三项上限均可观测且测试覆盖 |

任何硬门禁失败时 `VR_MEETING_INNER_OS_ENABLED` 必须保持默认关闭；不得仅凭平均值或单次冒烟开启。

功能验收以以下结果为准：

1. 用户可在录制期间用文本或快捷按钮完成首发 `fact / draft`；启用独立实验开关时，`analysis / mixed` 也必须通过相同契约与证据验收。
2. 事实回答可定位到真实转录片段，判断与事实明确区分。
3. 用户可以只保存某一条问答，刷新后仍能查看，幂等 PUT 不会重复保存。
4. 未保存内容不出现在持久化数据中，临时背景不出现在数据库中。
5. 问答的任何失败都不会停止会议录制、转录或会后纪要。
6. 会议结束后未完成问答被取消，会议仍可正常封存。

### 13.4 产品指标与验证方法

首版是本机单用户产品，不引入外部遥测 SDK，不上传任何使用数据。产品判断采用“固定评测集 + 真实会议任务复盘 + 本地无内容聚合计数”三类证据，避免用生成次数等虚荣指标代替用户价值。

**北极星指标：有效回答率。** 分母为成功完成的回答；分子为用户执行复制、保存或明确标记“有用”的回答。同一回答只计一次。首版尚未提供持久反馈时，以复制/保存和受控复盘中的有用标记计算；反馈功能上线后再统一口径。

| 指标层级 | 指标 | 候选判断方式 |
|---|---|---|
| 激活 | 合格会议问答激活率 | 最近 20 场时长 ≥ 10 分钟且 confirmed 片段 ≥ 20 的会议中，至少完成 1 次问答的比例 |
| 价值 | 有效回答率 | 完成回答中发生复制、保存或“有用”反馈的比例；目标值在 P0 基线后冻结 |
| 效率 | 首次价值时间 | 从打开面板到第一次复制/保存/有用反馈的时间，不以 query accepted 代替 |
| 信任 | 事实证据有效率 | 固定评测集中，事实引用真实支持结论的比例；首发候选门禁 ≥ 90% |
| 诊断 | 证据打开率、重新提问率、取消率 | 用于发现信任或回答质量问题，不单独视为成功 |
| 可靠性 | 完成率、错误码分布、p95 完整回答时间 | 与 §13.3 技术门禁联合判断 |
| 护栏 | 隐私事件、越界引用、ASR 退化、自动外发次数 | 必须分别为 0、0、满足非劣门禁、0 |

本地聚合只记录 intent、终态、耗时桶和复制/保存等布尔动作，不记录问题、答案、临时背景、证据正文或完整 ID；默认不出机，并提供一键清除。任何要新增持久化产品分析表或外部遥测的提议都必须另行审查隐私边界，不能借本规格获得授权。

产品门禁使用滚动样本而非单次演示：P0 至少覆盖 3 类会议、30 个事实/草稿问题和 10 个证据不足问题；P1 内测至少完成 20 场合格会议复盘。样本不足时只报告观察值，不用百分比宣称产品已验证。

## 14. 发布与回退

- 新表和接口采用纯新增 migration；旧版本应用忽略新表，不需要破坏性降级。
- 通过 `VR_MEETING_INNER_OS_ENABLED` 进行总开关控制，默认关闭，完成本机并发与隐私验收后再开启。
- `VR_MEETING_INNER_OS_ANALYSIS_ENABLED` 独立控制 `analysis / mixed`，默认关闭；关闭时服务端必须返回 `inner_os_intent_disabled`，不能只依赖前端隐藏入口。
- 首版开关只有在 UI 和 API 实际绑定均为 loopback 时才可启用；检测到 LAN/全网卡绑定时 fail-closed。
- 关闭开关只停止新问答入口，不删除已经保存的内心 OS 记录。
- 不新增模型下载；沿用已验证的本地 LM Studio 模型和原生 API 通道，模型 ID、采样和预算均配置化。
- 回退时保留独立数据表，用户已保存的问答仍可由历史接口读取或显式删除。

## 15. 分阶段实施计划

每个阶段必须独立可验证、可提交、可回退；禁止把后端、契约、持久化和完整 UI 一次性堆入单个变更。

### Phase 0：契约冻结与基准夹具

- 新增 Inner OS OpenAPI / AsyncAPI / JSON Schema / fixtures 草案和契约校验测试。
- 固定 `intent`、可选 `focus_segment_ids`、答案结构、证据快照、错误码、私有 WS 顺序和幂等 PUT 语义。
- 建立不调用真实模型的 benchmark 输入集和结果记录格式，并完成 3 类会议、30 个事实/草稿问题和 10 个证据不足问题的产品基线。
- 验收：现有 v1 契约测试继续通过，新契约 fixtures 可独立验证，旧客户端不会收到 Inner OS 事件。

### Phase 1：私有通道、任务生命周期与 LM 仲裁

- 实现 `InnerOSPrivateChannel`、有界临时缓存、显式取消和 finalizing/断线取消。
- 实现单槽基线的 `LocalLLMWorkloadGate`，先使用 fake model client 验证有界等待、录制期间暂停后台纪要接纳和资源释放。
- 在服务器装配层组合会议事件发布与 Inner OS 生命周期观察，不修改 `MeetingSession` 业务状态机。
- 验收：隐私、取消、队列上限和“EOF 冲刷不被问答阻塞”集成测试通过。

### Phase 2：上下文、证据与真实模型客户端

- 实现 `InnerOSContextProvider`、长会议裁剪、证据别名和证据快照映射。
- 抽取或复用经过验证的 LM Studio 原生 SSE 传输基础设施，实现 `store: false`、message-only、有限输出和一次 repair。
- 验收：事实必须有 included evidence；reasoning/tool 不泄漏；真实模型冒烟不写入 LM Studio response chain。

### Phase 3：migration、Repository 与历史 API

- 新增 `0002` 纯新增 migration、独立 Repository 协议和 PostgreSQL 实现。
- 实现幂等 PUT、历史 keyset 分页、删除、`context_advanced` 与 `evidence_invalidated`。
- 验收：独立临时 schema 测试全绿，重复保存无重复行，证据变化测试能区分会议继续和证据失效。

### Phase 4：独立前端 feature

- 建立 `ui/src/features/innerOS/`，实现面板、私有 socket、独立 store、保存历史和证据定位。
- 只读接入现有 meeting store 的活动会议、confirmed segments、speaker 映射和重点片段，不扩大其职责；完成键盘、屏幕阅读器和窄屏测试。
- 验收：刷新清除背景、断线不误报成功、保存记录可回源、全局会议订阅者看不到私有回答。

### Phase 5：真实并发门禁与灰度启用

- 在当前实际加载模型上执行 fact / analysis / draft / mixed 基准，并记录当时 model ID、context、parallel、采样和运行态。
- 同时运行真实 ASR，比较问答前后 confirmed 延迟和 transcription gap。
- 技术门禁全绿后只在 loopback 开启 `fact / draft`；`analysis / mixed` 继续受独立实验开关控制，达到产品证据有效率和时延门禁后再开放。
- 保留一键关闭、已保存历史读取和不含内容的本地聚合指标清除入口。

## 16. 后续产品迭代建议路线

产品阶段与 §15 技术实施阶段是两套不同视角：技术阶段说明“如何安全交付首版”，产品阶段说明“何时值得扩展下一类用户价值”。推荐采用门禁驱动而不是按日期堆功能；上一阶段没有形成足够证据时，不启动下一阶段的范围扩张。

### P0：价值验证与信任基线（当前优先级：最高）

**目标**：确认用户在真实会议中最愿意使用的问题，而不是先证明模型能回答任意问题。

- 用产品评审、技术评审、需求澄清三类匿名会议样本建立固定评测集。
- 验证“回顾事实”“明确承诺”“组织回应”三条主任务；风险分析仅作离线评测。
- 记录事实证据有效性、完整回答时间、证据不足时是否正确拒答，以及用户是否愿意复制或保存。
- 明确回答卡信息密度：默认先给一句结论和关键证据，事实、判断、草稿按需展开，避免用户在会议中阅读长文。

**进入 P1 的门禁**：事实证据有效率候选值 ≥ 90%；证据不足问题不得编造事实；`fact / draft` 性能和 ASR 非劣门禁通过；受控试用者能够在不解释功能的情况下完成首次提问、查看证据和复制答案。

### P1：私密证据问答 MVP（当前建议交付范围）

**目标**：建立“会中需要确认或回应时，打开 Inner OS”的稳定心智。

- 默认开放 `fact / draft`；`analysis / mixed` 仅对评测开关可见。
- 提供 3 个高频快捷问题与自由输入，不显示模板市场、模型选择器或复杂模式菜单。
- 自动带入当前会议 speaker 映射和用户重点片段；重点只提高检索优先级。
- 回答默认简洁，证据可定位；支持取消、复制、保存和重新提问。
- 未保存问答不恢复、不形成对话历史，明确表达这一隐私取舍。

**进入 P1.1 的门禁**：完成至少 20 场合格会议复盘；有效回答率候选值 ≥ 40%；除用户取消外的回答完成率 ≥ 95%；无隐私越界、跨会议引用或 ASR 回退；主要失败能够归因到稳定错误码。

### P1.1：信任反馈与会后连续使用

**目标**：让用户能纠正系统，并把一次性的会中价值延续到会议复盘。

- 增加“有用 / 不准确 / 缺少证据 / 太慢 / 太长”反馈；默认只保存标签和 exchange ID，不保存额外自由文本。
- 在 `MeetingDetailView` 的现有转录/纪要双栏中加入独立 Inner OS 区域，支持对已封存会议继续提问。
- 支持从已保存回答继续追问，但每个 follow-up 必须显式显示其上下文范围；不使用隐式跨会议记忆。
- 增加“回答截至哪个时间点”和“生成后会议有新内容”的可见提示。
- 支持把保存的草稿或事实复制进现有汇报/Checklist 工作流，但不自动修改正式纪要。

**进入 P1.2 的门禁**：反馈样本中“不准确 + 缺少证据”比例持续低于冻结阈值；会后问答形成可观察复用；上下文范围在可用性测试中无误解；删除和隐私清理链路通过验收。

### P1.2：场景模板与轻量个性化

**目标**：从通用输入框升级为面向具体工作的会议工具，但继续只使用当前会议数据。

- 首批只提供产品评审、技术评审、1:1、客户沟通四类本地模板；模板定义快捷问题和输出格式，不改变权限。
- 允许用户保存本地“提问配方”，明确展示会提交哪些临时背景；配方与会议转录分开存储并可单独删除。
- 根据会议阶段和已有内容推荐问题，例如“尚未明确负责人”“存在两个交付时间”，但推荐本身不触发 LLM。
- 开放通过门禁的 `analysis / mixed`，并继续保留不确定性原因和事实引用。

**进入 P2 的门禁**：至少一个模板相对通用入口显著提高有效回答率或缩短首次价值时间；推荐问题点击后有效率高于自由输入基线；模板没有引入新的数据越界。

### P2：受控的主动副驾驶

**目标**：在不打断会议的前提下，帮助用户发现可能错过的未决项。

- 仅在用户显式开启后显示私密提示卡；默认关闭，并可在会议中随时暂停。
- 优先用确定性规则识别候选信号，例如承诺缺负责人、时间冲突、问题长期未回应；只有用户点击“分析”才调用 LLM。
- 每张提示必须绑定 confirmed 证据、显示触发原因并支持“不再提示此类内容”。
- 严格限频，不播报、不抢焦点、不自动保存、不自动执行外部动作。

**进入 P3 的门禁**：主动提示的采纳率显著高于打扰/关闭率；误报不会造成决策误导；实时 ASR 和 LM 资源门禁仍满足；用户能够理解提示是模型建议而非会议结论。

### P3：本地会议知识与可审查工作流

**目标**：让用户在明确选择的数据范围内复用历史会议价值。

- 从“用户显式选择的若干会议”开始多会议分析，不默认搜索全部历史。
- 每个答案展示所用会议、片段和时间，允许逐个移除数据源；跨会议回答沿用证据快照。
- 在本地权限、加密、删除、索引重建和资源预算完成专项设计后，再评估本地向量检索。
- 外部文档、任务或消息连接器必须独立授权，并采用“生成草稿 → 用户复核 → 明确执行”流程。
- 多用户共享、LAN 访问和团队权限必须先完成身份认证与资源级授权，不与单用户首版共用“仅你可见”承诺。

### 不建议进入近期路线的能力

- **语音提问与 TTS 回答**：会与会议麦克风、回声抑制和注意力竞争，当前收益不足以覆盖复杂度。
- **模型选择器**：首版应由系统根据 intent 路由；暴露模型名称会增加决策负担且不能直接提升结果。
- **自动发送或自动改纪要**：破坏用户最终控制权，必须等到外部行动审计和回滚能力成熟。
- **默认跨会议记忆**：在身份、权限、数据选择和删除机制完成前，不应以“更聪明”为由扩大范围。

路线优先级总结：**先证明 `fact / draft` 有用且可信，再做会后连续与反馈；随后验证模板化；主动建议和跨会议知识必须分别经过新的隐私与资源审查。**

## 17. 明确拒绝的方案

- **复用 `/ws/v1/meetings` 广播回答**：无法兑现连接私密性，也会让旧客户端观察到新事件。
- **在服务端维护可变临时背景**：需要额外更新、清理、重连和并发契约，收益不足；首版由前端随请求携带。
- **为临时问答、保存记录和 Idempotency-Key 分配三套身份**：会形成冲突的幂等语义；首版让 `query_id == exchange_id`，通过 canonical PUT 资源路径保存。
- **流式展示未校验 JSON 正文**：可能显示残缺结构或泄漏 reasoning；首版只发布终态结构。
- **用整个 `content_revision` 作为 stale**：会议继续进行会使所有答案立即过期；改为上下文推进与证据失效双状态。
- **首版新增向量模型或跨会议 RAG**：扩大资源与隐私边界，不属于当前会议内被动问答的最小实现。
