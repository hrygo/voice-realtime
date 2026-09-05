# Sona TTS 播放 CoreAudio 稳定性修复设计

## 背景与已确认事实

2026-09-05 11:40:09 至 11:44:05 的语音助手实测窗口内，macOS `coreaudiod` 记录了 55 次
`HALS_OverloadMessage: Overload possibly due to safety violation`；同一窗口前后的空闲阶段为 0 次。
语音助手使用的内置扬声器上下文由 `sona-ui` 进程打开。

同一轮应用侧观测显示：SpeechRail 源块中位间隔约 25 ms、最大约 44 ms、超过 200 ms 的间隔
为 0，各音频队列丢块为 0。真实 PCM 冒烟也未发现 PCM16 削顶。因此 Sona 连续爆音的主根因
定位为本机播放输出链的实时调度过载，而不是 TTS 源断流或削顶。

当前 Sona 把 SpeechRail 的 24 kHz PCM 直接配置为 PyAudio 输出采样率，而当前内置扬声器原生
采样率为 48 kHz。Pipecat 的 `LocalAudioOutputTransport` 打开流时没有显式传入
`frames_per_buffer`，由 PortAudio/CoreAudio 自行协商。长播报和本地推理并发时，这一组合缺少
稳定的设备原生采样率与明确缓冲边界。

SpeechRail 自身的合成片段首块淡入修复已迁移到 SpeechRail 仓库维护，路径为：

- `docs/superpowers/specs/2026-09-05-tts-segment-boundary-stability-design.md`
- `docs/superpowers/plans/2026-09-05-tts-segment-boundary-stability.md`

本设计只拥有 Sona 播放侧职责。

## 目标

1. 以当前输出设备的原生采样率打开 PyAudio 输出流。
2. 显式设置与 Pipecat 逻辑输出块一致的 40 ms 播放缓冲。
3. 保留 SpeechRail 24 kHz 输入格式，由 Pipecat 在输出前统一流式重采样。
4. 提供单一显式回退开关，可恢复现有上游 `LocalAudioTransport`。
5. 用 CoreAudio 日志、应用队列诊断、真实长播报和打断场景联合验收。

## 非目标

- 不修改 SpeechRail 源码、模型、voice、PCM 协议或部署。
- 不修改 `.venv/site-packages` 中的 Pipecat 源码。
- 不在每个 40/80 ms 数据块上做淡入淡出，避免周期性音量调制。
- 不重构输入采集、回声抑制、会议模式或物理输出采集。
- 不实现默认输出设备热切换监听；设备变更在重启交互管道后生效。
- 不把 `readyz=200` 或单个测试退出码当作音频质量验收。

## 方案比较

### 方案 A：Sona 自有稳定本机输出适配器（采用）

新增一个薄适配层，复用 Pipecat 的排队、流式重采样、打断和 frame 语义，只接管 PyAudio 输出流
创建。适配层显式使用设备原生采样率和 `frames_per_buffer`，并由测试锁定受保护成员兼容性。

### 方案 B：修改 Pipecat 安装包（拒绝）

直接修改 `.venv/site-packages/pipecat/transports/local/audio.py` 无法稳定发布，重建环境后会丢失，
也会污染第三方依赖边界。

### 方案 C：只增加音量 fade（拒绝）

fade 可以缓解逻辑开始/结束 click，但无法解决 CoreAudio I/O deadline miss，不适合作为持续爆音的
主修复。

## 架构

```text
SpeechRailTTSService / TTSAudioRawFrame
  24 kHz / mono / PCM16
          │
          ▼
Pipecat BaseOutputTransport stream resampler
  24 kHz → 当前输出设备原生采样率
          │
          ▼
StableLocalAudioOutputTransport
  frames_per_buffer = native_rate × buffer_ms / 1000
  默认 48 kHz × 40 ms = 1920 frames
          │
          ▼
PyAudio blocking output → CoreAudio → 扬声器
```

## 组件与接口

新增内部模块 `sona.audio.local_output`：

```python
@dataclass(frozen=True, slots=True)
class OutputDeviceProfile:
    device_index: int
    device_name: str
    sample_rate: int
    frames_per_buffer: int
    buffer_ms: int


def resolve_output_device_profile(
    py_audio: pyaudio.PyAudio,
    *,
    output_device_index: int | None,
    buffer_ms: int,
) -> OutputDeviceProfile: ...


class StableLocalAudioOutputTransport(LocalAudioOutputTransport): ...


class StableLocalAudioTransport(LocalAudioTransport): ...
```

