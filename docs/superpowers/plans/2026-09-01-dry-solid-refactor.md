---
title: "sona DRY/SOLID 重构总控计划"
description: "六个可独立验证的内部结构重构阶段，以及与 SpeechRail 的跨仓闭环门禁。"
status: draft
type: execution_plan
category: architecture
version: "2.0.0"
date: 2026-09-01
last_updated: 2026-09-01
owners:
  - "sona-core"
tags:
  - sona
  - dry
  - solid
  - clean-architecture
  - speechrail
related_documents:
  - "docs/architecture/系统总体架构与详细设计方案.md"
  - "docs/architecture/实时语音交互与字幕-方案与最佳实践.md"
  - "docs/decisions/0011-speechrail-only-asr.md"
  - "docs/decisions/0012-speechrail-realtime-tts.md"
contracts:
  - "contracts/meeting-assistant/v1/"
---

# sona DRY/SOLID Refactor Program Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` to execute one linked child plan at a time. Do not implement this umbrella document as one change set.

**Goal:** 在保持 SpeechRail-only ASR/TTS、会议/字幕/交互所有权、PostgreSQL 事务、单 PCM owner 和现有 UI/协议行为的前提下，把六个已确认热点拆成可独立实施、验证和回退的阶段。

**Architecture:** 本文只定义依赖顺序、跨计划门禁和完成判定。具体代码改动由 ASR boundary、SubtitleProxy、meeting lifecycle、interaction pipeline、UI backend composition、frontend helpers 六份子计划承担；每份使用独立 commit，跨仓库不做混合提交。

**Tech Stack:** Python 3.12、asyncio、FastAPI、Pydantic v2、PostgreSQL、Pipecat、React 19、TypeScript 5.8、Vite/Vitest、pytest、Ruff、mypy。

**Status:** Draft — 2026-09-01 已完成静态审查与聚焦基线测试，尚未执行代码重构。

---

## 已核实基线

### 当前代码事实

- `asr/contracts.py` 的 `ASREvent` 和 `StreamingTranscriber.finish()` 直接使用 `meeting.models.TranscriptWindow`；streaming 与 Pipecat adapter 分别解析 SpeechRail raw transcription events。
- `speechrail/transport.py` 已统一通用 envelope、sequence、session/request ID、auth 和 socket close；新的 semantic decoder 必须建立在它之上，而不是复制 transport。
- `SubtitleProxy` 的 browser client 是 `add_client(ws_send)` callback + 每客户端有界队列；`_supervise_connection/_serve_connection/_audio_send_loop` 管的是普通字幕 SpeechRail 连接，不是 browser WebSocket。会议 capture 另有 event/send/reconnect/gap/finalize 状态。
- `MeetingSession.stop()` 当前正确顺序是：FINALIZING → finish capture → persist final window → replay journal → speaker remap → finalize transcript → create minutes → publish/cleanup。原计划中的 finalize-before-remap/replay 顺序错误。
- `MeetingRepository` 当前协议包含 meeting CRUD、transcript、speaker 和 minutes；`RecoveryJournal` 是独立组件。`PostgresMeetingRepository._connection()` 已存在，具体方法各自拥有显式 transaction，不需要 `PgSession`。
- `EchoSuppressionProcessor` 是 L1 音频/RMS/物理闭麦与 barge-in；`SelfEchoFilter` 是 L2 文本相似度兜底。两层不能反转或合并。
- `build_pipeline` 当前接受 `transport/context/persona/audio_queue/echo_state/echo_buffer/stt_factory`；`InteractionSession` 和 `UIRuntime` 都是调用方，factory refactor 必须一并更新且保留旧 seam。
- `ui/server.py` 已有 lifespan，并通过多个 `app.state.*` 字段向 server、meeting API 和 inner-OS API暴露依赖；目标是单一 typed context，而不是声称只改 server 一处即可消除所有动态 state。
- 前端真实入口是 `ui/src/App.tsx`；time helper 有三种单位/显示语义，MeetingHistorySidebar/MeetingPanel 还从 MeetingRecordingView 反向导入 utility；API response parsing 才是完全同构重复。

### 2026-09-01 实测基线

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

结果：`246 passed`。这只证明聚焦基线；各子计划仍须运行自己的新增测试、相关数据库测试、前端测试/构建和项目级门禁。

## 全局约束

