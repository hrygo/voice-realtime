# ASR 后端可插拔架构评估与前置设计

## 1. 文档状态

- 设计日期：2026-08-24（Asia/Shanghai）
- 代码证据：当前工作树 `HEAD`，代码知识图 generation `2026-08-24T10:44:36Z`
- 外部候选：`QwenAudio/Fun-ASR` `53a56d80667320b44a7dd779f5bf8c024b6c30a8`
- 目标：先建立可验证、可回退的 ASR 适配边界，再执行
  [`Fun-ASR 与现有 ASR 后端科学对比测试方案`](../../Fun-ASR与现有ASR后端科学对比测试方案.md)

## 2. 结论先行

当前架构只具备**局部、静态的后端选择能力**，不具备跨字幕、会议和交互助手的一致可插拔能力。

具体来说：

1. 字幕与会议链路可通过 `SubtitleSettings.backend` 在 WhisperLiveKit 已注册的
   `qwen3-streaming`、`funasr`、`auto` 之间选择，但必须重启服务；其中 `funasr` 实际是
   SenseVoiceSmall 的 LocalAgreement 适配器，不是 Fun-ASR-Nano。
2. `SubtitleProxy` 虽允许注入 `stream_factory`，却仍把类型、URL、事件、EOF 和错误语义绑定到
   `SubtitleStream`/WhisperLiveKit，属于测试接缝，不是稳定的后端契约。
3. `TranscriptNormalizer` 直接读取 WhisperLiveKit 的 `lines`、`buffer_transcription`、`speaker`
   字段。Fun-ASR 官方实时协议使用 `START`/二进制 PCM/`STOP`，并返回 `sentences`、`partial`、
   `is_final`，不能直接接入。
4. 交互助手在 `build_pipeline()` 中固定构造 Pipecat `FunASRSTTService(device="cpu")`，模型默认为
   SenseVoiceSmall；没有 `stt_backend`、注册表或工厂。
5. `UIRuntime` 直接构造 `SubtitleProxy` 和 `build_pipeline`。当前没有统一的能力探测、冷切换事务、
   后端身份记录或指标标签。

因此，直接把 Fun-ASR-Nano 塞进现有 `backend="funasr"` 会造成语义误导，并把协议差异扩散到
会议持久化和 UI。正确做法是先引入领域契约和适配器，再以冷切换方式接入候选后端。

## 3. 当前结构与耦合点

```text
AudioHub
├─ interaction audio queue
│  └─ build_pipeline()
│     └─ FunASRSTTService(SenseVoiceSmall, CPU)   [硬编码]
└─ subtitle PCM sink
   └─ SubtitleProxy
      └─ SubtitleStream(/asr?language=...&mode=full)
         └─ WhisperLiveKit
            ├─ qwen3-streaming                    [配置选择]
            └─ funasr = SenseVoiceSmall           [配置选择，命名易误解]

MeetingSession
└─ gateway.begin_capture()/finish_capture()
   └─ SubtitleProxy
      └─ TranscriptNormalizer(WLK full snapshot)  [协议耦合]
```

### 3.1 支持度矩阵

| 能力 | 字幕 | 会议 | 交互助手 | 结论 |
|---|---:|---:|---:|---|
| 启动时选择 WLK 内置后端 | 部分 | 部分 | 否 | 只覆盖同一 vendor 内部实现 |
| 选择 Fun-ASR-Nano | 否 | 否 | 否 | 当前 `funasr` 是 SenseVoiceSmall |
| 不改消费者即可更换协议 | 否 | 否 | 否 | 领域事件与 WLK 原始 JSON 混合 |
| 后端能力探测 | 否 | 否 | 否 | 时间戳、热词、分人能力无法前置校验 |
| 活动会话内热切换 | 否 | 否 | 否 | 本设计也不建议支持 |
| 会话间冷切换 | 仅重启配置 | 仅重启配置 | 否 | 缺少统一事务和后端身份记录 |
| A/B 可复现标记 | 否 | 否 | 否 | 数据记录中缺少模型/运行时/参数指纹 |

### 3.2 已存在且应保留的良好边界