`resolve_output_device_profile()` 使用指定设备或默认输出设备的 `defaultSampleRate`。采样率必须位于
8,000–192,000 Hz，设备至少具有一个输出通道；否则 fail-fast。缓冲帧数按
`round(sample_rate * buffer_ms / 1000)` 计算。

`StableLocalAudioOutputTransport.start()` 只替代上游流创建：调用 `BaseOutputTransport.start()`
初始化重采样与分块状态，以 `start=False` 打开 PyAudio，显式传入 `frames_per_buffer`，再启动流并
发布 transport ready。写入、关闭、异常传播沿用上游实现。

`StableLocalAudioTransport.output()` 创建稳定输出处理器；输入继续复用上游
`LocalAudioInputTransport`，保持 headless 与 UI 两个入口一致。

## 配置

在 `InteractionSettings` 增加：

```python
audio_output_stable_enabled: bool = True
audio_output_buffer_ms: int = Field(default=40, ge=20, le=100)
```

对应环境变量：

- `SONA_INTERACTION_AUDIO_OUTPUT_STABLE_ENABLED=true`
- `SONA_INTERACTION_AUDIO_OUTPUT_BUFFER_MS=40`

关闭开关后恢复当前 `LocalAudioTransport` + 24 kHz 输出行为。稳定输出初始化失败时不得静默降级，
否则会掩盖同一故障。

## 数据流与状态

1. 交互管道构造 `StableLocalAudioTransport` 时查询默认/指定输出设备。
2. 根据设备原生采样率计算 40 ms 缓冲帧数，并更新 transport 输出采样率。
3. 输出处理器收到 `StartFrame` 后以解析好的设备、采样率和帧数打开流。
4. Pipecat 将 24 kHz `TTSAudioRawFrame` 流式重采样到设备原生采样率。
5. `BaseOutputTransport` 按默认四个 10 ms 逻辑块排队，PyAudio 以同样 40 ms 帧数阻塞写入。
6. 管道停止或取消时复用上游清理逻辑关闭输出流。

## 错误处理与隐私

- 无默认设备、无输出通道、采样率异常：交互管道构造失败，返回稳定错误。
- PyAudio 不支持 PCM16 mono 原生采样率：启动失败，不回退到隐式 24 kHz 协商。
- 流打开或启动失败：关闭已创建流并重新抛出。
- 启动日志仅记录设备名称、索引、采样率、缓冲帧数和毫秒数。
- 不记录 API key、TTS 文本、PCM 或设备 UID。

## 自动化验收

- 配置默认启用，缓冲默认 40 ms，20–100 ms 之外校验失败。
- 48 kHz 解析为 1,920 帧，44.1 kHz 解析为 1,764 帧。
- 输出流以原生采样率、mono PCM16、显式帧数和 `start=False` 打开，只启动一次。
- 打开/启动失败时关闭临时 stream，不设置 `_out_stream`。
- 关闭开关时返回原 `LocalAudioTransport`，启用时返回稳定适配器。
- 现有 TTS、pipeline、回声抑制、配置和 UI 测试全部通过。

## 本机验收

在已部署通过独立验收的 SpeechRail 版本后，使用同一段 60–90 秒中文长文本完成三轮播报：

- 每轮 CoreAudio overload 计数为 0；
- `audio_hub.pipecat.dropped_chunks == 0`；
- `interaction.dropped_chunks == 0`；
- `tts.source_chunk_gaps_over_200ms == 0`；
- 无可闻爆音、噼啪、失速、变调或尾字截断；
- 首包和总播报时长相对修复前基线恶化不超过 10%；
- 自然结束三次、用户打断三次后均可继续下一轮交互。

## 回退

在仓库外私有配置设置：

```text
SONA_INTERACTION_AUDIO_OUTPUT_STABLE_ENABLED=false
```

再使用 `scripts/sona-ctl.sh restart -d` 重启。回退不修改 SpeechRail、系统音频设备、会议数据或
其他 Sona 模式。

## 剩余风险

若 CoreAudio overload 已为 0，但只在用户打断瞬间稳定复现单次 click，则另立 Sona 小范围设计：
只在整次 TTS 的开始、自然结束和打断处增加 5 ms look-behind fade，不对每个流式数据块处理。
