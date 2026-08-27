---
title: "会议助手『内心 OS』P0–P1 详细实施计划"
description: "从价值与可信度验证到本地私有问答闭环的契约、后端、前端、持久化、评测与发布任务清单"
status: draft
type: execution_plan
category: meeting
version: "v1.0.0"
date: 2026-08-27
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-core"
tags:
  - execution-plan
  - meeting-assistant
  - inner-os
  - local-ai
  - privacy
scope:
  - "voice_realtime.meeting.inner_os"
  - "voice_realtime.meeting.summary"
  - "voice_realtime.ui"
  - "ui.features.innerOS"
related_documents:
  - "docs/superpowers/specs/2026-08-27-meeting-inner-os-design.md"
  - "docs/architecture/系统总体架构与详细设计方案.md"
  - "docs/decisions/0007-bounded-meeting-summary-generation.md"
contracts:
  - "contracts/meeting-assistant/v1/"
---

# 会议助手“内心 OS”P0–P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先用可复现的 P0 评测证明“基于当前会议事实生成有证据答案/草稿”具备产品价值与可信度，再交付默认关闭、仅限 loopback、连接私有、可取消、可保存的 P1 最小闭环。

**Architecture:** `InnerOSPanel` 只提交当前问题、可选临时背景和星标片段 ID；后端在请求开始时冻结当前会议 confirmed transcript 快照，经确定性裁剪和证据别名映射后调用 LM Studio 原生 `/api/v1/chat`。`InnerOSQueryService` 负责单连接单任务、取消、短期缓存与结构校验，`LocalLLMWorkloadGate` 在模型调用前统一仲裁实时问答和后台纪要；完成结果仅经专用 WebSocket 返回本连接，用户显式保存后才写 PostgreSQL。

**Tech Stack:** Python 3.12、FastAPI/WebSocket、Pydantic、psycopg/PostgreSQL、httpx、LM Studio Native Chat API、React 19、TypeScript、Zustand、Vite 7、pytest、Vitest、OpenAPI 3.1、AsyncAPI 3.0、JSON Schema 2020-12。

**Spec:** `docs/superpowers/specs/2026-08-27-meeting-inner-os-design.md`

## Global Constraints

- P0 是 P1 的进入门：完成三类会议、40 个问题的盲评并形成 `Go / Revise / Stop` 结论前，不得把 P1 功能开关默认值改为 `true`。
- `VR_MEETING_INNER_OS_ENABLED=false` 与 `VR_MEETING_INNER_OS_ANALYSIS_ENABLED=false` 是发布默认值；P1 默认只开放 `fact`、`draft`。
- 仅处理当前会议的 confirmed transcript；不得读取 partial、其他会议、历史问答、会议音频、全局助手上下文或 `previous_response_id`。
- 临时目标、议程、背景只存在 `InnerOSPanel` 组件内存，禁止进入 `localStorage`、Zustand 持久化、服务端缓存字段、日志和 PostgreSQL。
- 专用 `/ws/v1/meetings/{meeting_id}/inner-os` 连接必须 fail-closed 校验 loopback；Inner OS 事件不得进入 `MeetingEventBroadcaster`。
- 每个 WebSocket 连接最多一个活动查询；P1 不发送答案 token delta。取消、断连、会议 `finalizing` 都必须终止活动任务，取消等待硬上限为 2 秒。
- 模型只看到请求快照中的证据别名（如 `S0001`）；模型输出不得携带数据库 UUID、原文、时间戳或内部 prompt。服务端负责别名验证和 canonical evidence 回填。
- `reasoning`、系统 prompt、临时背景、原始模型输出、工具输出和 Chain-of-Thought 均不得持久化或记录到普通日志；日志只保留耗时、状态、计数、裁剪信息和短 ID。
- 用户显式保存前，答案只在有界进程内缓存和前端内存中存在；保存后 PostgreSQL 只记录规范化问答、证据快照、版本和生成元数据。
- 现有 PostgreSQL 事实源、EOF 冲刷、recovery journal、单麦克风 owner、字幕路径和会议状态机语义保持不变。
- 所有实现任务先写失败测试，再做最小实现；每个任务先运行聚焦测试，最终运行项目全量质量门禁。

## Milestones and Stop/Go Gates

| 阶段 | 交付物 | 进入条件 | 退出条件 |
|---|---|---|---|
| P0-A | 脱敏/合成评测集、评分规则、运行器 | 规格已批准 | 数据集校验与指标单测通过 |
| P0-B | 3 类会议 × 40 问盲评报告 | 本地模型可用，P0-A 完成 | `Go / Revise / Stop` 结论签署 |
| P1-A | 契约、配置、上下文与模型安全边界 | P0 结论为 `Go`，或明确批准带风险继续 | 后端核心聚焦测试通过 |
| P1-B | 私有 WS、保存 API、前端闭环 | P1-A 完成 | 功能开关下端到端验收通过 |
| P1-C | 20 场候选发布验证 | P1-B 全量门禁通过 | 产品、隐私、ASR、可靠性门禁全部满足 |

## Planned File Structure

```text
src/voice_realtime/
├── benchmarks/inner_os/
│   ├── __init__.py
│   ├── dataset.py              # P0 数据集加载、脱敏与完整性校验
│   ├── metrics.py              # 盲评指标和 Go/Revise/Stop 判定
│   └── runner.py               # 本地模型评测 CLI，不写会议原文到报告
└── meeting/inner_os/
    ├── __init__.py
    ├── contracts.py            # 请求、事件、答案、证据和错误类型
    ├── context.py              # confirmed 快照、裁剪、别名和 hash
    ├── workload.py             # 进程内单槽模型工作负载仲裁
    ├── model_client.py         # LM Studio 原生流式调用与一次修复
    ├── cache.py                # 数量/字节/TTL 有界的未保存结果缓存
    ├── repository.py           # 已保存 exchange 的独立 PostgreSQL 仓储
    ├── service.py              # 查询状态机、取消、校验和生命周期联动
    ├── api.py                  # 保存、列表、详情、删除 REST API
    └── private_channel.py      # loopback-only 私有 WebSocket 会话

ui/src/features/innerOS/
├── contracts.ts               # 与公共契约一致的 TS 类型和运行时解析
├── api.ts                     # 保存、列表、详情、删除客户端
├── innerOSStore.ts            # 仅进程内查询/答案状态，不持有临时背景
├── useInnerOSSocket.ts        # 私有连接、命令、取消和重连策略
├── metrics.ts                 # 无内容、无完整 ID 的本地聚合指标
├── InnerOSPanel.tsx           # 问题、意图、临时上下文和快捷键
├── InnerOSAnswerCard.tsx      # 事实/判断/草稿/局限/证据展示
├── InnerOSUnsavedTray.tsx     # 结束会议后 TTL 内的保存入口
├── InnerOSPanel.css
└── index.ts
```

