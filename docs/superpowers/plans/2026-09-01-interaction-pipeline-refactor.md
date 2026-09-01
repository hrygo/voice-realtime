---
title: "交互管道依赖注入与防回声重构实施计划"
status: draft
type: execution_plan
date: 2026-09-01
owners: ["voice-realtime-core"]
related_documents:
  - "docs/architecture/实时语音交互与字幕-方案与最佳实践.md"
  - "docs/decisions/0012-speechrail-realtime-tts.md"
---

# Interaction Pipeline Dependency Refactor Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task, with a review checkpoint after each commit.

**Goal:** 把 `build_pipeline` 中可替换的外部构造收敛为 typed factory bundle，并把防回声算法从 Pipecat frame orchestration 中提取为可测试策略，同时保持 L1/L2、barge-in、processor 顺序和现有调用签名兼容。

**Architecture:** L1 `EchoSuppressionProcessor` 仍在音频域，使用独立 adaptive energy gate 控制物理闭麦/插话/relock；L2 `SelfEchoFilter` 仍在 STT 文本与 user aggregator 之间，使用独立 text policy。`PipelineFactories` 只注入 LocalAudioTransport、SpeechRail STT、LM Studio LLM、SpeechRail TTS、VAD/SmartTurn 构造；`build_pipeline` 保留当前 keyword seams 并只装配 processor 顺序。

**Tech Stack:** Python 3.12、Pipecat、asyncio、SpeechRail Realtime TTS、LM Studio、pytest、Ruff、mypy。

---

## 执行边界

- 工作目录：`/Users/hrygo/Documents/voice-realtime`
- 可在 meeting 计划之后独立执行；不得与其他任务同时修改 `interaction/pipeline.py`、`interaction/session.py` 或 `ui/runtime.py`。
- 当前层级事实：`EchoSuppressionProcessor`（约 405 行起）是 L1 音频/RMS/物理闭麦与 barge-in；`SelfEchoFilter`（约 352 行起）是 L2 文本相似度兜底。不得反转命名或合并两层。
- 保持 processor 顺序：input → L1 echo → STT → L2 self-echo → user aggregator → LLM → bot text recorder → TTS → TTS state observer → output → assistant aggregator。
- 保持 `build_pipeline(settings, *, transport=None, context=None, persona=None, audio_queue=None, echo_state=None, echo_buffer=None, stt_factory=None)` 的现有调用兼容；只允许新增可选 keyword。
- `InteractionSession` 的 custom `pipeline_factory` 与 `stt_factory` test seam 必须保留；`UIRuntime` 仍是生产 composition root。
- ADR-0012 规定生产 TTS 只走 SpeechRail Realtime；`tts_bridge_url` 只解析到 2026-10-31，factory refactor 不得重新使用旧 bridge。
- 不改变 16 kHz input、24 kHz mono PCM16 output、VAD/SmartTurn 参数、tail hangover、echo threshold、cancel/resume 或物理输出 owner。

## 目标文件

- Create: `src/voice_realtime/interaction/echo.py`
- Create: `src/voice_realtime/interaction/pipeline_dependencies.py`
- Modify: `src/voice_realtime/interaction/pipeline.py`
- Modify: `src/voice_realtime/interaction/session.py`
- Modify: `src/voice_realtime/ui/runtime.py`
- Create: `tests/test_pipeline_dependencies.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_interaction_session.py`
- Modify: `tests/test_speechrail_tts_service.py`

## Task 1: 固化现有 processor 与构造 seam

**Files:**

- Create: `tests/test_pipeline_dependencies.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_interaction_session.py`
- Modify: `tests/test_speechrail_tts_service.py`

- [ ] **Step 1: 增加顺序和资源保护测试**

断言 13 个含 source/sink processor 的当前顺序、L1/L2 位置、两层共享 `EchoState` 但不共享策略对象、L2 与 `BotTextRecorder` 共享 `EchoTextBuffer`、SpeechRail TTS client 在 pipeline stop/cancel 时关闭。

- [ ] **Step 2: 增加构造兼容测试**

覆盖当前 `transport=`、`stt_factory=`、`audio_queue=`、custom `pipeline_factory`；新增 factory bundle 后这些调用仍工作且显式参数优先于 bundle default。