- Python 保持 `>=3.12,<3.13`；继续使用 `uv`、PEP 621 和现有 PostgreSQL 测试配置。
- sona 继续拥有会议、字幕、AudioHub、Pipecat、LM Studio、播放、PostgreSQL、UI 与 speaker display name。
- SpeechRail 继续拥有 ASR/TTS 模型 lifecycle、Realtime v2 wire contract、worker、voice catalog 与 readiness。
- 不导入 SpeechRail 内部 Python 模块；只消费 public HTTP/WebSocket contract、typed local adapter 和脱敏 fixture。
- 断线创建新 session/source epoch；不重放或持久化旧 PCM、embedding、完整原始转写或完整 TTS prompt。
- 保持单 PCM owner、bounded queue、Echo L1/L2、barge-in、RuntimeModeCoordinator、会议事务、RecoveryJournal 和 SRT snapshot 语义。
- ADR-0011 保持 SpeechRail-only ASR；ADR-0012 保持 SpeechRail Realtime TTS。`tts_bridge_url`/`BridgeSettings` 只兼容解析至 2026-10-31，生产 pipeline 不得重新读取旧 bridge。
- accepted ADR 与 public contracts 在本轮作为只读依据。若实施必须改变 public field/event/schema，停止当前子计划，先走独立 ADR/contract change。
- 当前工作树含并行文档修改和未跟踪计划。执行时只暂存当前子计划列出的 code/test hunk，不使用 `reset`、`checkout`、`clean`、force-push 或广泛 `git add`。

## 子计划与依赖

| 顺序 | 子计划 | 主要写入热点 | 依赖 | 完成信号 |
|---|---|---|---|---|
| V1 | [SpeechRail ASR boundary](2026-09-01-speechrail-asr-boundary-refactor.md) | `speechrail/transcription_events.py`、`asr/`、meeting mapper | public SpeechRail v2 contract | 两个 adapter 共用 decoder；ASR 无 meeting import |
| V2 | [SubtitleProxy responsibilities](2026-09-01-subtitle-proxy-refactor.md) | `ui/subtitle_proxy.py`、三个 component modules | V1 | callback hub、standard/capture session、archive 分离 |
| V3 | [Meeting lifecycle and ports](2026-09-01-meeting-lifecycle-ports-refactor.md) | `meeting/session.py`、ports/persistence/finalization/repository | V2 | 正确封存顺序与窄 ports 通过 |
| V4 | [Interaction pipeline dependencies](2026-09-01-interaction-pipeline-refactor.md) | `interaction/pipeline.py`、session、`ui/runtime.py` | 可独立；按本总控在 V3 后执行 | L1/L2 分离；factory seam 兼容 |
| V5 | [UI backend composition](2026-09-01-ui-backend-composition-refactor.md) | `ui/server.py`、app context、HTTP/WS、meeting APIs | V3、V4 | 单一 typed context；route/lifespan 不变 |
| V6 | [UI shared helpers](2026-09-01-ui-shared-helpers-refactor.md) | `ui/src` 时间、toggle、HTTP parser | 可独立；按本总控最后执行 | 前端全测与 build 通过 |

依赖图：

```text
V1 ASR boundary
      ↓
V2 SubtitleProxy
      ↓
V3 Meeting ports/finalizer ─┐
                            ├→ V5 UI backend context/routes
V4 Pipeline factories ─────┘
                            ↓
V6 Frontend helpers（代码独立，串行降低 dirty-worktree 冲突）
```

虽然 V4/V6 可独立执行，当前共享工作树不并行修改；主执行者按 V1 → V2 → V3 → V4 → V5 → V6 串行，避免测试和 context seam 交叉漂移。

## 明确不做

- 不把 meeting entity、SpeechRail wire event、ASR DTO 和 UI event 强制复用一个模型。
- 不把 browser sender callback、普通字幕 SpeechRail connection、meeting capture 和 SRT file 继承自万能 connection base。
- 不把 RecoveryJournal 合入 PostgreSQL repository，不隐藏具体 transaction。
- 不把 Echo L1/L2 合并成一个接收 text/audio/RMS 的 policy。
- 不以 `Any/getattr`、service locator 或巨大 `BaseService` 代替 typed optional dependency/Null Object。
- 不提前删除 `tts_bridge_url`，不恢复旧 ASR/TTS backend，不新增自动 fallback。
- 不把不完全同构的时间、starred、clipboard、download 或 toast 逻辑为了 DRY 强行合并。

## Stage 0: 执行前检查

- [ ] 记录分支、HEAD、dirty files 和并行所有权。

```bash
git status --short
git log -5 --oneline
```

- [ ] 运行 246 项聚焦基线使用的同一命令；若失败，先区分当前基线与并行改动，不在结构任务中顺手修复无关错误。
- [ ] 检查 V1–V6 的目标文件是否出现新的未归属 code hunk；有冲突就停止该阶段。

## Stage 1: 执行 V1 ASR boundary

- [ ] 先实现 event-specific decoder，再迁移两个 adapter，最后引入 neutral DTO/meeting mapper。
- [ ] transport 的 JSON/envelope/sequence/session/request 验证保持唯一；ASR decoder 不测试或解析 TTS PCM base64。
- [ ] 只有 `asr/` 不再导入 `meeting.models` 且 proxy/meeting/Pipecat 回归通过后进入 V2。

## Stage 2: 执行 V2 SubtitleProxy

- [ ] 先提取 client hub/archive，再提取 standard session，最后提取 meeting capture。
- [ ] 每个 commit 后验证 browser disconnect、single PCM owner、epoch/gap/reconnect、finish timeout 与 SRT archive。
- [ ] 若出现 queue owner 不清、旧 PCM 进入新 epoch 或 browser disconnect 关闭 capture，回退当前 component commit，不继续 V3。