---

### Task 1（P0-A）: 固化价值验证数据集与人工盲评规则

**Files:**
- Create: `tests/fixtures/inner_os/product-review.json`
- Create: `tests/fixtures/inner_os/technical-review.json`
- Create: `tests/fixtures/inner_os/requirements-clarification.json`
- Create: `tests/fixtures/inner_os/questions.json`
- Create: `src/voice_realtime/benchmarks/inner_os/__init__.py`
- Create: `src/voice_realtime/benchmarks/inner_os/dataset.py`
- Create: `src/voice_realtime/benchmarks/inner_os/metrics.py`
- Create: `tests/benchmarks/test_inner_os_dataset.py`
- Create: `tests/benchmarks/test_inner_os_metrics.py`
- Create: `docs/benchmarks/inner-os/p0/README.md`

**Interfaces:**
- Consumes: 3 份不含真实个人信息的 confirmed transcript fixture。
- Produces: 30 个 `fact|draft` 问题、10 个“证据不足”问题、标准答案/必需证据别名、盲评表和确定性指标汇总。

- [ ] **Step 1: 先写数据集失败测试**

  ```python
  dataset = load_dataset(FIXTURE_ROOT)
  assert {case.meeting_type for case in dataset.meetings} == {
      "product_review", "technical_review", "requirements_clarification"
  }
  assert len(dataset.questions) == 40
  assert sum(q.expected_insufficient for q in dataset.questions) == 10
  assert all(q.meeting_id in dataset.meeting_ids for q in dataset.questions)
  assert_no_sensitive_fields(dataset)
  ```

  每类会议至少覆盖：事实回查、跨段归纳、决策/风险草稿、行动项草稿和证据不足；fixture 的 `segment_id` 使用固定测试 UUID，文本全部为合成或完成脱敏的内容。

- [ ] **Step 2: 运行数据集测试并确认 RED**

  Run: `uv run pytest tests/benchmarks/test_inner_os_dataset.py tests/benchmarks/test_inner_os_metrics.py -q`

  Expected: 因 loader、评分器和 fixture 尚不存在而失败，不得用跳过标记绕过。

- [ ] **Step 3: 实现强类型数据集与评分模型**

  定义 `EvaluationQuestion`、`ExpectedEvidence`、`HumanRating`、`EvaluationSummary`。每道题保存 `question_id`、`meeting_id`、`intent`、`question`、期望证据段 ID、是否应拒答；不保存模型输出到 fixture。

  指标公式固定为：

  ```text
  evidence_validity = 有效引用数 / 总引用数
  evidence_coverage = 命中必需证据的问题数 / 可回答问题数
  safe_insufficiency = 正确表达证据不足的问题数 / 10
  draft_usable = 无事实修正即可采用或仅需措辞修改的草稿数 / 草稿问题数
  effective_answer = 盲评 usefulness >= 4 且无无依据断言的回答数 / 已完成回答数
  ```

- [ ] **Step 4: 写清盲评与隐私协议**

  `docs/benchmarks/inner-os/p0/README.md` 必须规定：双人独立盲评、分歧仲裁、1–5 分 usefulness rubric、不得把真实原文/问题/回答写入 Git 报告、运行产物只保留聚合值和失败类别。

- [ ] **Step 5: 运行 P0-A 门禁**

  Run: `uv run pytest tests/benchmarks/test_inner_os_dataset.py tests/benchmarks/test_inner_os_metrics.py -q`

  Expected: 40 问分布、证据引用、脱敏字段和所有指标边界测试通过。

- [ ] **Step 6: 提交 P0 数据基线**

  ```bash
  git add src/voice_realtime/benchmarks/inner_os tests/fixtures/inner_os tests/benchmarks docs/benchmarks/inner-os/p0/README.md
  git commit -m "test(meeting): 建立内心 OS P0 价值评测基线"
  ```

### Task 2（P0-B）: 实现本地评测运行器并形成 Go/Revise/Stop 结论

**Files:**
- Create: `src/voice_realtime/benchmarks/inner_os/runner.py`
- Modify: `src/voice_realtime/benchmarks/inner_os/metrics.py`
- Create: `tests/benchmarks/test_inner_os_runner.py`
- Create: `docs/benchmarks/inner-os/p0/report.md`
- Create: `docs/benchmarks/inner-os/p0/summary.json`

**Interfaces:**
- Consumes: Task 1 数据集、LM Studio `/api/v1/chat`、评审者离线评分文件（运行目录外的本机临时文件）。
- Produces: 去内容化的聚合报告、各会议类型指标、失败类别计数与明确阶段结论。

- [ ] **Step 1: 写 runner 失败测试**

  使用 `httpx.MockTransport` 验证 runner 只向 `/api/v1/chat` 发送 `stream:true`、`store:false`、独立 `system_prompt`/`input`，只消费 `message.delta`，且报告不包含 transcript、问题、回答、UUID 或临时路径。

- [ ] **Step 2: 实现 dry-run 与真实运行两种模式**

  ```bash
  uv run python3 -m voice_realtime.benchmarks.inner_os.runner \
    --dataset tests/fixtures/inner_os \
    --output runtime/benchmarks/inner-os-p0 \
    --dry-run
  ```

  `--dry-run` 只校验数据、payload 与输出边界；真实模式要求本机 LM Studio 已可用，失败时返回非零退出码，不尝试下载或切换模型。

- [ ] **Step 3: 实现固定 P0 判定规则**

  - `Go`：引用有效率 `100%`、证据覆盖率 `>=90%`、证据不足安全表达率 `100%`、草稿可用率 `>=70%`、平均 usefulness `>=4.0/5`，且无跨会议引用、隐私泄漏和自信编造。
  - `Revise`：无隐私/跨会议事故，但一项质量指标未达 `Go`；先调整 prompt、裁剪或交互，再完整重跑 40 问。
  - `Stop`：出现任何跨会议数据、敏感信息落盘、无法阻断的自信编造，或两轮 `Revise` 后仍未达到 `Go`。

- [ ] **Step 4: 运行测试与 dry-run**

  Run: `uv run pytest tests/benchmarks/test_inner_os_runner.py tests/benchmarks/test_inner_os_metrics.py -q`

  Run: `uv run python3 -m voice_realtime.benchmarks.inner_os.runner --dataset tests/fixtures/inner_os --output runtime/benchmarks/inner-os-p0 --dry-run`

  Expected: 测试通过；dry-run 输出 3 类会议、40 问、0 条敏感内容写入报告。

