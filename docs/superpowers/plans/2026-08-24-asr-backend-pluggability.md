---
title: "ASR 后端可插拔实施计划"
description: "实现 ASR 契约抽象、适配层与评测执行器的执行任务清单"
status: implemented
type: execution_plan
category: asr
version: "v1.0.0"
date: 2026-08-24
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "sona-core"
tags:
  - execution-plan
  - asr
  - pluggability
---

# ASR Backend Pluggability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变默认行为的前提下，为字幕、会议和交互助手建立显式 ASR 契约、注册表、适配器和可复现实验入口，使 Fun-ASR-Nano 能以候选后端安全接入。

**Architecture:** 字幕/会议使用 `StreamingTranscriber`，交互助手使用 `ConversationSTTFactory`；两者共享后端 ID、能力描述和 profile。先用 WLK adapter 平移现有行为，再增加 benchmark runner 和 Fun-ASR adapter。多后端能力只服务于实验比较；最终生产固定单一胜出后端，不实现运行时切换。

**Tech Stack:** Python 3.12、asyncio、Pydantic v2、Pipecat、WebSockets、WhisperLiveKit、pytest、PostgreSQL（仅会议 confirmed 文本）。

**Spec:** `docs/superpowers/specs/2026-08-24-asr-backend-pluggability-design.md`

**Execution status (2026-08-24):** 基础重构 Task 1-4、7 已合入 `main`；Task 5 已在
`feature/asr-benchmark-runner` 实现并通过专项测试、mypy 与 ruff。Fun-ASR 候选和真实语料
实验尚未执行。实际测试文件沿用项目既有 `tests/test_runtime.py`，未创建计划草案中的
`tests/test_ui_runtime.py`。

## Global Constraints

- Python 严格保持 `>=3.12,<3.13`。
- 默认 `allow_model_downloads=False`，候选模型缺失时 fail-fast。
- 麦克风仍由 `AudioHub` 单源采集；助手和会议录音继续互斥。
- 会议不保存音频、不写 `runtime/subtitles/current.srt`；PostgreSQL 仍是 confirmed 文本唯一事实源。
- 保留 `EchoSuppressionProcessor` 与 `SelfEchoFilter` 双层回声防线。
- 不修改 vendor 子仓库来伪装统一接口；差异由 `src/sona/asr/adapters/` 吸收。
- 默认后端保持 `wlk-qwen3-streaming` 和 Pipecat SenseVoice，直到科学对比门禁通过。
- 活动会话内不支持热切换。

---

### Task 1: 冻结 ASR 领域契约

**Files:**
- Create: `src/sona/asr/__init__.py`
- Create: `src/sona/asr/contracts.py`
- Create: `tests/asr/test_contracts.py`

**Interfaces:**
- Produces: `ASRCapabilities`、`ASREvent`、`StreamingTranscriber`、`ConversationSTTFactory`。
- Consumes: `sona.meeting.models.TranscriptWindow`。

- [ ] **Step 1: 写失败测试**