- `AudioHub` 已提供单源采集和有界扇出，不应让每个 ASR 后端自行打开麦克风。
- `MeetingSession` 只依赖 `begin_capture`、`finish_capture`、监听器等行为，天然接近端口接口。
- `TranscriptWindow`/`NormalizedSegment` 已是不可变领域对象，可作为后端无关的 confirmed 输出。
- `SubtitleProxy` 已实现捕获租约、重连 epoch、EOF 冲刷、客户端背压和 SRT 行为，这些应继续由应用层
  拥有，而不是下沉到某个模型 SDK。
- 现有 `stream_factory` 和 `pipeline_factory` 可作为无行为变化迁移的切入点。

## 4. 目标架构

### 4.1 原则

1. **领域事件优先**：vendor JSON 只存在于适配器内部，会议和 UI 只消费规范事件。
2. **能力显式化**：时间戳、partial、热词、语言、说话人和 EOF 都通过能力描述，不靠字符串猜测。
3. **配置采用判别联合**：每个后端只接收自己的字段，禁止把 Qwen3 参数传给 Fun-ASR。
4. **会话期间不可切换**：切换仅允许在 `idle`，且采用 stop → preflight → start → health-check 的
   冷切换事务；失败恢复原配置。
5. **先平移后扩展**：先用 WLK 适配器证明零行为变化，再接 Fun-ASR-Nano。
6. **运行时与模型分离**：`Fun-ASR-Nano-2512` 是模型，PyTorch、官方 WebSocket、GGUF 是不同运行时，
   必须分别记录和比较。
7. **不把 benchmark 需求污染生产协议**：观测字段放在事件元数据和运行清单中，不写入会议正文。

### 4.2 双端口模型

字幕/会议需要长连接、partial、confirmed、EOF 与重连；交互助手需要 Pipecat FrameProcessor 和
端点等待语义。二者不能伪装成完全相同的接口，因此共享配置与能力描述，但使用两个窄端口。

```python
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ASREventKind = Literal["ready", "snapshot", "final", "error"]

@dataclass(frozen=True)
class ASRCapabilities:
    languages: frozenset[str]
    supports_partial: bool
    supports_segment_timestamps: bool
    supports_word_timestamps: bool
    supports_hotwords: bool
    supports_speaker_labels: bool
    supports_native_diarization: bool
    supports_eof_flush: bool

@dataclass(frozen=True)
class ASREvent:
    kind: ASREventKind
    window: TranscriptWindow | None = None
    error_code: str | None = None
    error_message: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

class StreamingTranscriber(Protocol):
    backend_id: str
    capabilities: ASRCapabilities

    async def connect(self) -> None: ...
    async def send_audio(self, chunk: bytes) -> None: ...
    def events(self) -> AsyncIterator[ASREvent]: ...
    async def finish(self) -> TranscriptWindow: ...
    async def close(self) -> None: ...

class ConversationSTTFactory(Protocol):
    backend_id: str
    capabilities: ASRCapabilities

    def create_processor(self, *, sample_rate: int, language: str) -> object: ...
```

一个 snapshot 同时承载 confirmed segments 和易失 partial，避免把 WLK 同一 full snapshot 拆开后
产生顺序歧义。`finish()` 统一封装 WLK 的空 PCM/`ready_to_stop` 和 Fun-ASR 的
`STOP`/`is_final`。上层不再判断 vendor 消息。`metadata` 只允许诊断信息，不作为持久化领域字段。

浏览器当前依赖 WLK 风格 full snapshot。为保持兼容，在应用边界增加纯函数
`legacy_subtitle_payload(window: TranscriptWindow) -> dict[str, Any]`，由统一领域窗口重建既有
`lines`/`buffer_transcription` payload；禁止把 vendor raw JSON 直接广播给浏览器。这样既保持外部
行为，又确保新后端不会被迫伪造 WLK 内部对象。

### 4.3 配置模型

后端 ID 使用无歧义名称：

```text
wlk-qwen3-streaming
wlk-sensevoice
funasr-nano-pytorch
funasr-nano-ws
funasr-nano-gguf
```

建议配置层使用 `kind` 判别联合，并把当前环境变量作为兼容输入映射到新配置：