- [ ] **Step 5: 在本地模型可用时执行真实盲评**

  先运行完整 40 问，再由两名评审者独立评分；分歧题完成仲裁后，将仅含聚合值的 `summary.json` 和结论写入 `docs/benchmarks/inner-os/p0/`。没有真实运行数据时不得把模板标记为 `completed`，也不得写虚构指标。

- [ ] **Step 6: 评审 P0 Gate**

  只有 `report.md` 明确记录 `Go`，才继续 Task 3–14。若为 `Revise`，回到 Task 1/2 修改并重跑；若为 `Stop`，保留报告并终止 P1 产品化。

- [ ] **Step 7: 提交 P0 结论**

  ```bash
  git add src/voice_realtime/benchmarks/inner_os tests/benchmarks docs/benchmarks/inner-os/p0
  git commit -m "docs(meeting): 归档内心 OS P0 价值验证结论"
  ```

### Task 3（P1-A）: 冻结公共契约、稳定错误码和配置开关

**Files:**
- Modify: `contracts/meeting-assistant/v1/openapi.json`
- Modify: `contracts/meeting-assistant/v1/asyncapi.yaml`
- Modify: `contracts/meeting-assistant/CHANGELOG.md`
- Create: `contracts/meeting-assistant/v1/schemas/inner-os-query-command.schema.json`
- Create: `contracts/meeting-assistant/v1/schemas/inner-os-cancel-command.schema.json`
- Create: `contracts/meeting-assistant/v1/schemas/inner-os-answer.schema.json`
- Create: `contracts/meeting-assistant/v1/schemas/inner-os-event.schema.json`
- Create: `contracts/meeting-assistant/v1/schemas/inner-os-exchange.schema.json`
- Create: `contracts/meeting-assistant/v1/fixtures/inner-os-completed.json`
- Create: `contracts/meeting-assistant/v1/fixtures/inner-os-insufficient.json`
- Create: `contracts/meeting-assistant/v1/fixtures/inner-os-invalid-focus.json`
- Modify: `contracts/meeting-assistant/v1/schemas/runtime-state.schema.json`
- Modify: `src/voice_realtime/config.py`
- Modify: `src/voice_realtime/ui/protocol.py`
- Modify: `src/voice_realtime/ui/runtime.py`
- Modify: `ui/src/protocol.ts`
- Modify: `tests/test_config.py`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_meeting_contracts.py`

**Interfaces:**
- Consumes: 已有 `contract_version: "1"` envelope 和统一 REST error envelope。
- Produces: P1 Query/Cancel 命令、五阶段事件、canonical answer、exchange REST 契约和 `VR_MEETING_INNER_OS_*` 设置。

- [ ] **Step 1: 先写契约与配置失败测试**

  测试以下默认值与约束：总开关/分析开关均为 false，limit 为 `1..100`，question 去空白后非空，`focus_segment_ids` 去重且有上限，`fact|draft` 默认允许，`analysis|mixed` 在分析开关关闭时返回 `inner_os_intent_disabled`。

- [ ] **Step 2: 固化 WS 生命周期与错误码**

  事件只允许 `accepted`、`started`、`completed`、`failed`、`cancelled`；公共错误码完整覆盖：

  ```text
  inner_os_not_active, inner_os_intent_disabled, inner_os_busy,
  inner_os_context_unavailable, inner_os_invalid_focus_segment,
  inner_os_model_unavailable, inner_os_timeout, inner_os_cancelled,
  inner_os_output_limit, inner_os_invalid_answer, inner_os_not_found,
  inner_os_private_channel_required
  ```

  `completed` 一次性携带完整答案，AsyncAPI 不定义 answer delta。

- [ ] **Step 3: 增加 MeetingSettings**

  使用以下确定默认值：缓存 TTL `1800s`、最大 `128` 条/`8MiB`、取消硬上限 `2s`、fact/draft 硬超时 `15s`、analysis/mixed `35s`、输出字符熔断 `65536`、上下文最大 `48000` 字符、最近窗口 `16000` 字符。环境变量由 `VR_MEETING_` 前缀映射为 `VR_MEETING_INNER_OS_*`。

- [ ] **Step 4: 暴露只读 runtime capability**

  在现有 `RuntimeStateSnapshot` 中新增向后兼容字段：

  ```json
  {
    "capabilities": {
      "inner_os_enabled": false,
      "inner_os_analysis_enabled": false,
      "inner_os_channel": "loopback_only"
    }
  }
  ```

  Python snapshot、JSON Schema 与 TypeScript interface 使用同一字段；这里只暴露能力，不暴露模型、prompt、缓存内容或临时背景。前端是否连接专用 WS 以此字段和当前页面 loopback 状态共同决定。

- [ ] **Step 5: 运行契约门禁**

  Run: `uv run pytest tests/test_config.py tests/test_runtime.py tests/test_meeting_contracts.py tests/test_meeting_contract_validator.py -q`

  Expected: schema、fixture、OpenAPI、AsyncAPI 和配置默认值全部一致；旧 fixture 继续有效。

- [ ] **Step 6: 提交契约基线**

  ```bash
  git add contracts/meeting-assistant src/voice_realtime/config.py src/voice_realtime/ui/protocol.py src/voice_realtime/ui/runtime.py ui/src/protocol.ts tests/test_config.py tests/test_runtime.py tests/test_meeting_contracts.py
  git commit -m "docs(contract): 固化内心 OS P1 私有问答契约"
  ```

### Task 4（P1-A）: 实现领域模型、confirmed 快照和确定性证据裁剪

**Files:**
- Create: `src/voice_realtime/meeting/inner_os/__init__.py`
- Create: `src/voice_realtime/meeting/inner_os/contracts.py`
- Create: `src/voice_realtime/meeting/inner_os/context.py`
- Create: `tests/test_inner_os_contracts.py`
- Create: `tests/test_inner_os_context.py`

**Interfaces:**
- Consumes: `PostgresMeetingRepository.get_meeting/get_transcript/get_speakers` 的当前会议 confirmed 数据。
- Produces: 不可变 `InnerOSContextSnapshot`、`S0001` 证据表、`content_hash`、裁剪元数据和规范化 `InnerOSAnswer`。

- [ ] **Step 1: 写答案模型失败测试**

  ```python
  answer = InnerOSAnswer.model_validate(payload)
  assert all(f.evidence_ids for f in answer.facts)
  assert all(j.uncertainty in {"low", "medium", "high"} for j in answer.judgements)
  assert all(j.uncertainty_reason.strip() for j in answer.judgements)
  ```

  拒绝 confidence 百分比、未知字段、事实无证据、判断 basis 不存在、模型返回 UUID/时间戳/原文等越权字段。

- [ ] **Step 2: 写快照与裁剪失败测试**

  覆盖：只含 confirmed、跨会议 focus 被拒绝、同会议不存在的 focus 被拒绝、全量在预算内不裁剪、超预算时“相关早期片段优先 + 最近窗口最后”、focus 只加权不强制纳入、相同输入重复选择完全一致、别名稳定且从 `S0001` 连续编号。

- [ ] **Step 3: 实现不可变上下文模型**

  `InnerOSContextSnapshot` 至少包含 `meeting_id`、`transcript_revision`、`content_revision`、`captured_at`、`evidence`、`total_segment_count`、`included_segment_count`、`cropped`、`selection_strategy`；`EvidenceSnapshot` 包含 segment ID、起止时间、speaker key/name、text 和 SHA-256 `content_hash`。

- [ ] **Step 4: 实现确定性选择器**

  先按 question token/字符相关度排序早期片段，再保留最近窗口，最终拼接顺序固定为“相关早期片段 → 最近窗口”；重叠去重，焦点片段只增加排序分，不突破会议/confirmed/字符预算边界。

- [ ] **Step 5: 运行上下文聚焦测试**

  Run: `uv run pytest tests/test_inner_os_contracts.py tests/test_inner_os_context.py -q`

  Expected: 所有快照、hash、别名、裁剪、引用回填和负向边界通过。

- [ ] **Step 6: 提交上下文边界**

  ```bash
  git add src/voice_realtime/meeting/inner_os tests/test_inner_os_contracts.py tests/test_inner_os_context.py
  git commit -m "feat(meeting): 建立内心 OS 快照与证据边界"
  ```

### Task 5（P1-A）: 建立共享 LocalLLMWorkloadGate 并接入后台纪要

**Files:**
- Create: `src/voice_realtime/meeting/inner_os/workload.py`
- Modify: `src/voice_realtime/meeting/summary.py`
- Create: `tests/test_inner_os_workload.py`
- Modify: `tests/test_meeting_summary.py`

**Interfaces:**
- Consumes: Inner OS 交互任务、`MeetingSummaryService` 后台生成任务、会议 recording 状态。
- Produces: 进程本地、单槽、有界、可取消、仅入场前排序的模型租约。

- [ ] **Step 1: 写并发和取消失败测试**

  覆盖：同一时刻最多一个模型调用；交互查询排在未入场后台纪要之前；已入场任务不被抢占；等待中的查询可取消且不泄漏 permit；recording 时暂停新后台纪要入场，但不强杀已入场任务；gate 关闭后所有 waiter 收敛。

- [ ] **Step 2: 定义最小租约接口**

  ```python
  async with gate.lease(
      workload=WorkloadKind.INNER_OS,
      priority=0,
      cancel_event=cancel_event,
  ):
      await call_model()
  ```

  `SUMMARY` 使用较低优先级；优先级只决定尚未 admission 的队列顺序，不实现运行中抢占。

- [ ] **Step 3: 把纪要模型调用包进共享 gate**

  在 `MeetingSummaryService` 进入 `MeetingSummaryClient.generate/reduce/repair_output/generate_title` 前申请后台租约；`requeue_for_recording` 继续承担录音状态切换，新增 `gate.pause_background()`/`resume_background()`，保持现有 output-limit 与一次 repair 语义。

- [ ] **Step 4: 运行仲裁回归测试**

  Run: `uv run pytest tests/test_inner_os_workload.py tests/test_meeting_summary.py -q`

  Expected: 单槽、优先级、暂停、取消测试通过；原纪要生成、map/reduce、repair 和 requeue 测试无回退。

- [ ] **Step 5: 提交工作负载仲裁**

  ```bash
  git add src/voice_realtime/meeting/inner_os/workload.py src/voice_realtime/meeting/summary.py tests/test_inner_os_workload.py tests/test_meeting_summary.py
  git commit -m "feat(meeting): 统一仲裁内心 OS 与后台纪要负载"
  ```

### Task 6（P1-A）: 实现 LM Studio 原生客户端、严格输出校验和一次修复

**Files:**
- Create: `src/voice_realtime/meeting/inner_os/model_client.py`
- Create: `tests/test_inner_os_model_client.py`

**Interfaces:**
- Consumes: Task 4 快照与证据别名、Task 5 模型租约、LM Studio `/api/v1/chat` SSE。
- Produces: 经 schema 和引用验证的 `InnerOSAnswer`；稳定映射模型不可用、超时、取消、输出上限和非法答案错误。

- [ ] **Step 1: 写 payload 与 SSE 失败测试**

  断言 payload 只含当前请求、`system_prompt`、`reasoning`、`stream:true`、`store:false`、`max_output_tokens` 等原生字段；不含 `messages`、`previous_response_id`、tools、integrations、音频、其他会议内容。只拼接 `message.delta`，忽略 reasoning/tool 事件。

- [ ] **Step 2: 固化两类 prompt 策略**

  - `fact|draft`：`reasoning:"off"`，要求 JSON、证据别名、明确 limitations。
  - `analysis|mixed`：仅当分析开关开启时 `reasoning:"on"`；仍不得返回或记录 reasoning 内容。

  模型输入的证据格式固定为 `[S0001][00:01.200-00:05.900][发言人] 文本`，输出只允许引用 `Sxxxx`。

- [ ] **Step 3: 实现字符熔断、超时和取消**

  超过 `65536` 字符立即关闭响应流并映射 `inner_os_output_limit`；请求超时映射 `inner_os_timeout`；用户取消/断连映射 `inner_os_cancelled`。任何路径都必须释放工作负载租约和 HTTP stream。

- [ ] **Step 4: 实现仅一次结构修复**

  只在 JSON 语法/结构或引用格式非法时发起一次独立 `store:false` 修复；timeout、cancel、output limit、model unavailable 不 repair。修复仍失败则返回 `inner_os_invalid_answer`，不循环调用。

- [ ] **Step 5: 运行模型边界测试**

  Run: `uv run pytest tests/test_inner_os_model_client.py -q`

  Expected: payload 白名单、SSE、错误映射、一次 repair、无 CoT 记录和资源释放测试全部通过。

- [ ] **Step 6: 提交模型客户端**

  ```bash
  git add src/voice_realtime/meeting/inner_os/model_client.py tests/test_inner_os_model_client.py
  git commit -m "feat(meeting): 实现内心 OS 本地模型安全调用"
  ```

### Task 7（P1-A）: 实现查询状态机、有界瞬时缓存和会议生命周期取消

**Files:**
- Create: `src/voice_realtime/meeting/inner_os/cache.py`
- Create: `src/voice_realtime/meeting/inner_os/service.py`
- Create: `tests/test_inner_os_cache.py`
- Create: `tests/test_inner_os_service.py`

**Interfaces:**
- Consumes: 当前 meeting/repository、Task 4 context builder、Task 6 model client。
- Produces: `accepted → started → completed|failed|cancelled` 状态机、单连接活动任务、30 分钟未保存结果缓存和 `cancel_meeting()` 生命周期入口。

- [ ] **Step 1: 写状态机失败测试**

  覆盖：一连接一活动查询；重复 query ID 幂等返回原状态；活动中发新 query 返回 busy；meeting 不是 active 返回 not_active；断连取消；finalizing 取消；正常完成只发一次 completed；异常路径只发一个终态；query ID 与 exchange ID 相同。

- [ ] **Step 2: 写缓存边界失败测试**

  覆盖 TTL、最大条数、最大序列化字节数、LRU/最早过期淘汰、显式删除、保存后移除、进程重启为空、缓存对象不含 prompt/临时背景/原始输出/reasoning。

- [ ] **Step 3: 实现连接级 QuerySession**

  `InnerOSConnectionSession` 持有 `connection_id`、`meeting_id`、`active_query` 和事件发送回调；服务全局只保存短期 query 状态，不通过 broadcaster 发布。接受后立即发 accepted，取得工作负载租约后发 started。

- [ ] **Step 4: 实现有界取消**

  `cancel_connection()` 和 `cancel_meeting()` 先置 cancel event、关闭 HTTP stream，再最多等待 2 秒；超时后解除会议停止流程的等待并记录无内容告警，不能阻塞 EOF/封存。

- [ ] **Step 5: 运行 service/cache 测试**

  Run: `uv run pytest tests/test_inner_os_cache.py tests/test_inner_os_service.py -q`

  Expected: 状态机、单终态、busy、断连/finalizing、TTL/字节边界与敏感字段检查通过。

- [ ] **Step 6: 提交查询服务**

  ```bash
  git add src/voice_realtime/meeting/inner_os/cache.py src/voice_realtime/meeting/inner_os/service.py tests/test_inner_os_cache.py tests/test_inner_os_service.py
  git commit -m "feat(meeting): 实现内心 OS 查询生命周期与瞬时缓存"
  ```

### Task 8（P1-B）: 增加已保存 exchange 迁移与独立仓储

**Files:**
- Create: `src/voice_realtime/meeting/migrations/0002_inner_os.sql`
- Create: `src/voice_realtime/meeting/inner_os/repository.py`
- Create: `tests/test_inner_os_repository.py`
- Modify: `tests/test_meeting_repository.py`

**Interfaces:**
- Consumes: Task 7 缓存中的 canonical completed answer。
- Produces: `meeting_inner_os_exchanges` 幂等保存、keyset 列表、详情、删除，以及读取时动态计算的证据状态。

- [ ] **Step 1: 写迁移与仓储失败测试**

  在独立临时 schema 中验证重复执行 migration 安全、外键隔离、meeting A 无法读取 meeting B、重复 PUT 同内容幂等、同 ID 不同内容冲突、删除不存在项仍返回成功、keyset 无重复/遗漏。

- [ ] **Step 2: 创建最小持久化表**

  表字段固定为：`id`、`meeting_id`、`question`、`intent`、`answer_json`、`source_transcript_revision`、`source_content_revision`、`used_ephemeral_context`、`model`、`reasoning`、`prompt_version`、`created_at`。主键 `id` 即 query/exchange ID；索引为 `(meeting_id, created_at DESC, id DESC)`。

  明确禁止列：prompt、临时目标/议程/背景、原始模型输出、CoT、音频、其他会议引用。

- [ ] **Step 3: 实现独立 PostgresInnerOSRepository**

  仓储使用同一 DSN/schema、独立小型 pool（`min_size=0,max_size=2`），由 app 生命周期统一 open/close；读取当前 transcript 修订和 evidence hash 时调用现有会议 repository，不复制会议事实表逻辑。

- [ ] **Step 4: 计算读取时状态而非持久化派生值**

  `context_advanced = current_revision > source_revision`；`evidence_invalidated` 通过当前同 ID segment 的 `content_hash` 与保存快照比较。缺失/变化返回结构化状态，但不改写历史 answer JSON。

- [ ] **Step 5: 运行 PostgreSQL 聚焦测试**

  Run: `VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/test_inner_os_repository.py tests/test_meeting_repository.py -q`

  Expected: 临时 schema 自动 `DROP ... CASCADE`；迁移、隔离、幂等、keyset、动态证据状态全部通过。

- [ ] **Step 6: 提交迁移与仓储**

  ```bash
  git add src/voice_realtime/meeting/migrations/0002_inner_os.sql src/voice_realtime/meeting/inner_os/repository.py tests/test_inner_os_repository.py tests/test_meeting_repository.py
  git commit -m "feat(meeting): 持久化用户保存的内心 OS 问答"
  ```

### Task 9（P1-B）: 实现保存/列表/详情/删除 REST API

**Files:**
- Create: `src/voice_realtime/meeting/inner_os/api.py`
- Modify: `src/voice_realtime/meeting/api.py`
- Create: `tests/test_inner_os_api.py`
- Modify: `tests/test_meeting_api.py`

**Interfaces:**
- Produces:
  - `PUT /api/v1/meetings/{meeting_id}/inner-os/exchanges/{exchange_id}`
  - `GET /api/v1/meetings/{meeting_id}/inner-os/exchanges?cursor=&limit=`
  - `GET /api/v1/meetings/{meeting_id}/inner-os/exchanges/{exchange_id}`
  - `DELETE /api/v1/meetings/{meeting_id}/inner-os/exchanges/{exchange_id}`

- [ ] **Step 1: 写 API 失败测试**

  覆盖：总开关关闭、缓存 miss、meeting mismatch、非 completed query、TTL 内 meeting 已结束仍可保存、幂等 PUT、limit `1..100`、非法/篡改 cursor、跨会议读取、详情动态状态、删除幂等 204、统一 error envelope。

- [ ] **Step 2: 固化保存语义**

  PUT 不接收 question/answer body，只以 path 中的 meeting/exchange ID 从服务端缓存取 canonical result，杜绝客户端改写模型答案；保存成功后从 transient cache 移除。

- [ ] **Step 3: 实现稳定 keyset cursor**

  cursor 编码 `(created_at,id)` 并做严格解析；排序固定为 `created_at DESC,id DESC`，返回 `next_cursor`。不存在详情使用 `inner_os_not_found`，删除不存在仍为 204。

- [ ] **Step 4: 运行 API 契约测试**

  Run: `uv run pytest tests/test_inner_os_api.py tests/test_meeting_api.py tests/test_meeting_contracts.py -q`

  Expected: OpenAPI 响应、错误 envelope、保存边界、分页和证据状态与契约一致。

- [ ] **Step 5: 提交 REST API**

  ```bash
  git add src/voice_realtime/meeting/inner_os/api.py src/voice_realtime/meeting/api.py tests/test_inner_os_api.py tests/test_meeting_api.py
  git commit -m "feat(meeting): 提供内心 OS 已保存问答接口"
  ```

### Task 10（P1-B）: 实现 loopback-only 连接私有 WebSocket

**Files:**
- Create: `src/voice_realtime/meeting/inner_os/private_channel.py`
- Modify: `src/voice_realtime/ui/server.py`
- Create: `tests/test_inner_os_websocket.py`
- Modify: `tests/test_ui_server.py`

**Interfaces:**
- Consumes: Task 3 WS schema、Task 7 QueryService。
- Produces: `/ws/v1/meetings/{meeting_id}/inner-os` 私有命令/事件通道；任何事件只返回发起连接。

- [ ] **Step 1: 写 fail-closed 安全测试**

  覆盖 IPv4/IPv6 loopback 成功；LAN/公网 client、非 loopback Host、非 loopback Origin、缺失或格式错误 Origin、伪造 `X-Forwarded-For/Host` 均拒绝；同源但非 loopback 仍拒绝；总开关关闭拒绝；普通 `/ws/v1/meetings` 客户端永远收不到 Inner OS 事件。

- [ ] **Step 2: 写连接状态测试**

  覆盖 accepted/started/completed 顺序、busy、cancel、重复 cancel、malformed command、分析意图关闭、focus 越界、meeting 不 active、断连清理和单终态。

- [ ] **Step 3: 实现专用授权函数**

  新增 `_allow_private_inner_os_websocket()`，只信任 ASGI socket 的 client/server 地址和解析后的 loopback Origin，不信任代理头；它独立于现有宽泛 `_allow_websocket()`，任一信息不确定即关闭 1008 并使用 `inner_os_private_channel_required`。

- [ ] **Step 4: 实现连接私有发送循环**

  每次 accept 创建一个 `InnerOSConnectionSession`；receive loop 只接收 query/cancel；事件直接 `websocket.send_json()`。finally 调用 `cancel_connection()`，不得注册到 `MeetingEventBroadcaster`。

- [ ] **Step 5: 运行 WS 与全局广播回归测试**

  Run: `uv run pytest tests/test_inner_os_websocket.py tests/test_ui_server.py tests/test_meeting_events.py -q`

  Expected: 私有性矩阵、loopback fail-closed、生命周期和原全局会议事件通道全部通过。

- [ ] **Step 6: 提交私有通道**

  ```bash
  git add src/voice_realtime/meeting/inner_os/private_channel.py src/voice_realtime/ui/server.py tests/test_inner_os_websocket.py tests/test_ui_server.py
  git commit -m "feat(meeting): 增加内心 OS 本机私有查询通道"
  ```

### Task 11（P1-B）: 完成服务装配、finalizing 联动和资源关闭

**Files:**
- Modify: `src/voice_realtime/ui/server.py`
- Modify: `src/voice_realtime/meeting/summary.py`
- Create: `tests/test_inner_os_integration.py`
- Modify: `tests/test_ui_server.py`
- Modify: `tests/test_meeting_session.py`

**Interfaces:**
- Consumes: Task 5 gate、Task 6 client、Task 7 service、Task 8 repository、现有 `MeetingSession` event publisher。
- Produces: app 生命周期中的单实例依赖图，以及 meeting `finalizing` 前有界取消。

- [ ] **Step 1: 写装配失败测试**

  断言关闭总开关时不创建 client/cache/service/repository/route 可用态；开启时 summary 与 Inner OS 共用同一 gate；任一 Inner OS 初始化失败不会破坏普通会议转录；shutdown 按 service → model client → inner repository → shared gate → meeting repository 顺序收敛。

- [ ] **Step 2: 添加组合事件发布器**

  在 server composition 层包装现有 `broadcaster.publish_event`：先处理 `meeting_state_changed(finalizing)` 的 `cancel_meeting()`，最多等待 2 秒，再转发原会议事件。不要把 Inner OS 条件写入 `MeetingSession`、`RuntimeModeCoordinator` 或 repository。

- [ ] **Step 3: 统一初始化顺序**

  `_initialize_meeting_backend()` 在 migration 后创建 shared gate；summary client/service 与 Inner OS client/service 均从 app state 注入明确依赖。Inner OS 子系统失败时记录错误类型（不含请求内容）并保持会议助手可用。

- [ ] **Step 4: 运行集成测试**

  Run: `uv run pytest tests/test_inner_os_integration.py tests/test_ui_server.py tests/test_meeting_session.py tests/test_meeting_summary.py -q`

  Expected: finalizing 不被查询阻塞超过 2 秒；已有会议 EOF、纪要重排队和普通助手行为无回退。

- [ ] **Step 5: 提交后端装配**

  ```bash
  git add src/voice_realtime/ui/server.py src/voice_realtime/meeting/summary.py tests/test_inner_os_integration.py tests/test_ui_server.py tests/test_meeting_session.py
  git commit -m "feat(meeting): 装配内心 OS 生命周期与资源边界"
  ```

### Task 12（P1-B）: 建立前端独立 feature、私有 socket 和只读 meeting adapter

**Files:**
- Create: `ui/src/features/innerOS/contracts.ts`
- Create: `ui/src/features/innerOS/api.ts`
- Create: `ui/src/features/innerOS/innerOSStore.ts`
- Create: `ui/src/features/innerOS/useInnerOSSocket.ts`
- Create: `ui/src/features/innerOS/index.ts`
- Create: `ui/src/features/innerOS/contracts.test.ts`
- Create: `ui/src/features/innerOS/api.test.ts`
- Create: `ui/src/features/innerOS/innerOSStore.test.ts`
- Create: `ui/src/features/innerOS/useInnerOSSocket.test.ts`
- Modify: `ui/src/stores/meetingStore.ts`

**Interfaces:**
- Consumes: 公共 fixture/REST/WS 契约；meeting store 的当前 meeting ID、status、confirmed segments、starred IDs 只读 selector。
- Produces: query/cancel/save/list/delete actions 和纯前端查询状态；不得反向修改会议 canonical state。

- [ ] **Step 1: 从共享 fixture 写 TS 解析失败测试**

  验证 completed/insufficient/error fixture，拒绝未知 contract version、未知 event type、缺失 evidence、confidence 数字和 answer delta。

- [ ] **Step 2: 写 store 与 socket 状态机测试**

  覆盖：单 active query、busy UI、防重复终态、cancel、重连不自动重发问题、meeting ID 变化立即断开并清空活动态、已完成未保存卡片保留在进程内、保存成功后标记 persisted。

- [ ] **Step 3: 实现无持久化 feature store**

  `innerOSStore` 不接入 Zustand `persist`；只存 query ID、状态、canonical answer 和保存状态。临时目标/议程/背景不能进入 store，而由组件 `useState` 持有并仅在 query command 构造时传递。

- [ ] **Step 4: 实现私有 socket hook**

  只在 `meeting.status === "recording"` 且总开关由 runtime capability 暴露时连接；WebSocket URL 使用当前页面 loopback host，非 loopback 页面不尝试降级代理。重连只恢复通道，不自动重放敏感问题或临时背景。

- [ ] **Step 5: 运行前端数据层测试**

  Run: `cd ui && npm test -- --run src/features/innerOS/contracts.test.ts src/features/innerOS/api.test.ts src/features/innerOS/innerOSStore.test.ts src/features/innerOS/useInnerOSSocket.test.ts src/stores/meetingStore.test.ts`

  Expected: 类型、运行时校验、连接状态和 meeting 只读边界全部通过。

- [ ] **Step 6: 提交前端数据层**

  ```bash
  git add ui/src/features/innerOS ui/src/stores/meetingStore.ts
  git commit -m "feat(ui): 建立内心 OS 私有连接与状态层"
  ```

### Task 13（P1-B）: 实现会议内问答面板、证据定位和结束后保存入口

**Files:**
- Create: `ui/src/features/innerOS/InnerOSPanel.tsx`
- Create: `ui/src/features/innerOS/InnerOSAnswerCard.tsx`
- Create: `ui/src/features/innerOS/InnerOSUnsavedTray.tsx`
- Create: `ui/src/features/innerOS/InnerOSPanel.css`
- Create: `ui/src/features/innerOS/InnerOSPanel.test.tsx`
- Create: `ui/src/features/innerOS/InnerOSAnswerCard.test.tsx`
- Modify: `ui/src/components/meeting/MeetingRecordingView.tsx`
- Modify: `ui/src/components/meeting/MeetingPanel.tsx`
- Modify: `ui/src/components/meeting/MeetingTranscriptViewer.tsx`
- Modify: `ui/src/components/meeting/MeetingPanel.css`
- Modify: `ui/src/components/meeting/MeetingComponents.test.tsx`
- Modify: `ui/src/components/meeting/MeetingPanel.style.test.ts`

**Interfaces:**
- Consumes: Task 12 store/socket、现有 starred IDs 和 transcript cards。
- Produces: 可折叠右侧面板、`fact|draft` 快捷问题、显式取消/保存、结构化答案和证据点击定位。

- [ ] **Step 1: 写核心交互失败测试**

  覆盖：空问题禁用；`Cmd/Ctrl+Enter` 提交；`Escape` 取消；active 时二次提交禁用；可选目标/议程/背景清空后不可恢复；星标 focus 只发送当前会议 confirmed ID；分析意图在 capability 关闭时不可见；无自动发送、无 TTS。

- [ ] **Step 2: 写可信展示失败测试**

  答案卡必须分区展示 Facts、Judgements、Draft、Limitations；Judgement 显示 low/medium/high 与原因，不显示百分比；每条事实可展开 canonical evidence；点击证据后转录滚动并聚焦对应 `data-segment-id`。

- [ ] **Step 3: 实现响应式右侧面板**

  桌面端作为 `MeetingRecordingView` 可折叠右栏，窄屏改为下方抽屉；默认不抢占 transcript 焦点。使用原生 button/textarea、清晰 `aria-live`、可见键盘焦点和 reduced-motion 兼容。

- [ ] **Step 4: 保留结束后 TTL 保存能力**

  已完成未保存答案由进程内 store 保留；会议进入 finalizing/completed 后 `MeetingPanel` 展示 `InnerOSUnsavedTray`，允许在服务端缓存 TTL 内 PUT 保存。页面刷新/进程重启后不可恢复时明确提示，不伪装为永久历史。

- [ ] **Step 5: 运行组件与样式测试**

  Run: `cd ui && npm test -- --run src/features/innerOS/InnerOSPanel.test.tsx src/features/innerOS/InnerOSAnswerCard.test.tsx src/components/meeting/MeetingComponents.test.tsx src/components/meeting/MeetingPanel.style.test.ts`

  Run: `cd ui && npm run build`

  Expected: 交互、可访问性、证据定位、响应式样式和生产构建通过。

- [ ] **Step 6: 提交 P1 UI 闭环**

  ```bash
  git add ui/src/features/innerOS ui/src/components/meeting
  git commit -m "feat(ui): 交付会议内心 OS 问答与证据交互"
  ```

### Task 14（P1-C）: 增加无内容产品指标、端到端验收与文档发布门禁

**Files:**
- Create: `ui/src/features/innerOS/metrics.ts`
- Create: `ui/src/features/innerOS/metrics.test.ts`
- Modify: `ui/src/features/innerOS/InnerOSPanel.tsx`
- Modify: `ui/src/features/innerOS/InnerOSAnswerCard.tsx`
- Create: `tests/test_inner_os_e2e.py`
- Create: `docs/operations/内心OS-P1-候选发布验收记录.md`
- Modify: `docs/manuals/会议助手后端运行与前后端联调.md`
- Modify: `docs/manuals/Voice-Studio-UI-设计方案.md`
- Modify: `docs/README.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: P0 报告、P1 全链路、用户复制/保存/显式“有帮助”动作、20 场候选会议验证数据。
- Produces: North Star 有效回答率、性能/可靠性/隐私验收记录、回滚说明和更新后的文档导航。