```python
def test_asr_capabilities_are_immutable() -> None:
    caps = ASRCapabilities(
        languages=frozenset({"zh"}),
        supports_partial=True,
        supports_segment_timestamps=True,
        supports_word_timestamps=True,
        supports_hotwords=False,
        supports_speaker_labels=True,
        supports_native_diarization=False,
        supports_eof_flush=True,
    )
    with pytest.raises(FrozenInstanceError):
        caps.supports_partial = False  # type: ignore[misc]

def test_error_event_requires_code_and_message() -> None:
    with pytest.raises(ValueError):
        ASREvent(kind="error")
```

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/asr/test_contracts.py -q --no-cov`

Expected: import 因 `sona.asr.contracts` 不存在而失败。

- [ ] **Step 3: 实现最小契约**

按设计规格实现冻结 dataclass 和两个 `Protocol`。`ASREvent.__post_init__()` 强制 error 事件包含
`error_code`/`error_message`，snapshot/final 事件包含 `TranscriptWindow`。

- [ ] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/asr/test_contracts.py -q --no-cov`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sona/asr tests/asr/test_contracts.py
git commit -m "feat(asr): 定义统一后端能力与事件契约"
```

### Task 2: 用 WLK 适配器平移现有协议

**Files:**
- Create: `src/sona/asr/adapters/__init__.py`
- Create: `src/sona/asr/adapters/wlk.py`
- Create: `src/sona/asr/presenters.py`
- Modify: `src/sona/subtitles/events.py`
- Modify: `src/sona/meeting/transcript.py`
- Create: `tests/asr/test_wlk_adapter.py`
- Modify: `tests/test_subtitles.py`
- Modify: `tests/test_meeting_transcript.py`

**Interfaces:**
- Consumes: `StreamingTranscriber`、现有 `/asr?language=...&mode=full` 协议。
- Produces: `WLKStreamingAdapter.events() -> AsyncIterator[ASREvent]`、
  `legacy_subtitle_payload(window: TranscriptWindow) -> dict[str, Any]` 与
  `WLKStreamingAdapter.finish() -> TranscriptWindow`。

- [ ] **Step 1: 写 golden contract 测试**

将现有 `config`、同时含 `lines`/`buffer_transcription` 的 full snapshot、`error` 和
`ready_to_stop` 固定为 fixtures。断言 adapter 产生一个同时含 confirmed segments 与 partial 的
snapshot、相同时间戳/speaker key 和最终窗口；断言 presenter 重建的浏览器 payload 与 fixture
等价，且 `finish()` 只发送一次空 PCM。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/asr/test_wlk_adapter.py tests/test_subtitles.py tests/test_meeting_transcript.py -q --no-cov`

Expected: 新 adapter import 失败。

- [ ] **Step 3: 实现 adapter 并收窄旧模块**

把 `SubtitleStream` 的连接和 `parse_events()` 逻辑组合到 `WLKStreamingAdapter`；保留旧导入名作为
兼容别名。把 WLK raw → `TranscriptWindow` 的解析留在 adapter 边界；
`meeting/transcript.py` 只保留后端无关的窗口对账。presenter 只从领域窗口生成 legacy full snapshot，
不接收 vendor raw JSON。

- [ ] **Step 4: 运行协议回归**

Run: `uv run pytest tests/asr/test_wlk_adapter.py tests/test_subtitles.py tests/test_meeting_transcript.py -q --no-cov`

Expected: PASS，golden fixture 字段逐项一致。

- [ ] **Step 5: 提交**

```bash
git add src/sona/asr/adapters src/sona/subtitles/events.py src/sona/meeting/transcript.py tests/asr tests/test_subtitles.py tests/test_meeting_transcript.py
git commit -m "refactor(asr): 隔离 WhisperLiveKit 协议适配"
```

### Task 3: 增加判别配置、能力校验与注册表

**Files:**
- Create: `src/sona/asr/profiles.py`
- Create: `src/sona/asr/registry.py`
- Modify: `src/sona/config.py`
- Create: `tests/asr/test_profiles.py`
- Create: `tests/asr/test_registry.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `ASRProfile` 判别联合和 `ASRBackendRegistry.create_streaming(profile)`。
- Preserves: `SONA_SUBTITLE_BACKEND=qwen3-streaming|funasr|auto` 兼容输入。

- [ ] **Step 1: 写配置与注册失败测试**

```python
def test_legacy_funasr_maps_to_sensevoice() -> None:
    settings = SubtitleSettings(backend="funasr")
    assert settings.asr_profile.kind == "wlk-sensevoice"

def test_registry_rejects_duplicate_backend() -> None:
    registry = ASRBackendRegistry()
    registry.register_streaming("wlk-qwen3-streaming", factory)
    with pytest.raises(DuplicateBackendError):
        registry.register_streaming("wlk-qwen3-streaming", factory)
