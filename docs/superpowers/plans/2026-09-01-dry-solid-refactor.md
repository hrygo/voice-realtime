---
title: "voice-realtime DRY/SOLID 重构实施计划"
description: "在保持 SpeechRail 迁移结果与会议/字幕/交互边界不变的前提下，拆分字幕代理、会议生命周期、仓储与交互管道中的高耦合模块。"
status: draft
type: execution_plan
category: architecture
version: "1.0.0"
date: 2026-09-01
last_updated: 2026-09-01
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-core"
tags:
  - voice-realtime
  - dry
  - solid
  - clean-architecture
  - speechrail
scope:
  - "src/voice_realtime/asr"
  - "src/voice_realtime/speechrail"
  - "src/voice_realtime/ui"
  - "src/voice_realtime/meeting"
  - "src/voice_realtime/interaction"
  - "ui/src"
related_documents:
  - "docs/architecture/系统总体架构与详细设计方案.md"
  - "docs/architecture/实时语音交互与字幕-方案与最佳实践.md"
  - "docs/decisions/0011-speechrail-only-asr.md"
contracts:
  - "contracts/meeting-assistant/v1/"
---

# voice-realtime DRY/SOLID Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** 在不改变 SpeechRail 公共协议、会议/字幕/交互所有权和现有用户体验的前提下，拆分 voice-realtime 的高耦合模块，建立清晰的应用用例、窄 port、外部 adapter 与 UI presentation 边界。

**Architecture:** ASR/SpeechRail adapter 负责外部协议解码并输出中立 DTO；meeting application 负责会话、对账、纪要与恢复用例；UI/WS 只负责传输和展示。音频 source、Pipecat、SpeechRail、PostgreSQL、LM Studio 依赖通过 typed port 注入，具体实现停留在 adapter/composition root。

**Tech Stack:** Python 3.12、asyncio、Pydantic v2、PostgreSQL、FastAPI、Pipecat、React、TypeScript、Vite、pytest、Ruff、mypy、前端测试与构建工具。

**Spec:** docs/architecture/系统总体架构与详细设计方案.md、docs/architecture/实时语音交互与字幕-方案与最佳实践.md、docs/decisions/0011-speechrail-only-asr.md、SpeechRail contracts/realtime-v2.md，以及同日落盘的 SpeechRail/docs/archive/process/superpowers/plans/2026-09-01-dry-solid-refactor.md。

**Status:** Draft — 2026-09-01 静态 DRY/SOLID 审计结果；当前 SpeechRail TTS/ASR 迁移已在 main 的代码中，本文不重新规划该迁移。

## Global Constraints

- Python 保持 >=3.12,<3.13；继续使用 uv、PEP 621、现有测试数据库和前端工具链。
- voice-realtime 继续拥有会议、字幕、AudioHub、Pipecat、LM Studio、播放、PostgreSQL、UI 和应用侧 speaker display name。
- SpeechRail 继续拥有 ASR/TTS 模型 lifecycle、Realtime v2 协议、worker、公共 voice catalog 和 runtime readiness。
- 不从 voice-realtime 导入 SpeechRail 内部 Python 模块；只消费公共 HTTP/WebSocket 协议、typed adapter 和脱敏 fixture。
- 断线创建新 session/source epoch；不重放或持久化旧 PCM、embedding、完整原始转写或完整 TTS prompt。
- 保持单 PCM owner、Echo L1/L2、防回声、barge-in、RuntimeModeCoordinator、会议事务与 SRT snapshot 的既有语义。
- 保留兼容 alias alloy 到 2026-10-31；在 sunset 前不删除 tts_bridge_url 的只读解析路径，生产 pipeline 不得重新使用旧 bridge。
- 每个行为变化先写确定性失败测试；每项重构完成后运行聚焦测试、ruff、mypy，并在阶段末运行全量门禁。
- 当前仓库 main 干净；执行本计划时不覆盖其他 agent 新增改动，不使用 reset、checkout、clean、force-push 或广泛暂存。

---

## 现状审计与设计结论

### 已确认的问题