```python
class WLKQwen3Config(BaseModel):
    kind: Literal["wlk-qwen3-streaming"]
    model_dir: Path
    device: Literal["mps", "cpu"]
    # qwen3 streaming 专属窗口参数

class WLKSenseVoiceConfig(BaseModel):
    kind: Literal["wlk-sensevoice"]
    model_dir: Path
    device: Literal["cpu"] = "cpu"

class FunASRNanoWSConfig(BaseModel):
    kind: Literal["funasr-nano-ws"]
    url: AnyUrl
    model_id: str
    runtime: Literal["pytorch", "vllm"]
    hotwords: tuple[str, ...] = ()
```

兼容期内：

- `VR_SUBTITLE_BACKEND=qwen3-streaming` 映射为 `wlk-qwen3-streaming`。
- `VR_SUBTITLE_BACKEND=funasr` 映射为 `wlk-sensevoice`，并记录一次弃用警告。
- 不复用 `funasr` 作为 Fun-ASR-Nano 的 ID。
- 旧字段至少保留一个发布周期；新增字段只做加法，不改变现有控制 WebSocket 响应结构。

### 4.4 注册表与构造边界

```python
StreamingFactory = Callable[[ASRProfile, ASRSessionContext], StreamingTranscriber]
ConversationFactory = Callable[[ASRProfile], ConversationSTTFactory]

class ASRBackendRegistry:
    def register_streaming(self, backend_id: str, factory: StreamingFactory) -> None: ...
    def create_streaming(self, profile: ASRProfile) -> StreamingTranscriber: ...
    def register_conversation(self, backend_id: str, factory: ConversationFactory) -> None: ...
    def create_conversation(self, profile: ASRProfile) -> ConversationSTTFactory: ...
```

- `UIRuntime` 接收已构造的 registry/factory，不再直接依赖 `SubtitleStream`。
- `SubtitleProxy` 改为接收 `Callable[[], StreamingTranscriber]`，并通过
  `legacy_subtitle_payload()` 广播兼容快照；保留现有捕获租约、重连和 SRT 所有权。
- `build_pipeline()` 从 `ConversationSTTFactory` 获取处理器；第一阶段仍只注册 SenseVoice 实现。
- registry 必须拒绝重复 ID、未知 ID、工厂身份不一致、语言不支持，以及会议缺少 segment 时间戳或
  EOF 能力，并返回结构化错误码。speaker labels 作为显式能力记录，但为兼容无分人的单说话人会议，
  不作为通用 meeting 硬门禁；科学分人实验单独要求该能力。

### 4.5 适配器责任

| 适配器 | 输入协议 | 输出责任 | 明确不负责 |
|---|---|---|---|
| `WLKStreamingAdapter` | `/asr` + PCM + 空 PCM EOF | 将 full snapshot 规范成 `TranscriptWindow`；等待 ready | 浏览器 payload、模型选择、会议持久化 |
| `FunASRNanoWSAdapter` | `START`/PCM/`STOP` | 映射 `sentences`、`partial`、`is_final`；验证单调时间轴 | CAM++/Sortformer 决策 |
| `PipecatSenseVoiceFactory` | Pipecat service | 保持当前 CPU、语言、ITN、TTFS 设置 | 字幕/会议协议 |
| `BenchmarkOfflineAdapter` | 文件/固定 PCM chunks | 产生原始 hypothesis 与耗时/资源样本 | 生产运行时切换 |

Fun-ASR-Nano 开源 checkpoint 的可靠字符/词时间戳必须经过独立能力门禁；若门禁失败，禁止伪造精确
时间戳或直接进入会议 confirmed 主链路。说话人分离继续固定为 Sortformer，以隔离 ASR 变量；官方
CAM++ 只能作为后续独立实验因素。

### 4.6 冷切换状态机

```text
idle
  └─ request_switch(profile)
       ├─ validate config/model files
       ├─ preflight capabilities
       ├─ stop current service
       ├─ start candidate
       ├─ wait ready + smoke PCM + final
       ├─ commit active profile
       └─ on failure: stop candidate → restore previous → emit switch_failed
```