## Stage 3: 执行 V3 Meeting lifecycle

- [ ] 先定义 capture/repository ports，再提取 persistence，最后提取 finalizer。
- [ ] 使用调用日志精确验证 persist/replay/remap/finalize/minutes 顺序。
- [ ] 不新增 schema/migration/PgSession；数据库测试使用现有 `SONA_TEST_DATABASE_URL`。
- [ ] normal、timeout、repository/journal failure、minutes failure 与 cancellation cleanup 都通过后进入 V4。

## Stage 4: 执行 V4 Interaction pipeline

- [ ] L1 energy gate 与 L2 text policy 分别迁移，先保持行为再注入 factories。
- [ ] `build_pipeline` 旧 keyword、custom pipeline factory、InteractionSession 和 UIRuntime 一起验证。
- [ ] 明确测试 default TTS factory 不读取兼容 `tts_bridge_url`。

## Stage 5: 执行 V5 UI backend composition

- [ ] 先建立 typed context/lifecycle，再提取 HTTP，然后 WS，最后迁移 meeting/inner-OS providers。
- [ ] route set、static ordering、Origin/CORS/auth/redaction、control single writer 与 meeting snapshot-first 不变。
- [ ] 不保留 `app.state.runtime` 与 context.runtime 两套事实源。

## Stage 6: 执行 V6 frontend helpers

- [ ] 时间 helper 先锁定单位和一小时边界，再移动所有 import。
- [ ] toggle hook 只替换 expanded blocks，不碰 starred controlled/uncontrolled 语义。
- [ ] HTTP helper只复用 response/error parsing，不合并业务 service 或 export blob。
- [ ] 每个前端 commit 都运行目标 Vitest 和 build；命令始终使用 `npm --prefix ui ...`，避免 `cd ui` 污染后续工作目录。

## Stage 7: 仓库级验收

- [ ] 运行 Python 全量门禁。

```bash
SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run --extra dev pytest
uv run --extra dev ruff check src tests
uv run --extra dev mypy src
git diff --check
```

- [ ] 运行前端全量门禁。

```bash
npm --prefix ui test -- --run
npm --prefix ui run build
```

- [ ] 核对 accepted ADR、contracts、migration 和 config：除非另有已批准行为任务，本轮不应修改。
- [ ] 检查 diff 不含 `.env`、DSN、token、音频、完整转写、模型、日志、cache、build artifact 或 SpeechRail 仓库文件。

## Stage 8: 跨仓 SpeechRail 闭环

只有 SpeechRail 三份子计划和本仓 V1–V6 均通过后执行。

- [ ] 先在两个仓库分别运行 contract/fake tests；禁止跨仓内部 import。
- [ ] 有真实 runtime、PostgreSQL、设备权限和明确授权时，依次验证 SpeechRail health/ready/models/voices、REST ASR/TTS、Realtime v2、conversation STT、subtitle reconnect/snapshot、meeting capture/finalize/recovery、interaction playback/cancel。
- [ ] 记录 HTTP/WS status、request ID、error code、首 partial/final/TTS chunk、RTF、queue wait、reconnect、峰值内存和设备/dtype 摘要；不记录音频、完整文本、token、绝对模型路径、设备 UID 或 embedding。
- [ ] 缺少真实条件时分别标记 `Python contract/fake: passed`、`frontend: passed`、`real runtime: unverified`，不得宣布生产闭环。
- [ ] 两仓分别提交、分别回退；不要构造跨仓原子 commit。

## 总体验收标准

- [ ] ASR port 不依赖 meeting model，两个 SpeechRail ASR adapter 共用 semantic decoder。
- [ ] SubtitleProxy 保持 public façade，四个实际职责和 ownership 测试分离。
- [ ] MeetingSession 使用 typed gateway/ports，finalizer 顺序正确，journal/transaction 边界清晰。
- [ ] PipelineFactories 可注入，L1/L2 与 barge-in 行为未反转。
- [ ] UI backend 只有一个 typed context，HTTP/WS/meeting/inner-OS dependency 获取显式。
- [ ] 前端只收敛同构 helper，单位、业务 schema、下载与 UX 差异保留。
- [ ] SpeechRail-only ASR/TTS 与 2026-10-31 legacy parse sunset 未被破坏。
- [ ] Python、database、frontend、real runtime 的状态分别有证据，不用静态检查冒充 E2E。
- [ ] 并行文档改动未被覆盖或混入 code commit。

## 回退原则

- 每个子计划和内部 task 使用独立 commit；只回退最近引入回归的结构 commit。
- V1/V2 回退不得重放 PCM；V3 回退不得删除 database/journal；V4 回退不得恢复旧 TTS bridge；V5 回退不得放宽 Origin/Auth；V6 回退不删除用户数据。
- 不使用 destructive Git 命令，不覆盖他人 hunk，不删除 `.env`、PostgreSQL 数据、外部模型、SRT archive 或用户配置。
