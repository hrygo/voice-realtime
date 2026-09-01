---
title: "voice-realtime DRY/SOLID 重构工作交接任务卡"
description: "六个串行重构工作包、仓库级验收和 SpeechRail 跨仓闭环的可领取任务卡。"
status: active
type: handoff_cards
category: architecture
version: "1.0.0"
date: 2026-09-01
last_updated: 2026-09-01
owners:
  - "voice-realtime-core"
tags:
  - voice-realtime
  - handoff
  - dry
  - solid
  - speechrail
related_documents:
  - "docs/superpowers/plans/2026-09-01-dry-solid-refactor.md"
  - "docs/decisions/0011-speechrail-only-asr.md"
  - "docs/decisions/0012-speechrail-realtime-tts.md"
---

# voice-realtime DRY/SOLID 重构工作交接任务卡

> **交接状态：** Ready for assignment（2026-09-01）
>
> **用途：** 供 voice-realtime 团队按依赖顺序领取、实施、审查和验收结构重构。
>
> **总控计划：** [2026-09-01-dry-solid-refactor.md](2026-09-01-dry-solid-refactor.md)

本文件是执行调度与交接证据清单，不替代六份子计划。实现者必须逐项执行对应子计划；若本卡与当前代码、accepted ADR、contract 或测试结果冲突，以当前实测和总控计划规定的事实层级为准，并在继续前更新交接记录。

## 交接快照与工作规则

- 2026-09-01 交接快照：`HEAD=5870d0d`；工作树已有并行文档、Python、测试和前端改动，总控计划已修改，六份子计划尚未跟踪。执行前必须重新记录 HEAD 和 dirty files。
- 当前 dirty hunk 已触及 `config.py`、`interaction/session.py`、meeting、`speechrail/transport.py`、ASR/pipeline/inner-OS tests、`StatusBar`、meeting UI、protocol、store 和 API tests；相关卡开始前必须逐文件确认所有权，不得覆盖或吸收这些改动。
- 执行顺序固定为 `VR-00 → VR-01 → VR-02 → VR-03 → VR-04 → VR-05 → VR-06 → VR-07 → XR-01`。
- 本仓库 WIP 上限为 1。V4/V6 虽可在代码上独立，当前共享 dirty worktree 下仍按总控计划串行，避免测试、context seam 和 UI diff 交叉漂移。
- 每个子计划使用独立分支或 PR；子计划内部按其 Task 边界提交。只暂存本卡归属的 hunk，不使用广泛 `git add`。
- 状态流转为 `待领取 → 进行中 → 待审查 → 完成`；无法满足开始门槛或发现公共行为变化时标记 `阻塞`。
- 实施负责人维护代码和聚焦测试；审查负责人核对 ADR/contract/ownership；验证负责人独立运行完成门禁。三类责任必须在 issue 或 PR 元数据中登记。
- ADR-0011、ADR-0012 和 `contracts/meeting-assistant/v1/` 在本轮只读。`tts_bridge_url`/`BridgeSettings` 只兼容解析至 2026-10-31，生产 pipeline 不得重新读取旧 bridge。
- 不修改 SpeechRail 仓库，不导入其内部 Python 模块；跨仓只使用 public HTTP/WebSocket contract、typed local adapter 与脱敏 fixture。
- 不启动或变更外部服务、数据库、模型、设备权限或生产运行态；真实 E2E 只有在条件与授权同时具备时执行。

## 任务看板