- [ ] **Step 1: 写无内容指标失败测试**

  `localStorage` 只允许聚合计数与日期桶：eligible meetings、accepted、completed、cancelled、failed、copied、saved、helpful；禁止问题、答案、证据、完整 meeting/query ID、speaker、模型原始输出。进程内短 ID Set 只用于单页去重。提供清除按钮并验证删除整个 key。

- [ ] **Step 2: 实现 North Star 计算**

  ```text
  effective_answer_rate = 去重后的 copied | saved | helpful 回答数 / completed 回答数
  completion_rate = completed / (completed + failed)
  ```

  cancelled 不进入完成率分母；同一回答多种有效动作只计一次。指标只作候选门禁，不发送外部 telemetry。

- [ ] **Step 3: 增加端到端负向与性能测试**

  覆盖：总开关关闭、loopback 拒绝矩阵、跨会议 focus、断连、取消、finalizing、模型超时、output limit、repair 失败、缓存过期、结束后保存、DB 重读 evidence 状态、全局 broadcaster 零泄漏。

- [ ] **Step 4: 执行聚焦门禁**

  Run: `VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/test_inner_os_*.py -q`

  Run: `cd ui && npm test -- --run src/features/innerOS`

  Expected: Inner OS 后端/前端测试全绿；结构化输出首轮成功率和 repair 后成功率可由 P0/P1 runner 计算。

