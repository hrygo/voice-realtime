---
title: "SpeechRail ASR 边界重构实施计划"
status: draft
type: execution_plan
date: 2026-09-01
owners: ["sona-core"]
related_documents:
  - "docs/decisions/0011-speechrail-only-asr.md"
  - "docs/decisions/0012-speechrail-realtime-tts.md"
---

# SpeechRail ASR Boundary Refactor Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task, with a review checkpoint after each commit.

**Goal:** 让字幕/会议 ASR 与 Pipecat conversation STT 共用一个 SpeechRail transcription event 语义解码器，并让 `asr` port 输出中立 DTO，不再直接依赖 `meeting.models`。

**Architecture:** `speechrail/transport.py` 继续独占 JSON、通用 envelope、sequence、session/request ID、auth 和 socket close；新 `speechrail/transcription_events.py` 只校验 event-specific 字段；`asr/models.py` 定义中立结果；`meeting/asr_mapping.py` 是进入会议实体的唯一 mapper。TTS 继续使用 `speechrail/tts.py`，本计划不解析音频 base64。

**Tech Stack:** Python 3.12、dataclasses、asyncio、Pipecat、pytest、Ruff、mypy、SpeechRail Realtime v2。

---

## 执行边界

- 工作目录：`/Users/hrygo/Documents/sona`
- 前置：SpeechRail 当前 public `contracts/realtime-v2.md` 作为只读事实来源；无需导入 SpeechRail Python 源码。
- 后续：`2026-09-01-subtitle-proxy-refactor.md` 依赖本计划稳定的 `ASRWindow` 与 mapper。
- `SpeechRailV2Transport.receive()` 已验证 JSON object、严格递增 sequence、非空 type/session/request ID 及同会话一致性；新 decoder 不重复这些检查。
- `SpeechRailRealtimeClient.connect()` 已消费 `session.created`；新 decoder 处理 `input_audio_buffer.ack`、`transcription.delta`、`transcription.completed`、`transcription.diarization.completed`、`session.completed` 和 `error`。
- PCM base64 只属于 TTS outbound 与 append input 边界；ASR event decoder 不新增 `decode_pcm16` 逻辑。
- 保持 ADR-0011 的 SpeechRail-only ASR，不恢复旧 backend 或自动 fallback。
- 不改变 browser legacy subtitle payload、meeting `TranscriptWindow`、Pipecat frame 顺序或用户可见错误文案。

## 目标文件

- Create: `src/sona/speechrail/transcription_events.py`
- Create: `src/sona/asr/models.py`
- Create: `src/sona/meeting/asr_mapping.py`
- Modify: `src/sona/asr/contracts.py`
- Modify: `src/sona/asr/presenters.py`
- Modify: `src/sona/asr/adapters/speechrail_realtime.py`
- Modify: `src/sona/asr/adapters/speechrail_pipecat.py`
- Modify: `src/sona/ui/subtitle_proxy.py`
- Create: `tests/asr/test_speechrail_events.py`
- Modify: `tests/asr/test_contracts.py`
- Modify: `tests/asr/test_speechrail_realtime.py`
- Modify: `tests/asr/test_speechrail_pipecat.py`
- Modify: `tests/asr/test_proxy_contract.py`

## Task 1: 建立单一 transcription event decoder

**Files:**

- Create: `src/sona/speechrail/transcription_events.py`
- Create: `tests/asr/test_speechrail_events.py`

- [ ] **Step 1: 写 event-specific 红灯测试**

覆盖每个支持 event、未知 type、缺失/空 text、非 list segments、非法 timestamp、缺失/非法 speaker、非法 remap、error 缺失 code。不要在这里测试 malformed JSON、sequence gap 或 session/request mismatch；它们继续由 `tests/test_speechrail_tts.py` 和 transport 测试负责。

- [ ] **Step 2: 定义窄 typed union**