| 卡号 | 工作包 | 依赖 | 默认状态 | 主要完成信号 |
|---|---|---|---|---|
| VR-00 | 执行前基线与所有权锁定 | 无 | 待领取 | HEAD、dirty files、目标 hunk 和基线结果均已登记 |
| VR-01 | SpeechRail ASR boundary | VR-00 | 待领取 | 两个 adapter 共用 decoder，ASR port 不依赖 meeting model |
| VR-02 | SubtitleProxy responsibilities | VR-01 | 待领取 | client、archive、standard、capture 职责分离且 façade 兼容 |
| VR-03 | Meeting lifecycle and ports | VR-02 | 待领取 | finalization 顺序与窄 ports 由测试锁定 |
| VR-04 | Interaction pipeline dependencies | VR-03 | 待领取 | L1/L2 分离，factory seam 与旧 keyword 兼容 |
| VR-05 | UI backend composition | VR-03、VR-04 | 待领取 | 单一 typed context，route/lifespan/安全行为不变 |
| VR-06 | UI shared helpers | VR-05 | 待领取 | 只收敛同构 helper，前端全测与 build 通过 |
| VR-07 | voice-realtime 仓库级验收 | VR-06 | 待领取 | Python、DB、Ruff、mypy、frontend 与 diff gate 通过 |
| XR-01 | 两仓公共契约闭环 | VR-07、SpeechRail `SR-04` | 待领取 | fake/contract 与真实 runtime 状态分别留证 |

## VR-00：执行前基线与所有权锁定

**责任角色：** voice-realtime 技术负责人领取；各模块负责人确认目标 hunk 所有权。

**写入范围：** 无生产文件写入；只在团队 issue/PR 中记录执行信息。

**阻塞后续：** VR-01。

### 执行清单

- [ ] 记录当前分支、HEAD、dirty files，以及每个既有 hunk 的归属。

```bash
git status --short
git log -5 --oneline
```

- [ ] 逐卡核对目标文件；尤其确认当前 pipeline、inner-OS、StatusBar、meeting UI 和 API test hunk 的执行者与保留方式。
- [ ] 运行总控计划使用的 246 项聚焦基线；数量可随新增测试变化，以实际 collected/passed 数为证据。

```bash
uv run pytest \
  tests/asr/test_speechrail_realtime.py \
  tests/asr/test_speechrail_pipecat.py \
  tests/asr/test_proxy_contract.py \
  tests/test_meeting_session.py \
  tests/test_pipeline.py \
  tests/test_ui_server.py \
  tests/test_config.py \
  -q --no-cov
```

- [ ] 若基线失败，记录失败测试、错误摘要、与现有 dirty hunk 的关系；未完成归因前不开始 VR-01。
- [ ] 在 issue/PR 登记实施负责人、审查负责人、验证负责人、执行分支和回退基点。

### 完成证据

- 开始时的 `git status --short` 与 HEAD。
- 聚焦基线的命令、退出码、collected/passed/failed 数量。
- VR-01 目标 hunk 的唯一所有权声明，以及 VR-02 至 VR-06 的冲突预检结果。
- 无代码 commit；本卡只解除执行阻塞。

## VR-01：SpeechRail ASR boundary

**来源：** [2026-09-01-speechrail-asr-boundary-refactor.md](2026-09-01-speechrail-asr-boundary-refactor.md)

**责任角色：** ASR/SpeechRail adapter 负责人实施；meeting 与 subtitle 负责人审查。

**依赖：** VR-00 完成。

**阻塞后续：** VR-02。

### 目标与写入边界

- 新建唯一的 SpeechRail transcription event decoder、ASR-neutral `ASRWindow` 和 meeting mapper。
- 迁移 realtime 与 Pipecat adapter，移除 `asr/` 对 `meeting.models` 的依赖。
- 只修改子计划列出的 `speechrail/transcription_events.py`、`asr/`、`meeting/asr_mapping.py`、`subtitle_proxy.py` 和 ASR tests；transport 的 envelope/sequence/session/request 验证保持唯一。

### 执行清单

- [ ] 按子计划 Task 1–4 执行：event-specific decoder → 两个 adapter → neutral DTO/meeting mapper → 完整门禁。
- [ ] 不在 semantic decoder 重复解析 malformed JSON、sequence gap 或 session/request mismatch，也不解析 TTS PCM base64。
- [ ] 运行 ASR/Subtitle 聚焦矩阵。