- [ ] **Step 5: 执行项目全量质量门禁**

  Run: `VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/`

  Run: `uv run mypy src/`

  Run: `uv run ruff check src/ tests/`

  Run: `cd ui && npm test -- --run`

  Run: `cd ui && npm run build`

  Expected: 后端测试及分支覆盖率门禁、mypy strict、ruff、前端全测和生产构建全部通过；任何既有无关失败必须记录原始命令、范围与证据，不得误报全绿。

- [ ] **Step 6: 运行 20 场候选会议产品门禁**

  候选阈值固定为：有效回答率 `>=40%`；排除取消后的完成率 `>=95%`；accepted p95 `<=150ms`；fact/draft p95 `<=10s`；若启用 analysis/mixed 则 p95 `<=30s`；取消 p95 `<=500ms` 且硬上限 `2s`；结构化输出首轮 `>=95%`、一次修复后 `>=99%`；ASR 无 gap 且 confirmed p95 延迟回退 `<=10%`；隐私、跨会议、全局广播泄漏均为 0。

- [ ] **Step 7: 发布或回滚判定**

  - 全部门禁满足：验收记录状态改为 `completed`，功能仍默认关闭，由明确本机配置开启。
  - 产品指标不足但无安全事故：保持开关关闭，回到 P0 调整问题入口/答案结构，不扩大 intent。
  - 任一隐私、跨会议或 ASR 回退：立即关闭总开关，清理未保存缓存，保留脱敏诊断，阻断发布。

