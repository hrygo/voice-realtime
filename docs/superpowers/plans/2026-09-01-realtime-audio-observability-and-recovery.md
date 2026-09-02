# 实时语音链路修复与可观测性增强实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复三种运行模式语音输入无响应的根因，补齐无 WLK 配置时的普通字幕 fallback，并建立从麦克风、AudioHub、Pipecat、SpeechRail 到 ASR/LLM/TTS 的安全可关联诊断信号。

**Architecture:** 保持 AudioHub 单一采集源和现有模式互斥模型不变。助手链路让 ASR 处理器在缓存 PCM 的同时继续转发 `InputAudioRawFrame`，使下游 VAD 能把语音轮次广播回 ASR；普通字幕增加周期性 `flush()`，在 SpeechRail 没有 WLK streaming backend 时仍能通过批量 flush 产出窗口。所有新日志只记录会话、计数、时延、状态和错误码，不记录 PCM、Base64、转写文本或凭据。

**Tech Stack:** Python 3.12、`uv`、Pipecat、asyncio、SpeechRail Realtime v2 WebSocket、Pydantic Settings、pytest、ruff、mypy、PostgreSQL（仅会议验收使用）。

**Spec:** `docs/architecture/系统总体架构与详细设计方案.md`；本计划同时纳入 2026-09-01 对 `sona` 与 `/Users/hrygo/Documents/SpeechRail` 当前运行态的核实结果。

## Global Constraints

- Python 版本严格锁定为 3.12；新增依赖前必须确认兼容性，优先复用现有依赖。
- `AudioHub` 仍是唯一麦克风采集者；助手、普通字幕、会议模式继续互斥消费 PCM。
- SpeechRail 独占 ASR/TTS 模型生命周期；不得把模型、音频或 `runtime/subtitles/current.srt` 放回 `sona`。
- 默认离线优先；不修改 SpeechRail 或 sona 的运行时 `.env`，不擅自重启服务或安装 WLK。
- 日志禁止包含原始 PCM、Base64 音频、完整转写文本、API key、token、DSN 密码和完整环境变量。
- 保留当前工作区未提交改动；实现前必须审阅当前 `src/sona/ui/subtitle_sessions.py` 的 session 提取边界并纳入兼容范围，禁止覆盖或删除。若执行时该文件重新变为未跟踪文件，也必须按同一规则保护。
- 代码改动遵循 TDD；最终按项目质量门禁执行验证，未完成真实带人声 smoke test 时不得把“ASR 质量正常”表述为已验收。

## 当前已核实事实与变更边界

1. 语音助手的 `SpeechRailConversationSTTProcessor` 在收到 `InputAudioRawFrame` 后只缓存并 `return`，没有把帧送入下游；Pipecat 的 `LLMUserAggregator` 依赖下游 `InputAudioRawFrame` 驱动 VAD，再把 VAD start/stop 广播回上游 STT，形成确定性的 VAD 死锁。因此需要修复帧转发，而不是只调整模型或静音阈值。
2. SpeechRail 当前 8201 服务健康、ASR/TTS ready，但 `/Users/hrygo/Documents/SpeechRail/.env` 没有 WLK streaming URL。无 WLK 时 Realtime v2 只在 `flush`/`commit` 做批量 ASR；普通字幕只 append PCM、没有 flush，所以连接成功但没有字幕事件。这是独立于助手死锁的第二个根因。
3. 真实 `AudioHub → SubtitleProxy → SpeechRail` 探针证明 PCM 可采集和传输，没有 read error、drop 或 reconnect；探针没有刻意讲话，故不能以零结果判定 ASR 失败。新增计数器后必须用带人声样本验收。
4. 旧 SpeechRail REST `/v1/audio/transcriptions` 历史日志出现过音频解码可执行文件缺失的 `FileNotFoundError`；它不等同于当前 `/v2/realtime` 故障，但纳入独立的 decoder preflight/稳定错误码任务，避免以后再次只看到堆栈而无法定位。

## 文件与职责地图

sona：