```python
@dataclass(frozen=True, slots=True)
class SpeechRailSegment:
    text: str
    start_ms: int
    end_ms: int
    speaker: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionDelta:
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptionCompleted:
    text: str
    segments: tuple[SpeechRailSegment, ...]


@dataclass(frozen=True, slots=True)
class DiarizationCompleted:
    mapping: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class InputAudioAck:
    pass


@dataclass(frozen=True, slots=True)
class SessionCompleted:
    pass


@dataclass(frozen=True, slots=True)
class SpeechRailTranscriptionError:
    code: str
    message: str


type SpeechRailTranscriptionEvent = (
    InputAudioAck
    | TranscriptionDelta
    | TranscriptionCompleted
    | DiarizationCompleted
    | SessionCompleted
    | SpeechRailTranscriptionError
)
```

`decode_transcription_event(raw: Mapping[str, object]) -> SpeechRailTranscriptionEvent` 对 bool/int 做严格区分，拒绝空文本 segment 和 `end_ms < start_ms`。是否必须有 speaker 由上层 session 的 diarization 配置判断，不由通用 decoder 猜测。

- [ ] **Step 3: 运行并提交 decoder**

```bash
uv run --extra dev pytest tests/asr/test_speechrail_events.py -q --no-cov
uv run --extra dev ruff check src/sona/speechrail/transcription_events.py tests/asr/test_speechrail_events.py
uv run --extra dev mypy src/sona/speechrail/transcription_events.py
git add src/sona/speechrail/transcription_events.py tests/asr/test_speechrail_events.py
git commit -m "refactor: centralize speechrail asr events"
```

## Task 2: 让两个 adapter 使用 decoder

**Files:**

- Modify: `src/sona/asr/adapters/speechrail_realtime.py`
- Modify: `src/sona/asr/adapters/speechrail_pipecat.py`
- Modify: `tests/asr/test_speechrail_realtime.py`
- Modify: `tests/asr/test_speechrail_pipecat.py`

- [ ] **Step 1: 写共享 fixture 一致性测试**

同一 delta/completed/error fixture 经 decoder 后，streaming adapter 生成现有 `ASREvent`，Pipecat adapter 生成现有 `InterimTranscriptionFrame`/`TranscriptionFrame`/stable error。Pipecat 可忽略 segments，但不得重新解析原始 dict。

- [ ] **Step 2: 替换分散的 `event.get("type")` 分支**

两个 adapter 只对 typed event 做 pattern matching。Streaming adapter 继续负责 diarization 是否请求、observed speaker、final-ready 与 timeout；Pipecat adapter 继续负责 VAD turn、client close 和 frame timestamp。

- [ ] **Step 3: 运行并提交 adapter 迁移**

```bash
uv run --extra dev pytest tests/asr/test_speechrail_events.py \
  tests/asr/test_speechrail_realtime.py tests/asr/test_speechrail_pipecat.py \
  -q --no-cov
uv run --extra dev ruff check src/sona/asr/adapters tests/asr
uv run --extra dev mypy src/sona/asr src/sona/speechrail
git add src/sona/asr/adapters tests/asr/test_speechrail_realtime.py tests/asr/test_speechrail_pipecat.py
git commit -m "refactor: reuse speechrail transcription decoder"
```

## Task 3: 引入 ASR 中立 DTO 与会议 mapper

**Files:**

- Create: `src/sona/asr/models.py`
- Create: `src/sona/meeting/asr_mapping.py`
- Modify: `src/sona/asr/contracts.py`
- Modify: `src/sona/asr/presenters.py`
- Modify: `src/sona/asr/adapters/speechrail_realtime.py`
- Modify: `src/sona/ui/subtitle_proxy.py`
- Modify: `tests/asr/test_contracts.py`
- Modify: `tests/asr/test_speechrail_realtime.py`
- Modify: `tests/asr/test_proxy_contract.py`

- [ ] **Step 1: 写依赖方向红灯测试**