- `assistant` 或 `meeting` 活动时返回 `MODE_CONFLICT`。
- 切换不删除模型、不修改下载权限、不写音频。
- 只有最终 health-check 成功后才更新 active profile；失败不会留下半切换状态。
- 第一阶段只提供启动配置冷切换，不向 UI 暴露按钮；科学测试通过独立 benchmark 入口选择 profile。

## 5. 与科学对比测试的接口

前置架构至少要提供以下稳定能力，测试方案才能避免每个后端一套不可比脚本：

1. `ASRRunManifest`：代码提交、模型 ID/哈希、运行时、设备、精度、参数、语料 manifest 哈希。
2. 固定音频切块回放器：同一轮对所有后端发送完全相同的 s16le/16kHz/mono chunks。
3. 统一事件记录：arrival time、audio cursor、partial、confirmed/final、错误和资源采样。
4. 后端能力探测：不支持的时间戳/热词/流式能力记为 `unsupported`，不以零分或推断值填充。
5. 原始输出保留：适配后的领域事件与 vendor 原始响应分开存储，便于审计。

## 6. 迁移顺序与回退

### Phase 0：契约与基线冻结

- 为现有 WLK 行为补齐 contract tests。
- 冻结当前 `/asr`、EOF、重连 epoch、会议窗口和交互 STT 行为。
- 不改变默认配置和运行拓扑。

### Phase 1：WLK 平移

- 引入领域契约、registry 和 `WLKStreamingAdapter`。
- `SubtitleProxy` 改从工厂创建 transcriber。
- 新旧全量快照、EOF、SRT、会议持久化测试必须完全等价。

### Phase 2：测试基础设施

- 实现离线与 1× 实时回放 runner、manifest、JSONL 事件和统计汇总。
- 先跑 Qwen3 对自身的重复性与配对一致性测试，证明 harness 不引入偏差。

### Phase 3：Fun-ASR-Nano 候选

- 接入官方 WS/PyTorch 与 GGUF benchmark adapter。
- 通过能力和科学门禁后，才允许注册为字幕/会议候选。
- vLLM 在本机因 CUDA/Ampere 前提排除，不做伪兼容。

### Phase 4：交互助手选择

- 把固定 `FunASRSTTService` 移入 `PipecatSenseVoiceFactory`。
- 只有 Fun-ASR 在交互延迟、端点完整性和回声双防线测试通过后，才增加第二个 conversation factory。

任一阶段均可回退到上一阶段；默认后端保持 `wlk-qwen3-streaming`（字幕/会议）和
Pipecat SenseVoice（交互），直到对比方案给出明确晋级结论。

## 7. 验收标准

1. 现有默认配置下，外部 WebSocket payload、会议数据库内容、SRT、控制协议均无行为变化。
2. vendor JSON 不越过适配器进入 `MeetingSession`。
3. registry 的未知/重复/能力不匹配均有确定错误码和测试。
4. 活动会议或交互会话期间拒绝切换；空闲冷切换失败可恢复旧后端。
5. EOF、最终窗口、重连 epoch、gap 事件和零音频持久化约束全部保留。
6. 每个 benchmark 结果可追溯到代码、模型、语料、参数与设备指纹。
7. 全量质量门禁保持通过，且真实 `assistant → meeting → idle → assistant` 闭环不回退。

## 8. 证据边界

- 自研代码结论来自知识图 Tier 2 验证和定点源码读取；所依赖路径无已记录图缺口。
- `tools/WhisperLiveKit` 被知识图按规则排除，相关结论来自
  `parse_args.py` 与 `funasr_backend.py` 的直接源码读取。
- PostgreSQL migration 的图解析缺口与本设计结论无关。
- Fun-ASR 能力以固定 commit、[官方仓库](https://github.com/QwenAudio/Fun-ASR/tree/53a56d80667320b44a7dd779f5bf8c024b6c30a8)、
  [官方实时服务](https://github.com/QwenAudio/Fun-ASR/blob/53a56d80667320b44a7dd779f5bf8c024b6c30a8/serve_realtime_ws.py)
  和 [Nano 模型卡](https://huggingface.co/FunAudioLLM/Fun-ASR-Nano-2512) 为依据；运行性能仍必须在本机实测。