- [ ] **Step 8: 更新文档与提交发布记录**

  运行手册记录配置、loopback 限制、TTL、保存/删除和排障；UI 方案记录面板状态机与可访问性；文档中心增加 spec、plan 和验收记录导航。

  ```bash
  git add ui/src/features/innerOS tests/test_inner_os_e2e.py docs README.md
  git commit -m "docs(meeting): 完成内心 OS P1 候选发布验收"
  ```

## Execution Order

```text
Task 1 P0 数据集
  └── Task 2 P0 运行与 Go/Revise/Stop
        └── [仅 Go]
            Task 3 公共契约与配置
              └── Task 4 快照/证据
                  ├── Task 5 工作负载仲裁
                  │    └── Task 6 模型客户端
                  │         └── Task 7 查询状态机/缓存
                  └── Task 8 PostgreSQL 仓储
                       └── Task 9 REST API
            Task 7 + Task 9 ──► Task 10 私有 WebSocket
            Task 5–10 ───────► Task 11 后端装配
            Task 3 ──────────► Task 12 前端数据层
            Task 11 + Task 12 ► Task 13 UI 闭环
            Task 1–13 ───────► Task 14 候选发布门禁
```

默认按上述顺序单 Agent 执行。只有用户明确授权多 Agent 后，才可在 Task 3 契约冻结后并行开展“Task 4–11 后端”和“Task 12 前端数据层”；Task 13、14 必须汇合后串行验证。