```

同时覆盖未知 ID、语言不支持、缺少 EOF 能力却用于 meeting、Qwen3 专属字段泄漏到 SenseVoice。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/asr/test_profiles.py tests/asr/test_registry.py tests/test_config.py -q --no-cov`

Expected: 新类型和映射不存在而失败。

- [ ] **Step 3: 实现判别联合与注册表**

旧环境变量只在配置边界映射；内部永远使用无歧义 ID。错误类型提供稳定 `code`：
`UNKNOWN_ASR_BACKEND`、`DUPLICATE_ASR_BACKEND`、`ASR_CAPABILITY_MISMATCH`。

- [ ] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/asr/test_profiles.py tests/asr/test_registry.py tests/test_config.py -q --no-cov`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sona/asr src/sona/config.py tests/asr tests/test_config.py
git commit -m "feat(asr): 增加后端配置与注册表"
```

### Task 4: 让 SubtitleProxy 和 UIRuntime 依赖接口

**Files:**
- Modify: `src/sona/ui/subtitle_proxy.py`
- Modify: `src/sona/ui/runtime.py`
- Modify: `src/sona/ui/server.py`
- Modify: `tests/test_subtitle_proxy.py`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_ui_server.py`

**Interfaces:**
- Consumes: `Callable[[], StreamingTranscriber]`。
- Preserves: `SubtitleProxy.begin_capture()`、`finish_capture()`、监听器、SRT 与状态属性。

- [ ] **Step 1: 写行为等价测试**

扩展现有 FakeStream 为契约实现，覆盖普通字幕、会议租约、重连 epoch、gap、EOF、超时、恢复
supervisor、慢客户端背压和 shutdown。断言对外 payload 与迁移前 golden fixture 相同。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/test_subtitle_proxy.py tests/test_runtime.py tests/test_ui_server.py -q --no-cov`

Expected: 构造签名仍要求 WLK `url/language` 而失败。

- [ ] **Step 3: 注入 registry factory**

`SubtitleProxy` 只调用契约方法；WLK URL 拼装移入 adapter；浏览器广播统一调用
`legacy_subtitle_payload()`。`UIRuntime` 接受可选 registry，未提供时构造默认 WLK registry，保持
生产行为不变。

- [ ] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/test_subtitle_proxy.py tests/test_runtime.py tests/test_ui_server.py tests/test_meeting_session.py -q --no-cov`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sona/ui tests/test_subtitle_proxy.py tests/test_runtime.py tests/test_ui_server.py tests/test_meeting_session.py
git commit -m "refactor(asr): 通过统一接口注入字幕后端"
```

### Task 5: 建立可复现实验 runner