- Modify `src/sona/asr/adapters/speechrail_pipecat.py`：缓存音频后继续转发 raw frame，并维护助手 STT/VAD 安全计数。
- Modify `src/sona/asr/contracts.py`：为字幕/会议流式端口增加 `flush()` 契约。
- Modify `src/sona/asr/adapters/speechrail_realtime.py`：实现 flush、区分普通字幕连续窗口与会议 EOF final 语义，并输出安全事件元数据。
- Modify `src/sona/speechrail/transport.py`：串行化 append/flush/commit 等发送操作，避免周期 flush 与音频 append 竞态。
- Modify `src/sona/config.py`：增加带边界校验的普通字幕 flush 间隔配置。
- Modify `src/sona/ui/subtitle_sessions.py`：在当前 session 提取结构上增加周期 flush、事件计数、任务异常回收和重连兼容。
- Modify `src/sona/ui/subtitle_proxy.py`、`src/sona/ui/runtime.py`、`src/sona/ui/server.py`：扩展诊断快照与安全字段白名单。
- Modify `src/sona/audio/hub.py`、`src/sona/audio/audio_injector.py`：补齐采集、扇出、注入、read error 和后台任务状态计数。
- Create or Modify `src/sona/observability/audio_flow.py`：集中定义轻量计数快照/安全字段转换，避免各组件自行记录敏感数据。
- Modify relevant tests under `tests/`：覆盖帧转发、flush fallback、发送顺序、计数器、任务失败和日志脱敏。

SpeechRail：

- Modify `/Users/hrygo/Documents/SpeechRail/src/speechrail/observability/logging.py`：扩展允许的安全字段和统一结构化事件格式。
- Modify `/Users/hrygo/Documents/SpeechRail/src/speechrail/app.py`、`src/speechrail/realtime/v2_session.py`：记录 Realtime v2 生命周期、append/flush/commit 聚合计数、结果状态、时延和错误码。
- Modify `/Users/hrygo/Documents/SpeechRail/src/speechrail/application/services.py` 及其现有启动/健康检查路径：为 REST 音频 decoder 做可诊断的依赖预检和稳定错误映射，不改变模型加载策略。
- Modify relevant tests under `/Users/hrygo/Documents/SpeechRail/tests/`：验证安全日志字段、Realtime 事件统计和 decoder 缺失时的错误码。

---

### Task 1: 保护工作区并锁定当前契约

**Files:**
- Inspect: `src/sona/ui/subtitle_sessions.py`
- Inspect: `src/sona/ui/subtitle_proxy.py`
- Inspect: `tests/asr/test_speechrail_pipecat.py`, `tests/asr/test_speechrail_realtime.py`, `tests/test_audio_hub.py`, `tests/test_runtime.py`

**Interfaces:**
- Consumes: 当前 `main` 工作区和已存在的字幕 session 提取。
- Produces: 实施基线、无覆盖风险的文件清单，以及后续任务可复用的现有测试命名。

- [ ] **Step 1: 记录并审阅工作区状态**

  执行 `git status --short --branch`、`git diff --stat`、`git diff -- src/sona/ui/subtitle_proxy.py`，确认 `subtitle_sessions.py` 是当前 `subtitle_proxy.py` 的依赖；不得使用 `git clean`、覆盖写入或删除未提交/未跟踪文件。

- [ ] **Step 2: 建立最小回归测试入口**

  执行：

  ```bash
  uv run pytest tests/asr/test_speechrail_pipecat.py tests/asr/test_speechrail_realtime.py tests/test_audio_hub.py tests/test_runtime.py -q --no-cov
  ```

  记录基线失败与通过项；后续只把由本计划引入的失败视为回归目标。

### Task 2: 修复语音助手 raw frame/VAD 死锁

**Files:**
- Modify: `src/sona/asr/adapters/speechrail_pipecat.py`
- Test: `tests/asr/test_speechrail_pipecat.py`
- Test: 新增或扩展 `tests/interaction/test_pipeline_audio_flow.py`

**Interfaces:**
- Consumes: Pipecat `InputAudioRawFrame`、现有 `append_audio` 缓存逻辑和下游 `FrameDirection`。
- Produces: 每个有效 raw frame 只缓存一次并继续向下游转发；VAD 仍可回传到该 STT processor；诊断快照至少包含 `audio_chunks`、`audio_bytes`、`vad_starts`、`vad_stops`、`last_error_code`。

- [ ] **Step 1: 写失败测试，证明 raw frame 当前没有抵达下游**

  使用 fake downstream collector，发送一个 `InputAudioRawFrame`，断言 collector 收到同一帧且 SpeechRail fake client 收到对应 PCM；再发送 `UserStartedSpeakingFrame`/`UserStoppedSpeakingFrame`，断言 VAD 帧仍按现有协议触发连接、append 和 commit。

  ```python
  async def test_raw_audio_is_forwarded_after_local_buffering():
      processor, downstream, client = make_processor_with_collector()
      frame = InputAudioRawFrame(audio=b"\\x00\\x01", sample_rate=16_000, num_channels=1)
      await processor.process_frame(frame, FrameDirection.DOWNSTREAM)
      assert downstream.frames == [frame]
      assert client.appended == []
  ```