`tests/asr/test_contracts.py` 断言 `sona.asr` 可在不导入 `sona.meeting.models` 的情况下加载；DTO 验证非负 epoch/order/timestamp、非空 speaker/text 和 immutable tuple。

- [ ] **Step 2: 定义 neutral result**

```python
@dataclass(frozen=True, slots=True)
class ASRSegment:
    order: int
    source_epoch: int
    speaker_key: str
    start_ms: int
    end_ms: int
    text: str
    translation: str | None = None
    detected_language: str | None = None


@dataclass(frozen=True, slots=True)
class ASRWindow:
    source_epoch: int
    partial: str = ""
    partial_speaker_key: str | None = None
    segments: tuple[ASRSegment, ...] = ()
    speaker_remap: tuple[tuple[str, str], ...] = ()
```

`ASREvent.window` 与 `StreamingTranscriber.finish()` 改为 `ASRWindow`。`asr/presenters.py::legacy_subtitle_payload` 接收 `ASRWindow`，因此 browser subtitle rendering 不需要 meeting model。

- [ ] **Step 3: 实现唯一会议 mapper**

`meeting/asr_mapping.py::to_transcript_window(window: ASRWindow) -> TranscriptWindow` 负责 deterministic UUID、meeting `NormalizedSegment` 和字段投影。UUID seed 使用版本化的 `speechrail:v2` 身份（source epoch、segment epoch/order、带 meeting group 的 speaker key、绝对 start/end 和 text），保证同一窗口重播稳定，同时隔离不同会议或不同时间的同文段；历史已落库 ID 不迁移。

- [ ] **Step 4: 在 SubtitleProxy 的 meeting capture 边界映射**

普通字幕路径直接把 `ASRWindow` 交给 legacy presenter；会议路径在更新 `last_window`、调用 meeting listener 和 `finish_capture()` 返回前调用 `to_transcript_window`。这样 MeetingSession 与 repository 无需在本阶段改变。

- [ ] **Step 5: 运行并提交 neutral boundary**

```bash
uv run --extra dev pytest tests/asr/test_contracts.py tests/asr/test_speechrail_realtime.py \
  tests/asr/test_speechrail_pipecat.py tests/asr/test_proxy_contract.py -q --no-cov
uv run --extra dev ruff check src/sona/asr src/sona/meeting/asr_mapping.py \
  src/sona/ui/subtitle_proxy.py tests/asr
uv run --extra dev mypy src
git add src/sona/asr src/sona/meeting/asr_mapping.py \
  src/sona/ui/subtitle_proxy.py tests/asr
git commit -m "refactor: decouple asr results from meeting models"
```

## Task 4: 完整门禁

- [ ] **Step 1: 运行 ASR/Subtitle 聚焦矩阵**

```bash
uv run --extra dev pytest tests/asr/test_contracts.py tests/asr/test_speechrail_events.py \
  tests/asr/test_speechrail_realtime.py tests/asr/test_speechrail_pipecat.py \
  tests/asr/test_proxy_contract.py tests/test_pipeline.py tests/test_meeting_session.py \
  -q --no-cov
```

- [ ] **Step 2: 运行项目静态门禁**

```bash
uv run --extra dev ruff check src tests
uv run --extra dev mypy src
git diff --check
```

- [ ] **Step 3: 审查 import 与协议所有权**

确认 `asr/` 不导入 `meeting.models`，两个 ASR adapter 不直接读取 raw event 字段，`transport.py` 仍是通用 envelope 的唯一验证者，`speechrail/tts.py` 未被改写。

## 完成标准

- [ ] SpeechRail transcription event 只有一个 event-specific decoder。
- [ ] ASR port 只暴露 `ASRWindow`，不依赖 meeting entity。
- [ ] meeting mapper 只有一个，segment UUID 与既有对账保持稳定。
- [ ] browser payload、Pipecat frame、meeting final window 和错误语义不变。
- [ ] ADR-0011/0012 与 SpeechRail public contract 未修改。