**Files:**
- Create: `src/sona/benchmarks/__init__.py`
- Create: `src/sona/benchmarks/asr/__init__.py`
- Create: `src/sona/benchmarks/asr/manifest.py`
- Create: `src/sona/benchmarks/asr/replay.py`
- Create: `src/sona/benchmarks/asr/metrics.py`
- Create: `src/sona/benchmarks/asr/cli.py`
- Create: `tests/benchmarks/test_asr_manifest.py`
- Create: `tests/benchmarks/test_asr_replay.py`
- Create: `tests/benchmarks/test_asr_metrics.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `vr-asr-benchmark run|score|compare`。
- Produces: `manifest.json`、`events.jsonl`、`hypotheses.jsonl`、`resources.csv`、`summary.json`。

- [x] **Step 1: 写 manifest 和回放 RED 测试**

固定一个 16kHz mono PCM fixture，断言每个 profile 收到相同 chunk 序列和 audio cursor；manifest
拒绝缺失模型哈希、语料哈希、git commit 或设备字段。

- [x] **Step 2: 写指标 RED 测试**

用手工可算样例验证 CER/WER、hotword precision/recall/F1、partial revision burden、commit latency、
RTF 和 percentile。空 reference、unsupported 指标和缺失样本必须返回显式状态，不返回伪零值。

- [x] **Step 3: 运行 RED**

Run: `uv run pytest tests/benchmarks -q --no-cov`

Expected: benchmark 模块不存在而失败。

- [x] **Step 4: 实现确定性 runner**

`run` 支持 `offline` 与 `realtime-1x`；chunk schedule 来自语料 manifest，使用单调时钟；原始 vendor
响应与统一事件分别写入。输出目录默认 `runtime/benchmarks/asr/<run_id>/`，不复制音频，只记录相对
ID 和 SHA-256。

- [x] **Step 5: 运行 GREEN**

Run: `uv run pytest tests/benchmarks -q --no-cov`

Expected: PASS。

- [x] **Step 6: 提交**

```bash
git add src/sona/benchmarks tests/benchmarks pyproject.toml
git commit -m "feat(asr): 增加可复现对比测试运行器"
```

### Task 6: 接入 Fun-ASR-Nano 官方 WebSocket 候选

**Files:**
- Create: `src/sona/asr/adapters/funasr_nano_ws.py`
- Modify: `src/sona/asr/profiles.py`
- Modify: `src/sona/asr/defaults.py`
- Modify: `src/sona/benchmarks/asr/cli.py`
- Create: `tests/asr/test_funasr_nano_ws_adapter.py`
- Create: `tests/asr/test_defaults.py`
- Modify: `tests/asr/test_profiles.py`
- Modify: `tests/asr/test_registry.py`
- Modify: `tests/benchmarks/test_asr_cli.py`

**Interfaces:**
- Consumes: Fun-ASR `START`/`LANGUAGE`/`HOTWORDS`/binary PCM/`STOP` 协议。
- Produces: `StreamingTranscriber`，backend ID `funasr-nano-ws`。

- [x] **Step 1: 写协议映射 RED 测试**

Mock WebSocket 必须验证消息顺序；覆盖 `started`、partial、sentences、final、服务端 error、断线和
超时。时间戳缺失或非单调时 capabilities 必须报告 false 或拒绝 meeting 用途，禁止补造时间戳。

- [x] **Step 2: 运行 RED**

Run: `uv run pytest tests/asr/test_funasr_nano_ws_adapter.py tests/asr/test_registry.py -q --no-cov`

Expected: adapter 不存在而失败。

- [x] **Step 3: 实现边界验证和 adapter**

所有外部 JSON 在 adapter 边界做类型、长度和单调性校验。`finish()` 发送一次 `STOP` 并等待
`is_final=true`；错误映射为稳定 `ASREvent(kind="error")`，不把服务端堆栈透传给 UI。

- [x] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/asr/test_funasr_nano_ws_adapter.py tests/asr/test_registry.py -q --no-cov`

Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add src scripts tests README.md AGENTS.md docs
git commit -m "feat(asr): 接入 Fun-ASR 并规范模型缓存"
```

### Task 7: 抽离交互助手 STT 工厂

**Files:**
- Create: `src/sona/asr/adapters/pipecat_sensevoice.py`
- Modify: `src/sona/interaction/pipeline.py`
- Modify: `src/sona/interaction/session.py`
- Modify: `src/sona/config.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_interaction_session.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `PipecatSenseVoiceFactory.create_processor(sample_rate, language)`。
- Preserves: 当前 `device="cpu"`、`use_itn=True`、`ttfs_p99_latency=0.5` 与模型本地解析。

- [ ] **Step 1: 写等价与注入 RED 测试**