```bash
uv run --extra dev pytest tests/asr/test_contracts.py tests/asr/test_speechrail_events.py \
  tests/asr/test_speechrail_realtime.py tests/asr/test_speechrail_pipecat.py \
  tests/asr/test_proxy_contract.py tests/test_pipeline.py tests/test_meeting_session.py \
  -q --no-cov
uv run --extra dev ruff check src tests
uv run --extra dev mypy src
git diff --check
```

### 完成门槛

- [ ] 两个 adapter 共用 decoder；`asr/` 不导入 meeting entity。
- [ ] segment UUID、browser payload、Pipecat frame、meeting final window 和错误语义不变。
- [ ] ADR-0011/0012 与 SpeechRail public contract 未修改。
- [ ] 交接记录包含 decoder/DTO/mapper 的职责边界和独立回退点。

## VR-02：SubtitleProxy responsibilities

**来源：** [2026-09-01-subtitle-proxy-refactor.md](2026-09-01-subtitle-proxy-refactor.md)

**责任角色：** Subtitle/AudioHub 负责人实施；meeting runtime 与 UI WebSocket 负责人审查。

**依赖：** VR-01 完成。

**阻塞后续：** VR-03。

### 目标与写入边界

- 从 `SubtitleProxy` 提取 callback-based client hub、SRT archive、standard SpeechRail session 与 meeting capture session。
- 保留 `SubtitleProxy` constructor/public façade、单 PCM owner、有界队列、epoch/gap/reconnect/finalize 行为。
- browser sender 仍是 callback；不把 browser WebSocket receive loop 或 `serve(websocket)` 放入 proxy。

### 执行清单

- [ ] 按子计划 Task 1–5 执行：保护 façade → client/archive → standard session → meeting capture → façade 收口。
- [ ] 每个组件迁移后验证 disconnect、owner 仲裁、旧 PCM 隔离、finish timeout 和 SRT snapshot。
- [ ] 运行聚焦与项目门禁。

```bash
uv run --extra dev pytest tests/test_subtitle_components.py tests/asr/test_proxy_contract.py \
  tests/test_ui_server.py tests/test_meeting_session.py tests/test_runtime_mode.py -q --no-cov
uv run --extra dev ruff check src tests
uv run --extra dev mypy src
git diff --check
```

### 完成门槛

- [ ] 四个实际职责各有独立 ownership/lifecycle 测试，façade 不重复保存组件状态。
- [ ] `last_window` 是 typed public property；上层不访问私有字段。
- [ ] browser disconnect 不关闭 meeting capture，旧 PCM 不进入新 epoch。
- [ ] UI WebSocket protocol、meeting persistence、public contract 与 SRT 语义不变。

## VR-03：Meeting lifecycle and ports

**来源：** [2026-09-01-meeting-lifecycle-ports-refactor.md](2026-09-01-meeting-lifecycle-ports-refactor.md)

**责任角色：** Meeting/domain 负责人实施；PostgreSQL/recovery 负责人审查。

**依赖：** VR-02 完成。

**阻塞后续：** VR-04、VR-05。

### 目标与写入边界

- 定义 capture 与 repository 窄 ports，提取 transcript persistence 和 `MeetingFinalizer`。
- 严格保留 `finish capture → persist final window → replay journal → speaker remap → finalize transcript → create minutes → publish/cleanup`。
- `RecoveryJournal` 保持独立；具体 repository 方法继续拥有显式 transaction。不新增 schema、migration 或 `PgSession`。

### 执行清单

- [ ] 按子计划 Task 1–5 执行：ports → persistence/fallback → finalizer → start/runtime protocol 与 repository 小型 DRY → 完整门禁。
- [ ] 使用调用日志测试 normal、timeout、repository/journal failure、minutes failure 与 cancellation cleanup。
- [ ] 使用现有测试数据库运行会议聚焦矩阵。

```bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run --extra dev pytest \
  tests/test_meeting_ports.py tests/test_meeting_finalization.py tests/test_meeting_session.py \
  tests/test_meeting_repository.py tests/test_meeting_recovery.py tests/test_meeting_api.py \
  tests/test_runtime_mode.py tests/test_ui_server.py -q --no-cov
uv run --extra dev ruff check src tests
uv run --extra dev mypy src
git diff --check
```

### 完成门槛

- [ ] finalization 调用顺序被精确锁定，所有清理路径最多执行一次。
- [ ] `MeetingSession` public construction/commands 兼容，内部无 repository/gateway/journal `Any/getattr`。
- [ ] `PostgresMeetingRepository` 继续兼容 aggregate ports，SQL transaction 边界可见。
- [ ] 没有新增 migration、隐式数据迁移或 journal 删除；数据库失败证据已记录。

## VR-04：Interaction pipeline dependencies

**来源：** [2026-09-01-interaction-pipeline-refactor.md](2026-09-01-interaction-pipeline-refactor.md)

**责任角色：** Interaction/Pipecat 负责人实施；audio/echo 与 TTS adapter 负责人审查。

**依赖：** 代码上可独立；本看板要求 VR-03 完成后开始。

**阻塞后续：** VR-05。

### 目标与写入边界

- 分别提取 L1 adaptive energy gate 与 L2 text echo policy；不反转、不合并两层。
- 建立 typed `PipelineFactories`，同时更新 `build_pipeline`、`InteractionSession` 与 `UIRuntime`，保留旧 keyword seam。
- default TTS factory 只读取 SpeechRail realtime 配置，不读取兼容 `tts_bridge_url`。

### 执行清单

- [ ] 按子计划 Task 1–5 执行：保护 processor/constructor seam → L1/L2 → factories → caller wiring → sunset gate。
- [ ] 锁定 processor 顺序、audio format、VAD/SmartTurn、barge-in、custom factory 和 cleanup。
- [ ] 运行聚焦与项目门禁。

```bash
uv run --extra dev pytest tests/test_pipeline_dependencies.py tests/test_pipeline.py \
  tests/test_interaction_session.py tests/test_speechrail_tts_service.py tests/test_config.py \
  -q --no-cov
uv run --extra dev ruff check src tests
uv run --extra dev mypy src
git diff --check
```

### 完成门槛

- [ ] L1 接收音频/RMS/物理闭麦状态，L2 只处理文本相似度；执行位置未改变。
- [ ] `build_pipeline` 旧 keyword 和 custom factory 兼容，新增 bundle 可完整注入。
- [ ] `InteractionSession`/`UIRuntime` 显式传递依赖，无隐式 legacy TTS bridge。
- [ ] ADR-0012 与 2026-10-31 parse sunset 未改变。

## VR-05：UI backend composition

**来源：** [2026-09-01-ui-backend-composition-refactor.md](2026-09-01-ui-backend-composition-refactor.md)

**责任角色：** UI backend/FastAPI 负责人实施；meeting/inner-OS 与安全负责人审查。

**依赖：** VR-03、VR-04 完成。

**阻塞后续：** VR-06、VR-07。

### 目标与写入边界

- 建立单一 typed `UIAppContext`，提取 HTTP 与 WebSocket routes，并迁移 meeting/inner-OS dependency provider。
- 保持 `create_app` 签名、route set、lifespan、static ordering、Origin/CORS/Auth/redaction 和 control single-writer 行为。
- 不保留 `app.state.runtime` 与 `context.runtime` 两套事实源；除 context attach/get 和非依赖 installer marker 外，不新增 dependency-style `app.state.*`。

### 执行清单

- [ ] 按子计划 Task 1–6 执行：保护 assembly → typed context/lifecycle → HTTP → WS → meeting/inner-OS providers → 完整门禁。
- [ ] 验证 meeting snapshot-first、SpeechRail proxy、error envelope、partial startup rollback 与 idempotent close。
- [ ] 使用现有测试数据库运行 UI/backend 聚焦矩阵。

```bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run --extra dev pytest \
  tests/test_ui_app_context.py tests/test_ui_server.py tests/test_meeting_api.py \
  tests/test_inner_os_api.py tests/test_runtime_mode.py tests/asr/test_proxy_contract.py \
  -q --no-cov
uv run --extra dev ruff check src tests
uv run --extra dev mypy src
git diff --check
```

### 完成门槛

- [ ] UI dependency state 只有一个 typed context，所有 router 通过显式 context/provider 取依赖。
- [ ] lifespan 正常、部分失败和重复 close 测试通过。
- [ ] HTTP/WS route set、static mount、安全策略、meeting envelope 与 SpeechRail proxy 行为不变。
- [ ] 实际 diff 未吸收当前并行 inner-OS、meeting 或 UI hunk。

## VR-06：UI shared helpers

**来源：** [2026-09-01-ui-shared-helpers-refactor.md](2026-09-01-ui-shared-helpers-refactor.md)

**责任角色：** React/TypeScript 负责人实施；meeting UI 与 inner-OS 负责人审查。

**依赖：** 代码上可独立；本看板要求 VR-05 完成且当前前端 dirty hunk 已协调。

**阻塞后续：** VR-07。

### 目标与写入边界

- 只提取单位明确的 duration functions、expanded-set hook 和同构 HTTP response/error parser。
- 保留 seconds/milliseconds 与显示语义差异；不合并 starred、clipboard、download、toast 或业务 service。
- 不修改 CSS、store、protocol type、API endpoint 或 package dependency；当前 `StatusBar` 等并行 hunk 必须分块保留。

### 执行清单

- [ ] 按子计划 Task 1–4 执行：duration tests/imports → toggle hook → HTTP parser → 全量门禁。
- [ ] 人工核对一小时边界、milliseconds evidence、starred、HTTP 204、export blob、error message/request ID/details、toast/clipboard。
- [ ] 始终从仓库根目录使用 `npm --prefix ui`，运行前端全量测试与构建。

```bash
npm --prefix ui test -- --run
npm --prefix ui run build
git diff --check
```

### 完成门槛

- [ ] `ui/src/shared/` 与 `ui/src/services/http.ts` 只承载同构逻辑，业务 schema 保留在原 service/component。
- [ ] 反向 utility import 消失，单位和 UI 行为由测试锁定。
- [ ] 全量 Vitest 与 production build 通过，无新增依赖或 build artifact 入库。
- [ ] 当前并行 CSS、protocol、store、meeting UI 与 API test hunk 未被覆盖或混入本卡提交。

## VR-07：voice-realtime 仓库级验收

**责任角色：** 未参与主要实现的验证负责人执行；技术负责人签收。

**依赖：** VR-01 至 VR-06 均处于待审查或完成，目标 diff 已冻结。

**写入范围：** 默认无代码写入；发现失败时退回对应卡，不在本卡顺手修复。

### 验收清单

- [ ] 运行 Python、数据库与静态 gate。

```bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run --extra dev pytest
uv run --extra dev ruff check src tests
uv run --extra dev mypy src
git diff --check
```

- [ ] 运行前端全量 gate。

```bash
npm --prefix ui test -- --run
npm --prefix ui run build
```

- [ ] 核对 ADR-0011、ADR-0012、meeting contract、migration 和 config：本轮不得出现未批准变更。
- [ ] 核对单 PCM owner、epoch/reconnect、meeting finalization、L1/L2、typed context 和 frontend semantics 的关键 fixture。
- [ ] 确认提交不含 `.env`、DSN、token、音频、完整转写、模型、日志、cache、build artifact、并行 hunk 或 SpeechRail 文件。
- [ ] 分别标记 `Python/fake`、`PostgreSQL`、`frontend`、`real runtime/client`、`performance/resource` 为 passed、failed 或 unverified。

### 完成证据

