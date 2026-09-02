---
title: "ADR-007：AI 会议纪要采用有界分段生成与服务端事件收敛"
description: "为会议纪要与标题生成建立多层超时、字符熔断与 output_limit 边界，防止长文本无限循环"
status: accepted
type: decision_record
category: meeting
date: 2026-08-26
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "sona-meeting"
tags:
  - adr
  - meeting-summary
  - lm-studio
  - bounded-generation
  - timeout-budget
  - title-generation
scope:
  - "sona.meeting"
related_documents:
  - "docs/manuals/会议助手后端运行与前后端联调.md"
  - "docs/decisions/0002-lm-studio-stateful-chat-context.md"
contracts:
  - "contracts/meeting-assistant/v1/"
---

# ADR-007：AI 会议纪要采用有界分段生成与服务端事件收敛

## 状态

Accepted

## 日期

2026-08-26

## 背景

长会议纪要曾出现模型持续输出约 5 万 tokens、近 50 分钟后才结束，最终结果仍因 JSON schema
无效而失败。事故暴露出多个边界同时缺失：HTTP read timeout 只限制“多久没有收到数据”，只要模型
持续输出就不会触发；map/reduce 没有输出 token 上限；模型直接复制长 UUID 证据，格式容易退化；
repair 会重新发送完整转写；数据库 lease 也不能主动取消仍在运行的 HTTP 流。

同一链路还会自动或手动生成会议标题。标题已写入 PostgreSQL 后，如果没有服务端事件，其他页面和
当前浏览器中的派生状态仍会显示旧标题，直到刷新或重新拉取。

## 决策

会议纪要采用“多层硬边界 + 失败可解释 + 服务端事实收敛”的生成策略：

1. 保持 LM Studio 原生 `POST /api/v1/chat`，显式使用 `reasoning: "off"`；不切回无法可靠关闭
   reasoning 的 OpenAI 兼容端点。
2. 将 HTTP read timeout、单次模型调用 wall-clock deadline、整条纪要任务 wall-clock deadline
   分开配置；任一 deadline 到期都取消流并持久化稳定错误码 `summary_timeout`。
3. map、reduce/repair、标题分别设置 `max_output_tokens`，客户端同时统计输出字符数；超过字符上限
   立即取消并持久化 `output_limit`。
4. 长会议按字符数或时间跨度中先达到的边界分块，保留一个 segment overlap；map 结果经受限 reduce
   合并，避免把整场长会议交给单次无界生成。
5. 模型输入只使用 `S0001` 形式的短证据引用，应用校验后映射回真实 segment UUID。最终领域模型继续
   保存 UUID，未知引用一律拒绝。
6. 首次输出无效时最多执行一次定向 repair；repair 只发送失败 JSON、允许的短引用和 schema，不重复
   发送完整转写。再次失败则终止，不循环修复。
7. 同一会议只允许一个 `queued/generating` 纪要任务；HTTP 请求携带 `Idempotency-Key`，服务端在会议行锁
   内去重，终态任务不阻止后续显式生成新版本。
8. LM Studio `chat.end.stats` 只保留阶段、token 数、速度和 TTFT 等脱敏指标，不记录 prompt、转写或模型
   正文；终态 `minutes_state_changed` 可携带 additive 的 `generation_stats`。
9. PostgreSQL 仍是标题事实源。手动改标题、AI 标题和纪要自动标题持久化成功后统一发布
   `meeting_title_updated`，前端原子更新当前会议、活动会议和历史列表；AI 标题操作只发起一次写请求。

2026-08-26 的 `v2-bounded` 长会议复测表明，最终 repair 在 4096-token 边界生成 4095 tokens 后被截断，
并在只完成 topics 时产生第 13 个条目。为让最终 JSON 有足够闭合预算且不恢复无界生成，默认
reduce/repair 上限调整为 10240 tokens、字符熔断调整为 65536；map 仍保持 2048。最终 reduce/repair 的模型侧
契约同时收紧为最多 8 个主题、8 个决策、8 个行动项、4 个风险、4 个开放问题和 6 个亮点，并限制概览与单项
长度。map 中间结果则允许使用领域模型容量（主题/决策/行动项/亮点最多 12 个，风险/开放问题最多 8 个），
避免把一个合法分块在进入 reduce 前按最终结果上限误拒绝。若 `chat.end.stats.total_output_tokens` 已触顶且
结构仍无效，任务直接以 `output_limit` 失败，不再用同一预算重复生成。

### Map 与 Reduce 的契约边界

两层契约分别服务于不同目标，不能用最终 reduce 的集合上限提前校验 map：

```text
confirmed segments
        │
        ▼
按 segment 边界切块（20,000 字符或 20 分钟，overlap=1）
        │
        ├─ MAP-1 ─┐
        ├─ MAP-2 ─┼─► 应用解析 S0001… → 真实 UUID → REDUCE → 最终 MinutesResult
        └─ MAP-N ─┘
```