断言默认 factory 构造参数与当前完全相同；自定义 factory 的 processor 位于 echo suppressor 与
self-echo filter 之间；现有 13 节点顺序、Silero VAD 和双层回声防线不变。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/test_pipeline.py tests/test_interaction_session.py tests/test_config.py -q --no-cov`

Expected: `ConversationSTTFactory` 尚未被 pipeline 消费而失败。

- [ ] **Step 3: 抽离默认实现**

`build_pipeline()` 接受可选 `stt_factory`，为空时使用 `PipecatSenseVoiceFactory`。此任务不注册
Fun-ASR conversation 实现，不改变默认模型或设备。

- [ ] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/test_pipeline.py tests/test_interaction_session.py tests/test_config.py -q --no-cov`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sona/asr/adapters/pipecat_sensevoice.py src/sona/interaction src/sona/config.py tests/test_pipeline.py tests/test_interaction_session.py tests/test_config.py
git commit -m "refactor(asr): 抽离交互助手 STT 工厂"
```

### Task 8: ~~增加空闲冷切换与失败恢复~~（取消）

**取消原因（2026-08-24）：** 科学对比只需 benchmark runner 并列执行多个 adapter；最终选型后生产
固定单一后端，落选模型和专用接入可清理。`UIRuntime` 也不拥有外部 ASR 服务进程，因此不再建设
没有生产需求支撑的 supervisor、切换事务或 UI/API 切换入口。以下步骤仅保留为历史计划，不执行。

**Files:**
- Create: `src/sona/asr/switching.py`
- Modify: `src/sona/ui/runtime.py`
- Modify: `src/sona/ui/protocol.py`
- Modify: `src/sona/ui/control.py`
- Create: `tests/asr/test_switching.py`
- Modify: `tests/test_runtime_mode.py`
- Modify: `tests/test_control.py`

**Interfaces:**
- Produces: `switch_profile(profile_id: str) -> ASRSwitchResult`。
- Errors: `MODE_CONFLICT`、`ASR_PREFLIGHT_FAILED`、`ASR_SWITCH_FAILED`、`ASR_ROLLBACK_FAILED`。

- [ ] **Step 1: 写状态机 RED 测试**

覆盖 idle 成功切换、assistant/meeting 拒绝、candidate preflight 失败不停止现后端、candidate 启动失败
恢复旧后端、恢复失败进入 error。断言 active profile 只在 smoke final 成功后提交。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/asr/test_switching.py tests/test_runtime_mode.py tests/test_control.py -q --no-cov`

Expected: switching service 和控制消息不存在而失败。

- [ ] **Step 3: 实现冷切换事务**

控制协议采用新增可选消息，不修改既有字段；首版只在 loopback 控制 WS 开放，不增加前端按钮。
preflight 验证本地模型、语言和用途能力，并用固定短 PCM 完成 ready/final smoke。

- [ ] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/asr/test_switching.py tests/test_runtime_mode.py tests/test_control.py tests/test_ui_server.py -q --no-cov`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sona/asr/switching.py src/sona/ui tests/asr/test_switching.py tests/test_runtime_mode.py tests/test_control.py tests/test_ui_server.py
git commit -m "feat(asr): 增加空闲态后端冷切换"
```

### Task 9: 完成全量门禁与真实闭环

**Files:**
- Modify: `README.md`
- Modify: `docs/实时语音交互与字幕-方案与最佳实践.md`
- Modify: `docs/会议助手后端运行与前后端联调.md`

**Interfaces:**
- Documents: 后端 ID、兼容映射、冷切换限制、benchmark 命令与回退方法。

- [ ] **Step 1: 更新用户文档**

命令使用 `python3`/`uv` 等可移植工具；不写入本机 wrapper、绝对缓存路径、凭据或真实音频路径。

- [ ] **Step 2: 执行全量静态与测试门禁**

Run:

```bash
SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/
uv run mypy src/
uv run ruff check src/ tests/
cd ui && npm test -- --run
cd ui && npm run build
```

Expected: 全部退出码为 0，pytest 分支覆盖率不低于 80%。

- [ ] **Step 3: 执行真实默认路径闭环**

依次验证字幕、助手、会议 EOF、会议后字幕恢复、重连 gap、PostgreSQL confirmed 文本、无音频文件；
再在 idle 执行一次 candidate 失败回退演练。每次记录 profile 和运行 manifest。

- [ ] **Step 4: 提交**

```bash
git add README.md docs/实时语音交互与字幕-方案与最佳实践.md docs/会议助手后端运行与前后端联调.md
git commit -m "docs(asr): 补充后端切换与基准测试说明"
```