- [ ] **Step 3: 运行基线**

```bash
uv run --extra dev pytest tests/test_pipeline.py tests/test_interaction_session.py \
  tests/test_speechrail_tts_service.py -q --no-cov
```

预期：现有基线通过；新 dependency tests 因目标接口尚不存在而失败。

## Task 2: 分别提取 L1 energy gate 与 L2 text policy

**Files:**

- Create: `src/voice_realtime/interaction/echo.py`
- Modify: `src/voice_realtime/interaction/pipeline.py`
- Modify: `tests/test_pipeline_dependencies.py`
- Modify: `tests/test_pipeline.py`

- [ ] **Step 1: 定义 L1 专用 gate**

```python
@dataclass(frozen=True, slots=True)
class EnergyGateDecision:
    allow_audio: bool
    barge_in_started: bool = False
    relocked: bool = False


class AdaptiveEnergyGate:
    def configure(self, *, gain: float, required_frames: int) -> None: ...
    def reset(self) -> None: ...
    def observe(self, rms: float) -> EnergyGateDecision: ...

    @property
    def barge_in_active(self) -> bool: ...
```

移动当前 warmup、peak envelope、fast/slow EMA、hot streak、quiet relock 算法；数值和分支保持一致。`EchoSuppressionProcessor` 仍决定 TTS/Bot/Interruption/InputAudio frame 的传播与日志。

同时把纯 `EchoState`、`EchoTextBuffer`、text normalization 与 PCM16 RMS primitive 移入 `interaction/echo.py`。`pipeline.py` 在本轮继续 re-export `EchoState`/`EchoTextBuffer` 以保持当前 import seam；`InteractionSession` 在 Task 4 改为从新模块直接导入。

- [ ] **Step 2: 定义 L2 专用 policy**

```python
@dataclass(frozen=True, slots=True)
class SelfEchoPolicy:
    min_ratio: float
    min_chars: int
    tail_hangover_secs: float

    def should_drop(
        self,
        text: str,
        *,
        now: float,
        protect_next_transcript: bool,
        echo_state: EchoState,
        buffer: EchoTextBuffer,
    ) -> bool: ...
```

空/纯标点归一化、interrupt 后首条保护和文本相似度留在 L2；RMS/energy 不进入该接口。`SelfEchoFilter` 仍处理 FrameProcessor lifecycle 和 `_interruption_pending`。

- [ ] **Step 3: 运行并提交 echo 拆分**

```bash
uv run --extra dev pytest tests/test_pipeline_dependencies.py tests/test_pipeline.py -q --no-cov
uv run --extra dev ruff check src/voice_realtime/interaction/echo.py \
  src/voice_realtime/interaction/pipeline.py tests/test_pipeline_dependencies.py tests/test_pipeline.py
uv run --extra dev mypy src/voice_realtime/interaction
git add src/voice_realtime/interaction/echo.py src/voice_realtime/interaction/pipeline.py \
  tests/test_pipeline_dependencies.py tests/test_pipeline.py
git commit -m "refactor: separate echo policies"
```

## Task 3: 建立 PipelineFactories，保持旧 keyword seam

**Files:**

- Create: `src/voice_realtime/interaction/pipeline_dependencies.py`
- Modify: `src/voice_realtime/interaction/pipeline.py`
- Modify: `tests/test_pipeline_dependencies.py`
- Modify: `tests/test_pipeline.py`

- [ ] **Step 1: 定义 typed factory bundle**

```python
@dataclass(frozen=True, slots=True)
class PipelineFactories:
    transport_factory: TransportFactory
    stt_factory: ConversationSTTFactory
    llm_factory: LLMFactory
    tts_factory: TTSFactory
    vad_factory: VADFactory
    smart_turn_factory: SmartTurnFactory


def default_pipeline_factories(settings: InteractionSettings) -> PipelineFactories:
    ...
```

每个 `*Factory` 是参数明确的 Protocol，不使用 `Callable[..., Any]`。default factory 继续构造 LocalAudioTransport、SpeechRail conversation STT、LM Studio service、SpeechRail TTS service 和现有 analyzer。

- [ ] **Step 2: 扩展而非替换 build signature**