## Rollback Plan

1. 运行时首选回滚：设置 `VR_MEETING_INNER_OS_ENABLED=false` 并重启 `vr-ui`；不影响转录、会议历史和已有纪要。
2. 分析意图单独回滚：设置 `VR_MEETING_INNER_OS_ANALYSIS_ENABLED=false`；保留 fact/draft。
3. 前端回滚：隐藏 runtime capability 后不建立专用 WS，不删除已有保存记录。
4. 数据回滚：`0002_inner_os.sql` 只新增表和索引；正常回滚不降迁移、不自动删表。确需删除已保存问答时，必须由用户明确授权并先导出/备份。
5. 事故处置：取消所有活动 query、清空进程内 transient cache、关闭模型 stream；不得影响 `MeetingSession` EOF 冲刷和 PostgreSQL confirmed transcript。

## Self-Review Checklist

- [ ] P0 明确覆盖 3 类会议、30 个 fact/draft 问题和 10 个证据不足问题，并有可执行 Go/Revise/Stop 门禁。
- [ ] P1 的 query/cancel、五阶段事件、完整错误码、REST 保存/分页/删除均有唯一契约文件和测试。
- [ ] context snapshot 只含当前会议 confirmed 数据，并记录 revision、裁剪策略、别名与 content hash。
- [ ] 模型调用固定使用 `/api/v1/chat`、`stream:true`、`store:false`，无 response chain、tools、history 和 CoT 泄漏。
- [ ] `LocalLLMWorkloadGate` 是单实例，优先级只作用于 admission 前，recording 不强杀已入场任务。
- [ ] 一连接一活动 query；断连/finalizing/cancel 均有 2 秒硬上限和单终态测试。
- [ ] 未保存缓存有 count/bytes/TTL 三重上限；保存表不含 prompt、临时背景、原始输出、音频或推理过程。
- [ ] 专用 WS fail-closed 校验 loopback，且任何 Inner OS 事件都不会进入全局会议 broadcaster。
- [ ] 前端临时上下文只在组件内存；feature store、meeting store 和 localStorage 均不保存其内容。
- [ ] 星标片段仅作 relevance boost，点击 evidence 可回到 canonical transcript，不改变事实层。
- [ ] 20 场门禁覆盖产品价值、延迟、取消、结构有效率、ASR 回退、隐私和跨会议隔离。
- [ ] 文档、契约、聚焦测试和项目全量质量门禁均有明确命令与预期结果。
- [ ] 文档中不存在占位标记、未定文件名、未定义类型或要求实施者自行猜测的接口。