- [ ] **Step 2: 运行定向测试确认红灯**

  执行 `uv run pytest tests/asr/test_speechrail_pipecat.py::test_raw_audio_is_forwarded_after_local_buffering -q --no-cov`，预期失败在 downstream 没有收到 raw frame。

- [ ] **Step 3: 实现最小修复**

  在当前 `InputAudioRawFrame` 分支中保留本地 `_append_audio(frame)`，随后无条件调用现有下游转发方法；没有 active SpeechRail session 时只转发，不尝试发送空连接。不要移动 L1 回声抑制或删除 L2 `SelfEchoFilter`。

- [ ] **Step 4: 增加边界与诊断**

  对空 PCM、非音频控制帧和处理器关闭状态分别保持现有语义；计数只递增整数，不记录音频内容。后台发送异常转换为稳定 `SPEECHRAIL_*` 错误码并记录一次 warning，不能让任务静默结束。

- [ ] **Step 5: 验证助手链路回归**

  执行：

  ```bash
  uv run pytest tests/asr/test_speechrail_pipecat.py tests/interaction/test_pipeline_audio_flow.py -q --no-cov
  ```

  预期 raw frame、VAD start/stop、STT final 相关测试全部通过。

### Task 3: 为普通字幕增加无 WLK 的周期 flush fallback

**Files:**
- Modify: `src/sona/asr/contracts.py`
- Modify: `src/sona/asr/adapters/speechrail_realtime.py`
- Modify: `src/sona/speechrail/transport.py`
- Modify: `src/sona/config.py`
- Modify: `src/sona/ui/subtitle_sessions.py`
- Test: `tests/asr/test_speechrail_realtime.py`
- Test: 新增或扩展 `tests/ui/test_subtitle_sessions.py`

**Interfaces:**
- Consumes: `ASRSessionContext.purpose`, SpeechRail client 的既有 `flush()`，以及 `StandardSubtitleSession` 的 send/receive 两任务模型。
- Produces: `StreamingTranscriber.flush() -> None`；`SubtitleSettings.flush_interval_secs` 默认 `2.0`、范围 `0.0..30.0`；普通字幕在 interval 大于 0 时周期 flush，会议仍仅由 `finish()` 做 EOF commit。

- [ ] **Step 1: 写失败测试覆盖协议和 no-WLK fallback**

  为 fake transcriber 增加 `flush_calls`，让 fake SpeechRail 在收到 `flush` 后返回 `transcription.completed`；断言普通字幕在一个 flush 周期内调用 `flush()` 并广播窗口。另测 `finish()` 只 commit 一次，避免把普通字幕 fallback 误改成会议式终止。

  ```python
  async def test_standard_subtitle_flushes_without_wlk_backend():
      session, transcriber = make_committed_subtitle_session(flush_interval_secs=0.01)
      await session.push_audio(b"pcm")
      await wait_until(lambda: transcriber.flush_calls == 1)
      assert transcriber.payloads[-1]["type"] == "subtitle"
  ```

- [ ] **Step 2: 运行定向测试确认红灯**

  执行 `uv run pytest tests/asr/test_speechrail_realtime.py tests/ui/test_subtitle_sessions.py -q --no-cov`，预期 fake transcriber 没有 `flush()` 或 flush 调用数为 0。

- [ ] **Step 3: 扩展稳定契约并串行化发送**

  在 `StreamingTranscriber` 增加 `async def flush(self) -> None`；`SpeechRailStreamingTranscriber.flush()` 调用客户端 flush。客户端通过单一 async send lock 或统一发送协程保证 `append_pcm → flush → commit` 在同一连接上有序，不允许 flush 与 append 并发写 WebSocket。

- [ ] **Step 4: 实现字幕周期 flush 和完成事件继续监听**

  在 `StandardSubtitleSession._serve_connection()` 中增加 flush task，与 audio send/receive task 一起回收；flush task 只在 session committed、stream 仍为当前 stream 且 interval 大于 0 时运行。普通字幕收到 `TranscriptionCompleted` 后产出 snapshot/final 窗口但保持连接可继续接收后续周期结果；会议 purpose 保持现有 EOF final/diarization 终止语义。

- [ ] **Step 5: 完善重连、关闭和配置边界**

  interval 为 `0` 时不创建 flush task；连接关闭、pause、reset、reconnect 时必须取消并 await flush task，防止旧 epoch flush 到新连接。配置错误在 settings 校验阶段明确报错；不得自动写 `.env` 或自动开启 WLK。