- 完整 gate 结果与审查结论。
- VR-01 至 VR-06 的 commit/PR 对应关系和回退顺序。
- ADR/contract“无变化”证据，或导致阻塞的精确差异。
- 可交给 SpeechRail 团队的 adapter fixture、事件字段、错误语义与 workflow 摘要。

## XR-01：两仓公共契约闭环

**共同责任：** voice-realtime 团队执行 adapter/workflow 验证；SpeechRail 团队发布服务端 contract/runtime 证据；跨团队验证负责人汇总。

**依赖：** 本仓 VR-07 与 SpeechRail `SR-04` 均完成。

**写入范围：** 默认只生成验收记录；任何修复必须回到所属仓库的新任务和独立 commit。

### 验收清单

- [ ] 两仓先分别运行 contract/fake tests；本仓至少覆盖两个 ASR adapter 与 SpeechRail TTS adapter/service。

```bash
uv run --extra dev pytest \
  tests/asr/test_speechrail_realtime.py tests/asr/test_speechrail_pipecat.py \
  tests/test_speechrail_tts.py tests/test_speechrail_tts_service.py \
  -q --no-cov
```

- [ ] 确认本仓没有 SpeechRail 内部 Python import，只有 public wire contract 与 local typed adapter。
- [ ] 核对 conversation STT、subtitle reconnect/snapshot、meeting capture/finalize/recovery、interaction playback/cancel 的事件顺序和 owner 边界。
- [ ] 缺少真实 runtime、PostgreSQL 或设备权限时，分别记录 `contract/fake: passed`、`database/frontend: passed|unverified` 与 `real runtime: unverified`。
- [ ] 条件与授权具备时，按服务健康 → REST → Realtime v2 → conversation → subtitle → meeting → interaction 顺序执行 E2E。
- [ ] 只记录状态码、request ID、错误码、首 partial/final/TTS chunk、RTF、queue wait、reconnect、峰值内存和设备/dtype 摘要；不记录凭据、音频、完整文本、embedding、绝对模型路径或设备 UID。
- [ ] 两仓分别提交、分别回退，不制造跨仓原子 commit。

### 完成门槛

- [ ] 两仓 gate 证据可相互引用，public field/event/schema 没有未解释差异。
- [ ] SpeechRail-only ASR/TTS、会议/UI/PostgreSQL 所有权和 2026-10-31 sunset 均未改变。
- [ ] 真实 runtime/client 的 passed/failed/unverified 状态明确。
- [ ] 失败项已落入唯一所属仓库的新任务，不在闭环卡内直接改代码。

## 强制交接记录模板

每张卡进入“待审查”前，执行负责人须在 issue 或 PR 填写以下字段；不得只写“测试通过”。

```text
卡号与状态：
仓库、分支、开始 HEAD、结束 HEAD：
实施负责人、审查负责人、验证负责人：
实际修改文件与未触碰的并行文件：
提交列表及每个提交的职责：
聚焦验证：命令、退出码、collected/passed/failed 数量：
项目门禁：命令、退出码、collected/passed/failed 数量：
ADR/contract/migration/config 结论：
PostgreSQL、前端、真实 runtime/client、性能与资源状态：passed / failed / unverified：
已知风险与后续任务：
回退点与回退后必须复跑的验证：
```

## 回退与升级规则

- 优先 revert 当前卡最近的结构性 commit；不使用 `reset --hard`、`checkout --`、`clean` 或 force-push。
- VR-01/VR-02 回退不得重放 PCM；VR-03 回退不得删除 database/journal；VR-04 回退不得恢复旧 TTS bridge；VR-05 回退不得放宽安全策略；VR-06 回退不得覆盖 UI 并行 hunk。
- PostgreSQL 数据、SRT archive、`.env`、外部模型、用户配置和 SpeechRail 团队改动均不在回退范围。
- 发现 public contract、accepted ADR、安全边界、数据 schema 或运行态必须变化时，停止执行并升级为独立决策任务。