新增 `factories: PipelineFactories | None = None`。解析优先级为显式 `transport` / `stt_factory` → provided bundle → `default_pipeline_factories(settings)`。保留 `context/persona/audio_queue/echo_state/echo_buffer`。

- [ ] **Step 3: 让 build_pipeline 只装配**

把 endpoint/model/auth 参数翻译放入 default factories；`build_pipeline` 只创建共享 echo state/buffer、调用 factories、配置 aggregator 并返回现有顺序的 `Pipeline`。不要把 settings 本身隐藏为全局 singleton。

- [ ] **Step 4: 运行并提交 factories**

```bash
uv run --extra dev pytest tests/test_pipeline_dependencies.py tests/test_pipeline.py tests/test_speechrail_tts_service.py -q --no-cov
uv run --extra dev ruff check src/voice_realtime/interaction/pipeline_dependencies.py \
  src/voice_realtime/interaction/pipeline.py tests/test_pipeline_dependencies.py
uv run --extra dev mypy src/voice_realtime/interaction
git add src/voice_realtime/interaction/pipeline_dependencies.py src/voice_realtime/interaction/pipeline.py \
  tests/test_pipeline_dependencies.py tests/test_pipeline.py tests/test_speechrail_tts_service.py
git commit -m "refactor: inject interaction pipeline factories"
```

## Task 4: 贯通 InteractionSession 与 UIRuntime composition

**Files:**

- Modify: `src/voice_realtime/interaction/session.py`
- Modify: `src/voice_realtime/ui/runtime.py`
- Modify: `tests/test_interaction_session.py`
- Modify: `tests/test_pipeline_dependencies.py`

- [ ] **Step 1: 给 session 增加可选 bundle**

`InteractionSession.__init__` 新增 `pipeline_factories: PipelineFactories | None = None`；start 时把它加入现有 `pipeline_kwargs`。custom `pipeline_factory` 若不接受该参数，仅在 bundle 非 None 时才传入，保持既有测试 fake 兼容。

- [ ] **Step 2: 在 UIRuntime 创建生产 bundle**

`UIRuntime` 调用 `default_pipeline_factories(settings.interaction)` 并注入 session；现有 `conversation_stt_factory` 可作为 bundle 的 STT override，不能在 session 内再次构造另一份。

- [ ] **Step 3: 运行 session/runtime 回归并提交**

```bash
uv run --extra dev pytest tests/test_pipeline_dependencies.py tests/test_pipeline.py \
  tests/test_interaction_session.py tests/test_ui_server.py -q --no-cov
uv run --extra dev ruff check src/voice_realtime/interaction/session.py src/voice_realtime/ui/runtime.py \
  tests/test_interaction_session.py
uv run --extra dev mypy src
git add src/voice_realtime/interaction/session.py src/voice_realtime/ui/runtime.py \
  tests/test_interaction_session.py tests/test_pipeline_dependencies.py
git commit -m "refactor: compose interaction pipeline dependencies"
```

## Task 5: 配置 sunset 与完整门禁

- [ ] **Step 1: 验证 legacy bridge 不进入 factory**

在 `tests/test_pipeline_dependencies.py` 断言即使 settings 能解析 `tts_bridge_url`，default TTS factory 仍只读取 `speechrail_realtime_url/model/voice/language/api_key`。本计划不删除兼容配置；删除日期仍为 2026-10-31。

- [ ] **Step 2: 运行聚焦与项目门禁**

```bash
uv run --extra dev pytest tests/test_pipeline_dependencies.py tests/test_pipeline.py \
  tests/test_interaction_session.py tests/test_speechrail_tts_service.py tests/test_config.py \
  -q --no-cov
uv run --extra dev ruff check src tests
uv run --extra dev mypy src
git diff --check
```

## 完成标准

- [ ] L1 adaptive energy gate 与 L2 text policy 是两个独立接口，算法和位置未反转。
- [ ] `build_pipeline` 现有 keyword 调用兼容，新增 bundle 可完整注入外部构造。
- [ ] `InteractionSession` 和 `UIRuntime` 显式传递依赖，无隐式旧 TTS bridge。
- [ ] processor 顺序、audio format、VAD/SmartTurn、barge-in 和 cleanup 测试通过。
- [ ] ADR-0012 sunset 与 SpeechRail-only production path 未改变。