- [ ] **Step 6: 验证 no-WLK 与 WLK 两条行为**

  执行定向测试并补充：no-WLK fake 只在 flush 后出 completed；WLK fake 可返回 delta，周期 flush 不破坏 delta；commit/close 不产生未捕获 `Task exception was never retrieved`。

### Task 4: 建立 sona 音频流可观测性

**Files:**
- Create or Modify: `src/sona/observability/audio_flow.py`
- Modify: `src/sona/audio/hub.py`
- Modify: `src/sona/audio/audio_injector.py`
- Modify: `src/sona/asr/adapters/speechrail_pipecat.py`
- Modify: `src/sona/ui/subtitle_proxy.py`
- Modify: `src/sona/ui/subtitle_sessions.py`
- Modify: `src/sona/ui/runtime.py`
- Modify: `src/sona/ui/server.py`
- Test: `tests/test_audio_hub.py`, `tests/test_audio_injector.py`, `tests/test_runtime.py`, `tests/test_proxy_contract.py`

**Interfaces:**
- Consumes: 现有 `SinkDiagnostics`、`SubtitleProxyDiagnostics`、runtime diagnostic snapshot 和日志 logger。
- Produces: 只读快照字段 `capture_chunks/capture_bytes/read_errors/dropped_chunks/forwarded_chunks/forwarded_bytes`、`stt_vad_starts/stt_vad_stops/stt_flushes/stt_commits`、`asr_ready/asr_delta/asr_completed/asr_errors`、`last_error_code`；字段保持 JSON 可序列化并加入 server 白名单。

- [ ] **Step 1: 写失败测试验证缺失计数**

  测试 AudioHub 读取成功、read `OSError`、sink drop；测试 AudioInjector 的 pump 异常会出现在 diagnostics；测试 SubtitleProxy 能区分 accepted/sent bytes、ready/delta/completed/error、flush 次数。断言诊断快照不含 `audio`、`pcm`、`transcript`、`text`、`base64` 字段。

- [ ] **Step 2: 实现低开销计数和聚合日志**

  在音频热路径只做整数累加和必要的 monotonic timestamp；每约 5 秒输出一次聚合日志，并在关闭时输出 final summary。`stream.read()` 的 `OSError` 保留重试语义，但记录 rate-limited `audio_read_error` 和累计 `read_errors`，不能每帧刷屏。

- [ ] **Step 3: 报告 AudioInjector 后台任务失败**

  为 pump task 加 done callback 或显式 try/except/finally，将失败状态、异常类型和稳定错误码写入快照；所有取消路径使用 `CancelledError` 正常收尾，避免未回收 task。

- [ ] **Step 4: 将诊断安全地暴露到 UI runtime**

  扩展 `UIRuntime.diagnostics()` 和 `_RUNTIME_DIAGNOSTIC_KEYS`，只允许上述数值、布尔值、短错误码和状态字符串。保持现有 API 兼容：旧字段不删除，新字段缺省为 0/null。

- [ ] **Step 5: 验证计数链路**

  执行：

  ```bash
  uv run pytest tests/test_audio_hub.py tests/test_audio_injector.py tests/test_runtime.py tests/test_proxy_contract.py -q --no-cov
  ```

  预期能从一次 fake PCM 注入中看到 capture/forward/ASR append 的单调计数，并能区分“无输入”“有输入但未 VAD”“有 VAD 但 SpeechRail 错误”。

### Task 5: 补齐 SpeechRail Realtime v2 和 REST decoder 安全日志

**Files:**
- Modify: `/Users/hrygo/Documents/SpeechRail/src/speechrail/observability/logging.py`
- Modify: `/Users/hrygo/Documents/SpeechRail/src/speechrail/app.py`
- Modify: `/Users/hrygo/Documents/SpeechRail/src/speechrail/realtime/v2_session.py`
- Modify: `/Users/hrygo/Documents/SpeechRail/src/speechrail/application/services.py`
- Test: `/Users/hrygo/Documents/SpeechRail/tests/test_realtime_v2_websocket.py`
- Test: `/Users/hrygo/Documents/SpeechRail/tests/test_realtime_v2_session.py`
- Test: `/Users/hrygo/Documents/SpeechRail/tests/test_realtime_logging.py`

**Interfaces:**
- Consumes: 当前 SpeechRail v2 WebSocket session、既有 `safe_log_fields`/logging helper 和 REST `_decode_pcm` 路径。
- Produces: 可按 `session_id` 关联的 `realtime_session_started`、`realtime_audio_summary`、`realtime_flush_completed`、`realtime_commit_completed`、`realtime_session_closed`、`realtime_error` 事件；每条只包含 endpoint、client、model、backend、audio chunk/byte 聚合、result segment count、duration、status、error code 等安全字段。