- **领域/对外契约**：`contracts/meeting-assistant/v1/schemas/minutes-content.schema.json` 允许
  `topics/decisions/action_items/risks/open_questions/highlights` 为 `12/12/12/8/8/12`，用于表达
  最终可持久化的领域容量。
- **MAP 模型契约**：沿用上述集合容量，并保持标题、概览、单项字段和 evidence 字段的长度约束；每个分块
  使用本地的 `S0001` 短引用，避免在中间结果阶段丢弃独立事实。
- **REDUCE/REPAIR 模型契约**：收紧为 `8/8/8/4/4/6`，并使用 `10240` token 预算；reduce 只接收已经
  通过 evidence 校验的 map JSON，不再次接收全量转录。

应用负责把每个分块的短引用解析为真实 segment UUID，并在最终落库前再次校验引用归属。短会议只有一次
map 调用；长会议才执行多个 map 调用后再执行一次 reduce。各阶段的 repair 使用对应契约，最多执行一次。

### 2026-08-27 故障复盘

最新会议第一次生成时，模型返回的是合法 JSON，但 map 中间结果包含 9 个主题和 7 个亮点；旧版将最终
reduce/repair 上限（主题 8、亮点 6）提前用于 map，因此在 reduce 之前以 `invalid_schema` 失败。短引用形态、
证据解析结果和数据库状态均排除了 ASR 封存、evidence 映射及 PostgreSQL 持久化故障。修复后以
`v4-map-domain-10240` 分离两层契约，并加入“map 结果超过最终上限但未超过领域上限”的回归测试。

## 备选方案

### 只增大 HTTP timeout

无法阻止持续有数据的退化输出，还会延长资源占用和错误反馈时间。拒绝。

### 只依赖数据库 lease

lease 适合任务认领和崩溃恢复，不会自动取消进程内仍活跃的 HTTP 流。拒绝。

### 切换到 OpenAI 兼容端点并使用 JSON Schema structured output

该端点可提供受约束 JSON，但当前本地模型链路无法通过它可靠关闭 reasoning，与项目实测约束冲突。
保留为未来在模型/runtime 能力重新验收后的候选，不用于本次修复。

### 仅更换模型

历史上不同模型均出现过 schema 失败；模型替换不能代替资源和协议边界。后续可以独立基准测试模型，
但不作为正确性保障。

## 后果

### 正向后果

- 退化输出有明确的时间、token 和字符上界，不再无限占用本地推理队列；
- 92 分钟级会议会被拆成多个可观测、可重试的有限调用；
- schema/证据错误最多消耗一次定向修复，不会重复发送敏感转写；
- 标题写入后所有在线页面通过同一事件收敛，无需刷新；
- 错误码、统计和 prompt 版本可以区分旧事故与新策略生成。

### 负向后果

- 极端复杂会议可能在默认 token 上限内无法完整表达，需要通过评测调整限额；
- 分块 reduce 增加一次或多次模型调用，必须在整条任务 deadline 内完成；
- 原生 `/api/v1/chat` 当前不能直接使用 OpenAI 兼容端点的 JSON Schema 约束，仍需应用层校验与一次 repair。

### 保留风险

- 模型质量、硬件负载和 prompt 注入式转写内容仍可能导致单次输出无效，但不会突破资源边界；
- 请求取消依赖 HTTP 连接关闭传播到 LM Studio，发布验收必须检查模型回到 `idle` 且队列清空；
- 默认阈值基于当前本机模型与历史纪要分布，换模型或硬件后需要重新基准测试。

## 实施约束

- `summary_timeout_secs` 只能表示流式 read inactivity timeout，禁止再次作为总时限使用；
- `summary_request_timeout_secs` 和 `summary_job_timeout_secs` 必须使用可取消的 wall-clock deadline；
- 所有 map/reduce/repair/title payload 必须显式传 `max_output_tokens`；
- 最终 evidence 只能包含本次转写中存在的 UUID；
- repair 不得携带完整转写；不得记录 prompt、转写和原始正文到统计日志；
- `meeting_title_updated` 是 additive durable event，断线时仍以 HTTP/`meeting_snapshot` 回源；
- 当前 prompt 版本为 `v4-map-domain-10240`；后续策略变化需继续更新版本并保留旧数据可读。

## 关联文档

- [`docs/manuals/会议助手后端运行与前后端联调.md`](../manuals/会议助手后端运行与前后端联调.md)
- [`contracts/meeting-assistant/v1/asyncapi.yaml`](../../contracts/meeting-assistant/v1/asyncapi.yaml)
- [`contracts/meeting-assistant/CHANGELOG.md`](../../contracts/meeting-assistant/CHANGELOG.md)
- [ADR-002：LM Studio 交互上下文采用原生有状态会话链](./0002-lm-studio-stateful-chat-context.md)