| 优先级 | 位置 | 观察 | SOLID/DRY 影响 |
|---|---|---|---|
| P1 | src/voice_realtime/ui/subtitle_proxy.py:110 | 一个类同时管理浏览器 websocket、SpeechRail capture reconnect、bounded audio、事件广播、meeting persistence、SRT 落盘与 finalize | SRP、DIP |
| P1 | src/voice_realtime/asr/contracts.py、asr/adapters/speechrail_realtime.py、speechrail_pipecat.py | ASR port 直接依赖 meeting.models；两个 adapter 各自解析 SpeechRail raw event、重复 capabilities 与 chunk/sequence 规则 | DIP、DRY、ISP |
| P1 | src/voice_realtime/meeting/session.py:45 | MeetingSession 混合状态机、timeout、journal replay、speaker remap、transcript finalization、minutes queue 和 resource cleanup，并通过 getattr 兼容动态 gateway | SRP、DIP、LSP |
| P1 | src/voice_realtime/meeting/repository.py:86 | 一个 MeetingRepository protocol 覆盖 meeting CRUD、transcript、speaker、minutes job、recovery、close；实现文件约 1265 行 | ISP、SRP |
| P2 | src/voice_realtime/interaction/pipeline.py:405、618 | EchoSuppressionProcessor 与 build_pipeline 同时承担算法策略和 composition root；LLM/TTS 依赖没有全部通过 factory 注入 | SRP、DIP、OCP |
| P2 | src/voice_realtime/ui/server.py:231 | app factory、health/proxy、meeting bootstrap、WebSocket handler、static serving 混在同一模块；app.state 类型边界较弱 | SRP、DIP |
| P2 | ui/src/components/*.tsx、ui/src/services/*.ts | duration、timer、toggle set、HTTP response、clipboard 等 helper 重复；相似逻辑存在单位和 UX 差异 | DRY、可维护性 |
| P3 | config.py:106、241、638 | retired BridgeSettings 与 tts_bridge_url 仍需兼容解析；Settings 聚合本身不是立即拆分理由 | YAGNI、兼容性 |

### 已有的正向边界，必须保留

- speechrail/transport.py 已是 ASR/TTS 共用的 sequence、session/request ID、认证、close 与 JSON 校验边界；不要复制第二个 transport。
- speechrail/tts.py 已负责 TTS 事件解析、PCM base64 和 chunk order；结构重构应继续复用，不再引入第三套 client。
- runtime_mode.py 已提供 InteractionWorkload、SubtitleWorkload、MeetingWorkload 协议；只收紧 Any，不把状态仲裁移入 UI。
- voice-realtime 已通过 ADR-0011 选择 SpeechRail-only ASR；不恢复旧 ASR backend 或自动 fallback。
- SubtitleProxy 的 bounded audio ownership、source epoch、reconnect、gap 和 finalize 行为是外部可观察语义，拆分类时必须由 façade 保持兼容。

### 明确不做的“过度 DRY”

- 不把会议 transcript model、SpeechRail wire event 和 UI event 强行复用同一个 Pydantic 类。
- 不把浏览器 websocket、SpeechRail capture websocket 和 SRT 文件写入共用一个万能 connection base。
- 不把 MeetingRepository 的所有 SQL 压缩成失去事务语义的 generic CRUD。
- 不把 Echo L1 与 Echo L2 合并；两层防御分别应对不同故障。
- 不用一个巨大 BaseService、Any 或 getattr 恢复所有依赖；缺少能力必须由 typed optional port 或 Null Object 表达。

---

## 目标拓扑

~~~text
UI / WebSocket / Pipecat adapters
              │
              ▼
Application use cases and typed ports
      │             │              │
      ▼             ▼              ▼
 SpeechRail ASR  Meeting store  Audio/runtime
 TTS adapters    repository     Pipecat/LM/UI
~~~

跨仓库边界：

~~~text
voice-realtime application
        │
        ├── SpeechRailV2Transport / typed event decoder
        ├── SpeechRail ASR/TTS adapters
        └── public HTTP/WebSocket contracts only
                         │
                         ▼
                    SpeechRail service
~~~

---

## 文件与模块地图

本计划执行时新增或修改的主要文件如下：

- Create: src/voice_realtime/asr/models.py
- Create: src/voice_realtime/asr/adapters/speechrail_events.py
- Modify: src/voice_realtime/asr/contracts.py
- Modify: src/voice_realtime/asr/presenters.py
- Modify: src/voice_realtime/asr/adapters/speechrail_realtime.py
- Modify: src/voice_realtime/asr/adapters/speechrail_pipecat.py
- Create: src/voice_realtime/ui/subtitle_sessions.py
- Create: src/voice_realtime/ui/subtitle_broadcast.py
- Create: src/voice_realtime/ui/subtitle_archive.py
- Modify: src/voice_realtime/ui/subtitle_proxy.py
- Create: src/voice_realtime/meeting/ports.py
- Create: src/voice_realtime/meeting/finalization.py
- Modify: src/voice_realtime/meeting/session.py
- Modify: src/voice_realtime/meeting/repository.py
- Create: src/voice_realtime/interaction/echo_policy.py
- Create: src/voice_realtime/interaction/echo_detector.py
- Create: src/voice_realtime/interaction/pipeline_dependencies.py
- Modify: src/voice_realtime/interaction/pipeline.py
- Create: src/voice_realtime/ui/app_services.py
- Create: src/voice_realtime/ui/health_routes.py
- Create: src/voice_realtime/ui/ws_routes.py
- Modify: src/voice_realtime/ui/server.py
- Create: ui/src/shared/duration.ts
- Create: ui/src/shared/useToggleSet.ts
- Create: ui/src/services/http.ts
- Modify: ui/src/components/App.tsx
- Modify: ui/src/components/InnerOSEvidenceItem.tsx
- Modify: ui/src/components/StatusBar.tsx
- Modify: ui/src/components/MeetingRecordingView.tsx
- Modify: ui/src/components/MeetingTranscriptViewer.tsx
- Modify: ui/src/features/innerOS/api.ts
- Modify: ui/src/services/meetingApi.ts
- Create: tests/asr/test_speechrail_events.py
- Create: tests/test_subtitle_components.py
- Create: tests/test_meeting_finalization.py
- Create: tests/test_meeting_repository_ports.py
- Create: tests/test_pipeline_dependencies.py
- Create: ui/src/shared/duration.test.ts
- Create: ui/src/shared/useToggleSet.test.ts
- Create: ui/src/services/http.test.ts

若执行前仓库已有等价文件，沿用现有目录和 import path，不同时保留两个同职责模块。

---

### Task 1: 固化 SpeechRail wire event 与 ASR 中立模型

**Files:**

- Create: src/voice_realtime/asr/models.py
- Create: src/voice_realtime/asr/adapters/speechrail_events.py
- Modify: src/voice_realtime/asr/contracts.py
- Modify: src/voice_realtime/asr/presenters.py
- Modify: src/voice_realtime/asr/adapters/speechrail_realtime.py
- Modify: src/voice_realtime/asr/adapters/speechrail_pipecat.py
- Create: tests/asr/test_speechrail_events.py
- Modify: tests/asr/test_speechrail_realtime.py
- Modify: tests/asr/test_speechrail_pipecat.py

**Interfaces:**

- ASRSegment 是只含语音结果语义的不可变 DTO，包含 order: int、source_epoch: int、speaker_key: str、start_ms: int、end_ms: int、text: str 和可选 detected_language: str。
- ASRWindow 是不可变 DTO，包含 source_epoch: int、partial: str、可选 partial_speaker_key、segments: tuple[ASRSegment, ...] 和 speaker_remap: tuple[tuple[str, str], ...]。
- decode_speechrail_event(raw: Mapping[str, object]) -> SpeechRailEvent 只按 SpeechRail contracts/realtime-v2.md 校验公共 envelope、sequence、session_id、request_id 和 event-specific fields。
- map_event_to_asr_window(event: SpeechRailEvent, state: ASRWindow) -> ASRWindow 不导入 meeting.models。
- SpeechRailStreamingTranscriber.finish() -> ASRWindow 的应用层 mapper 再把 ASRWindow 转成 meeting.NormalizedSegment/TranscriptWindow。

- [ ] **Step 1: 写失败测试**

覆盖 malformed JSON、未知 event type、sequence gap、错误 session/request ID、partial/final/segment/speaker remap、base64 PCM 非法和两个 adapter 对同一 fixture 产生相同 typed event。fixture 只含合成文本和 PCM 摘要，不含真实录音。

- [ ] **Step 2: 运行红灯**

~~~bash
uv run pytest tests/asr/test_speechrail_events.py tests/asr/test_speechrail_realtime.py tests/asr/test_speechrail_pipecat.py -q --no-cov
~~~

预期：新 decoder 和 neutral DTO 尚不存在，新增测试失败。

- [ ] **Step 3: 实现 decoder 与 DTO**

transport.py 继续负责连接、通用 envelope 和 sequence；speechrail_events.py 负责事件类型到 typed result 的映射；ASR adapter 只负责 session lifecycle 与 meeting boundary mapping。对 vendor 字段的缺失、类型错误和不支持事件返回稳定本地异常。

- [ ] **Step 4: 移除 ASR port 对 meeting.models 的直接依赖**

保留对外兼容的 presenter 和 adapter import；在单一 boundary mapper 中构造 NormalizedSegment 与 TranscriptWindow。会议状态、minutes、repository entity 不进入 asr/models.py。

- [ ] **Step 5: 运行聚焦门禁并提交**

~~~bash
uv run pytest tests/asr/test_speechrail_events.py tests/asr/test_speechrail_realtime.py tests/asr/test_speechrail_pipecat.py -q --no-cov
uv run ruff check src/voice_realtime/asr tests/asr
uv run mypy src/voice_realtime/asr
git add src/voice_realtime/asr tests/asr
git commit -m "refactor: isolate speechrail asr boundary"
~~~

---

### Task 2: 拆分 SubtitleProxy，保留兼容 façade 和 bounded audio ownership

**Files:**

- Create: src/voice_realtime/ui/subtitle_sessions.py
- Create: src/voice_realtime/ui/subtitle_broadcast.py
- Create: src/voice_realtime/ui/subtitle_archive.py
- Modify: src/voice_realtime/ui/subtitle_proxy.py
- Create: tests/test_subtitle_components.py
- Modify: tests/asr/test_proxy_contract.py
- Modify: tests/test_ui_server.py

**Interfaces:**

- BrowserSubtitleSession.serve(websocket: WebSocket) -> None 只负责浏览器连接、发送快照、接收 UI command 和 close。
- MeetingCaptureSession.run() -> None 只负责 capture stream、bounded PCM queue、reconnect、gap 与 SpeechRail transcription lifecycle。
- SubtitleBroadcaster.publish(event: SubtitleEvent) -> None 只负责有界 fan-out、慢订阅者清理与 snapshot。
- SrtArchive.append(event: SubtitleEvent) -> None、SrtArchive.finalize() -> None 只负责 SRT/快照写入和 close。
- SubtitleProxy 保留当前 public constructor、prepare/start/stop、diagnostics、last_window、listener 和测试注入点，内部委托以上组件。

- [ ] **Step 1: 为组件写失败测试**

分别测试 browser disconnect 不会关闭 capture owner、capture reconnect 会创建新 source epoch、慢浏览器不会阻塞 PCM owner、SRT finalize 幂等、meeting persistence 失败时仍保留可用 snapshot，以及 SubtitleProxy 旧调用方式仍可用。

- [ ] **Step 2: 运行红灯**

~~~bash
uv run pytest tests/test_subtitle_components.py tests/asr/test_proxy_contract.py tests/test_ui_server.py -q --no-cov
~~~

预期：新组件接口尚不存在，新增测试失败。

- [ ] **Step 3: 提取纯广播与归档组件**

先移动无 socket/模型依赖的 snapshot、subscriber、SRT 状态；组件通过 typed event/callback 接口接收输入，不访问 app.state，不读取 meeting repository。

- [ ] **Step 4: 提取 browser 与 capture session**

将 _supervise_connection、_serve_connection、_audio_send_loop 放入 BrowserSubtitleSession；将 capture loop、reconnect、event loop、finalize 放入 MeetingCaptureSession。PCM queue 只有一个 owner，所有权转移必须由原有状态机显式完成。

- [ ] **Step 5: 保留 façade 并删除重复状态**

SubtitleProxy 只保留生命周期协调、组件装配和兼容属性；不新增 getattr。对外 diagnostics、gap、speaker remap、finalization timeout 和 error code 保持原值。

- [ ] **Step 6: 运行并提交**

~~~bash
uv run pytest tests/test_subtitle_components.py tests/asr/test_proxy_contract.py tests/test_ui_server.py -q --no-cov
uv run ruff check src/voice_realtime/ui/subtitle_sessions.py src/voice_realtime/ui/subtitle_broadcast.py src/voice_realtime/ui/subtitle_archive.py src/voice_realtime/ui/subtitle_proxy.py tests/test_subtitle_components.py
uv run mypy src/voice_realtime/ui
git add src/voice_realtime/ui/subtitle_sessions.py src/voice_realtime/ui/subtitle_broadcast.py src/voice_realtime/ui/subtitle_archive.py src/voice_realtime/ui/subtitle_proxy.py tests/test_subtitle_components.py
git commit -m "refactor: split subtitle proxy responsibilities"
~~~

---

### Task 3: 收紧 MeetingSession 生命周期与 finalization 用例

**Files:**

- Create: src/voice_realtime/meeting/ports.py
- Create: src/voice_realtime/meeting/finalization.py
- Modify: src/voice_realtime/meeting/session.py
- Modify: src/voice_realtime/meeting/runtime_mode.py
- Create: tests/test_meeting_finalization.py
- Modify: tests/test_meeting_session.py

**Interfaces:**

- MeetingGateway protocol 明确声明 last_window: TranscriptWindow | None、prepare_start(...)、publish_started(...)、stop_capture(...) 和 finalize_transcript(...)；不再通过 getattr(gateway, "_capture_last_window") 读取私有字段。
- MeetingFinalizer.finalize(meeting_id: UUID, *, stop_reason: str, timeout_seconds: float) -> MeetingFinalizationResult 负责 transcript finalize、speaker remap、minutes enqueue、journal replay 和资源清理的顺序。
- MeetingFinalizationResult 是不可变结果，至少包含 meeting_id、transcript: TranscriptDocument、minutes: MinutesRecord | None、timed_out: bool 和 recovered: bool。
- MeetingSession 只负责 session state、start/stop command、listener dispatch 和对 MeetingFinalizer 的调用；异常映射与旧 public error type 保持兼容。

- [ ] **Step 1: 写 finalization 失败测试**

覆盖正常 stop、重复 stop、finalize timeout、repository unavailable、journal replay、speaker remap、summary enqueue failure、capture close failure 和 cancellation。测试断言每个 cleanup action 最多执行一次、事件顺序稳定。

- [ ] **Step 2: 运行红灯**

~~~bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/test_meeting_finalization.py tests/test_meeting_session.py -q --no-cov
~~~

预期：MeetingFinalizer 和 typed gateway port 尚不存在，新增测试失败。

- [ ] **Step 3: 定义 typed gateway 与 preparation records**

把当前 session 使用的 Any preparation/result 替换成明确 dataclass 或 protocol；可选能力使用 None 或 Null Object 表达，不用 getattr、字符串方法名或动态属性探测。

- [ ] **Step 4: 实现 MeetingFinalizer**

按现有行为固定顺序：停止 capture → 取得最后 window → 对账/补 gap → finalize transcript → remap speaker → enqueue minutes → 写 recovery journal → 关闭资源。每一步接收 cancellation 和 deadline，失败时保留原 error mapping 与可恢复 journal。

- [ ] **Step 5: 缩小 MeetingSession**

删除 session 内的 repository SQL、speaker remap、minutes queue 和兼容 reflection，只保留状态转换与 finalizer orchestration。对 last_window 由 typed gateway 提供。

- [ ] **Step 6: 运行质量门禁并提交**

~~~bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/test_meeting_finalization.py tests/test_meeting_session.py tests/test_ui_server.py -q --no-cov
uv run ruff check src/voice_realtime/meeting/ports.py src/voice_realtime/meeting/finalization.py src/voice_realtime/meeting/session.py src/voice_realtime/meeting/runtime_mode.py tests/test_meeting_finalization.py tests/test_meeting_session.py
uv run mypy src/voice_realtime/meeting
git add src/voice_realtime/meeting/ports.py src/voice_realtime/meeting/finalization.py src/voice_realtime/meeting/session.py src/voice_realtime/meeting/runtime_mode.py tests/test_meeting_finalization.py tests/test_meeting_session.py
git commit -m "refactor: isolate meeting finalization"
~~~

---

### Task 4: 拆分 MeetingRepository port 与 PostgreSQL transaction helpers

**Files:**

- Modify: src/voice_realtime/meeting/ports.py
- Modify: src/voice_realtime/meeting/repository.py
- Create: tests/test_meeting_repository_ports.py
- Modify: tests/test_meeting_repository.py

**Interfaces:**

- MeetingStore 只拥有 meeting create/get/list/update/delete。
- TranscriptStore 只拥有 reconcile_window(meeting_id: UUID, window: TranscriptWindow) -> TranscriptReconcileResult、finalize_transcript(meeting_id: UUID) -> TranscriptDocument 和 get_transcript(meeting_id: UUID) -> TranscriptDocument。
- SpeakerStore 只拥有 get_speakers(meeting_id: UUID) -> tuple[SpeakerRecord, ...] 与 remap_speakers(...)。
- MinutesQueue 只拥有 enqueue/claim/complete/fail/latest。
- RecoveryStore 只拥有 journal recovery、checkpoint 和 close。
- PostgresMeetingRepository 可以实现多个窄 protocol，但 transaction boundary 必须仍在具体 use case 方法内可见。

- [ ] **Step 1: 写 port contract 测试**

使用 fake repository 实现每个窄 port，断言 MeetingFinalizer 只依赖它需要的接口；测试应拒绝把 minutes queue 方法作为 transcript-only dependency 注入。

- [ ] **Step 2: 运行红灯**

~~~bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/test_meeting_repository_ports.py tests/test_meeting_repository.py -q --no-cov
~~~

预期：当前单一 MeetingRepository protocol 尚未拆分，新增 contract test 失败。

- [ ] **Step 3: 拆分 protocol，不改变 SQL 行为**

保留 PostgresMeetingRepository 对外 import 兼容；将协议按聚合责任分组。不要把每个 SQL 变成隐藏连接/commit 的通用 helper。

- [ ] **Step 4: 提取安全的重复 primitive**

将 _validate_title 与 _validate_display_name 的长度/空白边界抽成带明确字段名和错误信息的 bounded text validator；将重复连接上下文抽成只管理 session/rollback 的 PgSession，事务提交仍由调用方法控制。

- [ ] **Step 5: 运行数据库与静态门禁**

~~~bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/test_meeting_repository_ports.py tests/test_meeting_repository.py tests/test_meeting_finalization.py -q --no-cov
uv run ruff check src/voice_realtime/meeting/repository.py src/voice_realtime/meeting/ports.py tests/test_meeting_repository_ports.py
uv run mypy src/voice_realtime/meeting/repository.py src/voice_realtime/meeting/ports.py
git diff --check
~~~

- [ ] **Step 6: 提交仓储主题**

~~~bash
git add src/voice_realtime/meeting/ports.py src/voice_realtime/meeting/repository.py tests/test_meeting_repository_ports.py tests/test_meeting_repository.py
git commit -m "refactor: narrow meeting repository ports"
~~~

---

### Task 5: 将交互管道改为显式依赖注入并保留双层防回声

**Files:**

- Create: src/voice_realtime/interaction/echo_policy.py
- Create: src/voice_realtime/interaction/echo_detector.py
- Create: src/voice_realtime/interaction/pipeline_dependencies.py
- Modify: src/voice_realtime/interaction/pipeline.py
- Modify: src/voice_realtime/interaction/tts.py only when a typed factory boundary is required
- Create: tests/test_pipeline_dependencies.py
- Modify: tests/test_pipeline.py

**Interfaces:**

- EchoPolicy.should_suppress(*, bot_text: str, user_text: str, rms: float, now: float) -> bool 是无副作用的文本/能量策略。
- EchoDetector.observe(audio: bytes, *, sample_rate: int, channels: int) -> EchoObservation 只负责音频特征与时间窗口。
- PipelineDependencies 明确注入 stt_factory、llm_factory、tts_factory、audio_transport、vad 和 smart_turn；不在 build_pipeline 内隐式构造可替换的外部服务。
- build_pipeline(settings: InteractionSettings, *, dependencies: PipelineDependencies) -> Pipeline 只完成顺序装配。
- EchoSuppressionProcessor 仍保留 Pipecat FrameProcessor 生命周期，但只协调 EchoPolicy、EchoDetector 和 barge-in state；L1 与 L2 行为不合并。

- [ ] **Step 1: 写失败测试**

覆盖 fake STT/LLM/TTS factory 注入、缺失依赖的稳定错误、EchoPolicy 的文本匹配、EchoDetector 的静音/能量边界、barge-in 后 LM Studio cancel/resume，以及 pipeline 中 SpeechRail TTS client 的 cleanup。

- [ ] **Step 2: 运行红灯**

~~~bash
uv run pytest tests/test_pipeline_dependencies.py tests/test_pipeline.py tests/test_speechrail_tts_service.py -q --no-cov
~~~

预期：当前 build_pipeline 尚未接受完整 dependencies，新增测试失败。

- [ ] **Step 3: 实现纯 echo policy/detector**

把可测试的匹配、迟滞、cooldown、rms 和时间窗口逻辑移出 FrameProcessor；FrameProcessor 只维护 task/cancel 和帧方向。保留现有 Echo L1 的文本过滤与 Echo L2 的能量/设备策略。

- [ ] **Step 4: 实现 PipelineDependencies 与 factory 注入**

将现有内联 SpeechRail STT、LM Studio LLM、SpeechRail TTS、VAD/SmartTurn 构造分成 typed factory；默认 composition root 提供生产 factory，测试提供 fake factory。不要改变 transport、frame 顺序、TTS 24 kHz mono 或播放 owner。

- [ ] **Step 5: 运行聚焦门禁并提交**

~~~bash
uv run pytest tests/test_pipeline_dependencies.py tests/test_pipeline.py tests/test_speechrail_tts_service.py -q --no-cov
uv run ruff check src/voice_realtime/interaction/echo_policy.py src/voice_realtime/interaction/echo_detector.py src/voice_realtime/interaction/pipeline_dependencies.py src/voice_realtime/interaction/pipeline.py tests/test_pipeline_dependencies.py tests/test_pipeline.py
uv run mypy src/voice_realtime/interaction
git add src/voice_realtime/interaction/echo_policy.py src/voice_realtime/interaction/echo_detector.py src/voice_realtime/interaction/pipeline_dependencies.py src/voice_realtime/interaction/pipeline.py tests/test_pipeline_dependencies.py tests/test_pipeline.py
git commit -m "refactor: inject interaction pipeline dependencies"
~~~

---

### Task 6: 拆分 UI server、配置兼容边界与前端机械重复

**Files:**

- Create: src/voice_realtime/ui/app_services.py
- Create: src/voice_realtime/ui/health_routes.py
- Create: src/voice_realtime/ui/ws_routes.py
- Modify: src/voice_realtime/ui/server.py
- Modify: src/voice_realtime/config.py
- Create: ui/src/shared/duration.ts
- Create: ui/src/shared/useToggleSet.ts
- Create: ui/src/services/http.ts
- Modify: ui/src/components/App.tsx
- Modify: ui/src/components/InnerOSEvidenceItem.tsx
- Modify: ui/src/components/StatusBar.tsx
- Modify: ui/src/components/MeetingRecordingView.tsx
- Modify: ui/src/components/MeetingTranscriptViewer.tsx
- Modify: ui/src/features/innerOS/api.ts
- Modify: ui/src/services/meetingApi.ts
- Create: ui/src/shared/duration.test.ts
- Create: ui/src/shared/useToggleSet.test.ts
- Create: ui/src/services/http.test.ts
- Modify: tests/test_ui_server.py
- Modify: tests/test_config.py

**Interfaces:**

- UIAppServices 是 typed container，集中提供 settings、runtime、meeting backend、SpeechRail client 和 HTTP client factory；不再让每个 route 从 app.state 动态猜属性。
- create_health_router(services: UIAppServices) -> APIRouter 只负责 health/services/runtime diagnostics。
- create_ws_router(services: UIAppServices) -> APIRouter 只负责 UI websocket protocol。
- formatDurationSeconds(seconds: number): string 与 formatDurationMs(milliseconds: number): string 明确单位，不互相隐式转换。
- useToggleSet<T>(initial: Iterable<T>): { values: Set<T>; toggle(value: T): void; clear(): void } 只复用展开/收起集合逻辑。
- requestJson<T>(input: RequestInfo | URL, init?: RequestInit) -> Promise<T> 统一 HTTP status/error parsing；保留每个业务 API 的 schema mapping。
- SpeechRail voice 列表由服务端代理或 typed API response 提供；alloy 只在边缘 normalize 到 default，不复制四个 voice instruction。

- [ ] **Step 1: 写失败测试**

后端测试覆盖 health、ready、SpeechRail /v1/voices proxy、TTS audition error、meeting bootstrap 和 websocket lifecycle；前端测试覆盖秒/毫秒 duration、toggle set、HTTP error body 和 clipboard 业务差异。

- [ ] **Step 2: 运行红灯**

~~~bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/test_ui_server.py tests/test_config.py -q --no-cov
cd ui && npm test -- --run
~~~

预期：新 typed app services 和共享 helper 尚不存在，新增测试失败或暴露当前重复实现的单位差异。

- [ ] **Step 3: 拆分 UI server**

把 health/proxy 和 websocket route 从 server.py 提取；create_app 保留 lifespan、静态资源和 app assembly。所有 SpeechRail 请求继续经过当前 URL derivation、Authorization header 和 response redaction。

- [ ] **Step 4: 固化配置 sunset**

保留 BridgeSettings 与 tts_bridge_url 的解析兼容直到 2026-10-31；增加测试证明生产 pipeline 不读取该字段。清理时一次性更新 env 示例、文档、启动脚本和所有引用，不提前删除兼容入口。

- [ ] **Step 5: 提取前端 helper**

- 将 App/InnerOSEvidenceItem 的 seconds 与 milliseconds 格式化分成两个命名函数。
- 将 StatusBar 与 MeetingRecordingView 的计时格式化收敛到 duration helper。
- 将两个 transcript viewer 的 toggle set 逻辑收敛到 hook。
- 将 innerOS/api.ts 与 meetingApi.ts 的通用 response/error parsing 收敛到 http.ts；保留各自 endpoint schema 和错误文案。
- clipboard helper 只提取底层写入能力，不抹平 2000/2500 ms 等明确 UX 差异。

- [ ] **Step 6: 运行后端、前端质量门禁并提交**

~~~bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/test_ui_server.py tests/test_config.py tests/test_pipeline.py -q --no-cov
uv run ruff check src tests
uv run mypy src
cd ui && npm test -- --run
cd ui && npm run build
git diff --check
~~~

~~~bash
git add src/voice_realtime/ui src/voice_realtime/config.py tests/test_ui_server.py tests/test_config.py ui/src
git commit -m "refactor: separate voice ui services and shared helpers"
~~~

---

### Task 7: 跨项目契约、架构守护与闭环验收

**Files:**

- Create: tests/test_architecture_boundaries.py
- Modify: tests/asr/test_contracts.py
- Modify: tests/asr/test_proxy_contract.py
- Modify: contracts/meeting-assistant/v1/README.md only when current event mapping changes
- Modify: docs/architecture/系统总体架构与详细设计方案.md only when the current boundary changes
- Modify: docs/decisions/0011-speechrail-only-asr.md only when a new accepted decision supersedes it
- Modify: docs/superpowers/plans/2026-09-01-dry-solid-refactor.md

**Interfaces:**

- voice-realtime 的 ASR/TTS adapter 只能依赖 SpeechRail public REST/WebSocket contract；测试可通过 typed fake transport 注入。
- 不允许 src/voice_realtime 导入 SpeechRail 仓库内部路径；架构测试必须扫描并拒绝该依赖。
- SpeechRailV2Transport 的 sequence、session_id、request_id、auth、close 和 error mapping 只有一个实现。
- meeting event、UI event、SpeechRail wire event 各自拥有明确 mapper，任何字段转换都在 boundary 完成。

- [ ] **Step 1: 写架构边界测试**

测试 import graph、SpeechRail URL/legacy bridge active reference、ASR neutral DTO 与 meeting model 的依赖方向、pipeline factory 注入和 UI app.state typed access。allowlist 只包含当前明确的兼容 alias、文档和迁移测试。

- [ ] **Step 2: 运行聚焦契约门禁**

~~~bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/test_architecture_boundaries.py tests/asr/test_contracts.py tests/asr/test_proxy_contract.py tests/test_speechrail_tts.py tests/test_speechrail_tts_service.py -q --no-cov
~~~

- [ ] **Step 3: 运行两仓库静态门禁**

SpeechRail：

~~~bash
uv run pytest
uv run ruff check src tests
uv run mypy src
npx @redocly/cli lint contracts/openapi.yaml
~~~

voice-realtime：

~~~bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest
uv run ruff check src tests
uv run mypy src
cd ui && npm test -- --run
cd ui && npm run build
~~~

- [ ] **Step 4: 执行真实本地闭环**

在已授权、已配置且不下载模型的前提下，核验 SpeechRail health/ready/models/voices、REST ASR/TTS、Realtime v2 ASR/TTS、voice-realtime adapter、Pipecat audio frame、playback、cancel、reconnect、subtitle snapshot、meeting finalize 和 recovery。没有真实 runtime 时，只报告 fake backend/协议结果。

- [ ] **Step 5: 记录性能与隐私边界**

记录首 partial/final、首 TTS chunk、RTF、并发、队列等待、重连次数、峰值内存和慢消费者行为；不记录音频、完整文本、token、绝对模型路径、设备 UID 或 embedding。

- [ ] **Step 6: 更新计划状态并提交**

只有所有代码门禁和必要真实 smoke 均有证据时，才把本计划 status 改为 implemented；若真实模型、客户端或设备门禁缺失，保留 draft/under_review，并列出精确缺口。

~~~bash
git diff --check
git add tests/test_architecture_boundaries.py tests/asr/test_contracts.py tests/asr/test_proxy_contract.py docs/superpowers/plans/2026-09-01-dry-solid-refactor.md
git commit -m "docs: record voice architecture refactor acceptance"
~~~

---

## 验收标准

- [ ] ASR port 不再直接依赖 meeting.models；SpeechRail raw event 只有一个 typed decoder，ASR 与 Pipecat adapter 共享它。
- [ ] SubtitleProxy 仍保持旧 public façade，但 browser、capture、broadcast、archive 的职责和测试边界分离。
- [ ] MeetingSession 不再通过 getattr 访问私有 capture state；finalization 顺序、timeout、recovery 与 cleanup 由 typed use case 管理。
- [ ] MeetingRepository 被拆成窄 protocol；SQL transaction boundary 可见，标题/名称 bounded validation 只保留一个明确 primitive。
- [ ] build_pipeline 通过 PipelineDependencies 注入 STT/LLM/TTS/runtime；Echo L1/L2 和 barge-in 语义保持不变。
- [ ] UI server 的 app services、health、WS handler 分离；前端共享 helper 只收敛真正同构逻辑，单位和 UX 差异显式保留。
- [ ] SpeechRail-only ASR 与 legacy TTS bridge sunset 规则不被重构破坏；旧 alias 在 2026-10-31 前可解析但不进入生产路径。
- [ ] Python、前端、契约、真实 runtime smoke 的状态分别记录，不以静态检查代替运行态验收。
- [ ] 所有提交只包含本计划范围，未提交凭据、音频、模型、日志、缓存或构建产物。

## 风险与回退

- 若 SubtitleProxy 拆分影响 shared bounded queue 或 source epoch，立即回到 façade 内部实现，保留新组件只用于无副作用 helper，先恢复行为再继续拆分。
- 若 MeetingFinalizer 引入与 journal/repository 的事务冲突，保留旧 stop orchestration 作为可注入 fallback，不改变数据库 schema 和恢复记录。
- 若 ASR neutral DTO 导致既有 UI/meeting 字段丢失，先扩展 boundary mapper 和 fixture，不把 meeting entity 重新塞回 ASR port。
- 若 UI helper 的单位/UX 语义不完全相同，保持两个明确 API；DRY 只应用于完全同构的算法。
- 若真实 SpeechRail snapshot、PostgreSQL 或设备权限不可用，最多完成 fake/contract/static gates；不得宣布端到端生产闭环。
- 每个阶段保持独立 commit；回退时只回退最近结构性 commit，保留 .env、模型、数据库和用户数据。