- [ ] **Step 1: 写 caplog 失败测试**

  建立 fake v2 client，发送 connect、多个 append、flush、commit、close；断言日志含事件名、session/request 关联和计数，且日志文本不含 PCM 字节、Base64、segment text 或 token。另测 decoder executable 缺失时返回稳定 `audio_decoder_unavailable` 错误码并保留原始异常类型在内部日志字段之外不向用户泄露路径。

- [ ] **Step 2: 扩展安全字段白名单**

  只允许有限事件名、短枚举状态、非负整数计数、有限精度时延和稳定错误码；对未知 extra 字段丢弃。禁止把完整异常字符串直接写入公共日志，路径、命令参数和环境变量需脱敏。

- [ ] **Step 3: 在 v2 生命周期边界记录聚合事件**

  在 app/session 的 connect、append 聚合、flush/commit 完成、协议错误和 close 边界记录一次；不要按每个 PCM append 打日志。`flush`/`commit` 的 duration 从 monotonic clock 计算；结果只记录 completed/delta/error 状态与 segment 数，不记录文本。

- [ ] **Step 4: 增加 REST decoder preflight 和稳定错误映射**

  在现有服务启动/ready 检查使用的依赖检查模式中探测 `_decode_pcm` 所需 executable；缺失时明确记录 `audio_decoder_unavailable`，REST 请求返回既有错误响应结构加稳定 code。该检查不影响 Realtime v2 已加载的 ASR/TTS ready 状态，也不自动安装依赖。

- [ ] **Step 5: 运行 SpeechRail 定向测试**

  在 SpeechRail 仓库执行：

  ```bash
  uv run --extra dev pytest tests/test_realtime_v2_websocket.py tests/test_realtime_v2_session.py tests/test_realtime_logging.py -q --no-cov
  ```

### Task 6: 端到端验收、文档和提交边界

**Files:**
- Modify: `docs/manuals/SpeechRail-Realtime-v2-语音转文字开发对接手册.md`
- Modify: `docs/architecture/系统总体架构与详细设计方案.md`（仅补充实际运行诊断字段与 fallback 语义）
- Test: `tests/` and SpeechRail `tests/`

**Interfaces:**
- Consumes: Task 2–5 的代码、诊断 API 和 safe logs。
- Produces: 可重复的排障手册、带人声 smoke 验收记录格式、两个仓库可独立回滚的提交。

- [ ] **Step 1: 更新运行手册中的故障判别矩阵**

  写明以下判别链：`capture_chunks=0` 表示采集层；`capture_bytes>0 && forwarded_chunks=0` 表示 AudioHub/Injector；`forwarded_chunks>0 && vad_starts=0` 表示 VAD/门限；`vad_starts>0 && asr_completed=0` 表示 SpeechRail/flush/协议；ASR completed 有值但 LLM/TTS 无值才继续查交互聚合和模型链。明确 no-WLK 普通字幕依赖周期 flush，会议 EOF 依赖 commit。

- [ ] **Step 2: 执行 sona 质量门禁**

  ```bash
  SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/
  uv run mypy src/
  uv run ruff check src/ tests/
  cd ui && npm test -- --run
  cd ui && npm run build
  ```

- [ ] **Step 3: 执行 SpeechRail 质量门禁**

  ```bash
  cd /Users/hrygo/Documents/SpeechRail
  uv run --extra dev pytest
  uv run --extra dev ruff check src tests
  uv run --extra dev mypy src
  git diff --check
  ```

- [ ] **Step 4: 在获得运行态重启授权后做三模式带人声 smoke**

  只在用户明确允许重启当前服务后执行：检查 `/health`、`/readyz`、`/v1/models`，启动 UI，分别验证 assistant、subtitles、meeting。每种模式保存时间窗内的诊断快照：至少要求 assistant 有 raw/VAD/ASR/LLM/TTS 链路事件，subtitles 在 no-WLK 下有 flush 与 completed，meeting 有 append/commit 和最终窗口；环境音或未讲话不作为失败证据。

- [ ] **Step 5: 检查日志隐私与工作区边界**

  用 `rg -n "base64|pcm|transcript|api[_-]?key|authorization|token"` 检查新增日志代码和测试；确认没有日志写入音频/转写内容。再次执行 `git status --short`，确认 `subtitle_sessions.py` 没有被覆盖；两个仓库分别使用符合规范的 commit message，例如 `fix(interaction): 转发音频帧并恢复语音助手 VAD`、`fix(subtitles): 增加 SpeechRail 无 WLK flush fallback`、`feat(observability): 增强实时语音链路诊断`。
