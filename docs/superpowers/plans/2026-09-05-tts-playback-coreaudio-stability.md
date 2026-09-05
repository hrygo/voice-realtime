# Sona TTS Playback CoreAudio Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Sona 在 macOS 上以输出设备原生采样率和显式 40 ms PyAudio 缓冲播放 TTS，降低长播报期间的 CoreAudio overload 与爆音风险，并以三轮实测验证是否达到零 Overload、无可闻爆音。

**Architecture:** 新增一个薄的本机输出适配器，继续复用 Pipecat 的排队、流式重采样、打断和 frame 语义，只替换 PyAudio 输出流创建。SpeechRail 仍提供 24 kHz mono PCM16；其合成片段边界修复由 SpeechRail 仓库独立维护和部署。

**Tech Stack:** Python 3.12、uv、Pipecat 1.7、PyAudio/PortAudio、CoreAudio、pytest、ruff、mypy

**Spec:** `docs/superpowers/specs/2026-09-05-tts-playback-coreaudio-stability-design.md`

## Global Constraints

- Python 保持 `>=3.12,<3.13`，不得升级 Pipecat、PyAudio 或其他依赖。
- 不修改 `.venv/site-packages`；第三方代码只作为行为依据。
- SpeechRail 公共协议和 24 kHz PCM 输入格式不变；Sona 输入采集继续保持 16 kHz。
- 只有输出侧改为当前设备原生采样率，并由 Pipecat 执行 24 kHz→原生采样率流式重采样。
- 默认缓冲固定为 40 ms，可配置范围为 20–100 ms。
- 初始化失败必须 fail-fast；回退只通过显式配置开关，禁止静默回退。
- 不记录 TTS 文本、PCM、API key 或设备 UID。
- 工作区已有改动属于其所有者；不得覆盖、stash、revert 或混入本方案提交。
- 实施基线必须包含已提交的动态音色闭环 `e548631`；本方案不得回退或改写其 UI 代理职责。
- Sona 真实扬声器验收的前置条件是 SpeechRail 自己的计划已通过真实 PCM 验收；其文档位于 SpeechRail 仓库：`docs/superpowers/plans/2026-09-05-tts-segment-boundary-stability.md`。

---

### Task 1: 稳定输出配置契约

**Files:**
- Modify: `src/sona/config/interaction.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `InteractionSettings.audio_output_stable_enabled: bool`
- Produces: `InteractionSettings.audio_output_buffer_ms: int`
- Environment: `SONA_INTERACTION_AUDIO_OUTPUT_STABLE_ENABLED`
- Environment: `SONA_INTERACTION_AUDIO_OUTPUT_BUFFER_MS`

- [x] **Step 0: 核对实施基线与目标文件所有权**

Run:

```bash
git merge-base --is-ancestor e548631 HEAD
git status --short
git diff -- src/sona/config/interaction.py tests/test_config.py
```

Expected: HEAD 包含 `e548631`，两个目标代码文件无未提交改动；本计划文档自身可以处于未跟踪或
已修改状态。若目标代码文件 dirty，停止本 Task，由其所有者先独立收口，不 stash 或 revert。

- [x] **Step 1: 写默认值与范围失败测试**

在 `tests/test_config.py` 增加：

```python
def test_interaction_stable_audio_output_defaults() -> None:
    settings = InteractionSettings(_env_file=None)
    assert settings.audio_output_stable_enabled is True
    assert settings.audio_output_buffer_ms == 40


@pytest.mark.parametrize("buffer_ms", [0, 19, 101, 1_000])
def test_interaction_rejects_unsafe_audio_output_buffer(buffer_ms: int) -> None:
    with pytest.raises(ValidationError):
        InteractionSettings(_env_file=None, audio_output_buffer_ms=buffer_ms)
```

- [x] **Step 2: 运行测试并确认失败**

Run:

```bash
uv run pytest \
  tests/test_config.py::test_interaction_stable_audio_output_defaults \
  tests/test_config.py::test_interaction_rejects_unsafe_audio_output_buffer \
  -q
```

Expected: FAIL，两个字段尚不存在。

- [x] **Step 3: 增加配置字段**

在 `InteractionSettings` 的输入设备字段之前加入：

```python
audio_output_stable_enabled: bool = Field(
    default=True,
    description="是否使用设备原生采样率和显式缓冲的稳定本机输出传输",
)
audio_output_buffer_ms: int = Field(
    default=40,
    ge=20,
    le=100,
    description="PyAudio 输出缓冲毫秒数；默认与 Pipecat 40ms 输出块对齐",
)
```

- [x] **Step 4: 运行配置测试**

Run:

```bash
uv run pytest tests/test_config.py -q
```

Expected: 全部 PASS。只保留改动，尚不提交；Sona 要求完整质量门禁通过后再提交。

---

### Task 2: 稳定 PyAudio 输出适配器

**Files:**
- Create: `src/sona/audio/local_output.py`
- Create: `tests/test_local_audio_output.py`

**Interfaces:**
- Consumes: Pipecat `LocalAudioTransport`、`LocalAudioOutputTransport`、`LocalAudioTransportParams`、`BaseOutputTransport`
- Produces: `OutputDeviceProfile`
- Produces: `resolve_output_device_profile(py_audio, *, output_device_index: int | None, buffer_ms: int) -> OutputDeviceProfile`
- Produces: `StableLocalAudioOutputTransport`
- Produces: `StableLocalAudioTransport`

- [x] **Step 1: 写输出设备 profile 失败测试**

创建 `tests/test_local_audio_output.py`：

```python
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from pipecat.frames.frames import StartFrame
from pipecat.transports.local.audio import LocalAudioTransportParams

from sona.audio.local_output import (
    StableLocalAudioOutputTransport,
    resolve_output_device_profile,
)


class FakePyAudio:
    def __init__(self, *, sample_rate: float = 48_000.0, channels: int = 2) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.open_kwargs: dict[str, Any] | None = None
        self.stream = MagicMock()

    def get_default_output_device_info(self) -> dict[str, object]:
        return {
            "index": 2,
            "name": "Test Speaker",
            "defaultSampleRate": self.sample_rate,
            "maxOutputChannels": self.channels,
        }

    def get_device_info_by_index(self, index: int) -> dict[str, object]:
        return {**self.get_default_output_device_info(), "index": index}

    def get_format_from_width(self, width: int) -> str:
        assert width == 2
        return "pcm16"

    def is_format_supported(self, rate: int, **kwargs: object) -> bool:
        assert rate == round(self.sample_rate)
        assert kwargs["output_channels"] == 1
        return True

    def open(self, **kwargs: Any) -> MagicMock:
        self.open_kwargs = kwargs
        return self.stream


@pytest.mark.parametrize(
    ("sample_rate", "expected_frames"),
    [(48_000.0, 1_920), (44_100.0, 1_764)],
)
def test_output_profile_uses_native_rate_and_40ms_buffer(
    sample_rate: float,
    expected_frames: int,
) -> None:
    profile = resolve_output_device_profile(
        FakePyAudio(sample_rate=sample_rate),
        output_device_index=None,
        buffer_ms=40,
    )
    assert profile.sample_rate == round(sample_rate)
    assert profile.frames_per_buffer == expected_frames
    assert profile.buffer_ms == 40


@pytest.mark.parametrize(
    "fake",
    [FakePyAudio(sample_rate=1.0), FakePyAudio(sample_rate=48_000.0, channels=0)],
)
def test_output_profile_rejects_invalid_device(fake: FakePyAudio) -> None:
    with pytest.raises(RuntimeError, match="audio_output_device_invalid"):
        resolve_output_device_profile(fake, output_device_index=None, buffer_ms=40)
```

- [x] **Step 2: 运行 profile 测试并确认失败**

Run:

```bash
uv run pytest tests/test_local_audio_output.py -q
```

Expected: collection FAIL，`sona.audio.local_output` 尚不存在。

- [x] **Step 3: 实现 profile 解析**

在 `src/sona/audio/local_output.py` 加入：

```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pyaudio
from pipecat.frames.frames import StartFrame
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.local.audio import (
    LocalAudioOutputTransport,
    LocalAudioTransport,
    LocalAudioTransportParams,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OutputDeviceProfile:
    device_index: int
    device_name: str
    sample_rate: int
    frames_per_buffer: int
    buffer_ms: int


def resolve_output_device_profile(
    py_audio: Any,
    *,
    output_device_index: int | None,
    buffer_ms: int,
) -> OutputDeviceProfile:
    info = (
        py_audio.get_default_output_device_info()
        if output_device_index is None
        else py_audio.get_device_info_by_index(output_device_index)
    )
    device_index = int(info["index"])
    sample_rate = round(float(info["defaultSampleRate"]))
    channels = int(info["maxOutputChannels"])
    if channels < 1 or not 8_000 <= sample_rate <= 192_000:
        raise RuntimeError("audio_output_device_invalid")
    try:
        py_audio.is_format_supported(
            sample_rate,
            output_device=device_index,
            output_channels=1,
            output_format=pyaudio.paInt16,
        )
    except ValueError as exc:
        raise RuntimeError("audio_output_format_unsupported") from exc
    return OutputDeviceProfile(
        device_index=device_index,
        device_name=str(info.get("name", "unknown")),
        sample_rate=sample_rate,
        frames_per_buffer=round(sample_rate * buffer_ms / 1_000),
        buffer_ms=buffer_ms,
    )
```

- [x] **Step 4: 写显式流参数与失败清理测试**

在 `tests/test_local_audio_output.py` 追加：

```python
@pytest.mark.asyncio
async def test_stable_output_opens_once_with_explicit_native_buffer() -> None:
    fake = FakePyAudio()
    params = LocalAudioTransportParams(
        audio_out_enabled=True,
        audio_out_sample_rate=48_000,
        audio_out_channels=1,
        output_device_index=2,
    )
    profile = resolve_output_device_profile(fake, output_device_index=2, buffer_ms=40)
    output = StableLocalAudioOutputTransport(fake, params, profile=profile)

    await output.start(StartFrame())
    await output.start(StartFrame())

    assert fake.open_kwargs == {
        "format": "pcm16",
        "channels": 1,
        "rate": 48_000,
        "frames_per_buffer": 1_920,
        "output": True,
        "output_device_index": 2,
        "start": False,
    }
    fake.stream.start_stream.assert_called_once_with()


@pytest.mark.asyncio
async def test_stable_output_closes_stream_when_start_fails() -> None:
    fake = FakePyAudio()
    fake.stream.start_stream.side_effect = OSError("start failed")
    params = LocalAudioTransportParams(
        audio_out_enabled=True,
        audio_out_sample_rate=48_000,
        audio_out_channels=1,
        output_device_index=2,
    )
    profile = resolve_output_device_profile(fake, output_device_index=2, buffer_ms=40)
    output = StableLocalAudioOutputTransport(fake, params, profile=profile)

    with pytest.raises(OSError, match="start failed"):
        await output.start(StartFrame())

    fake.stream.close.assert_called_once_with()
    assert output._out_stream is None
```

- [x] **Step 5: 实现稳定输出 transport**

在同一模块追加：

```python
class StableLocalAudioOutputTransport(LocalAudioOutputTransport):
    def __init__(
        self,
        py_audio: Any,
        params: LocalAudioTransportParams,
        *,
        profile: OutputDeviceProfile,
    ) -> None:
        super().__init__(py_audio, params)
        self._profile = profile

    async def start(self, frame: StartFrame) -> None:
        await BaseOutputTransport.start(self, frame)
        if self._out_stream:
            return
        stream = None
        try:
            stream = self._py_audio.open(
                format=self._py_audio.get_format_from_width(2),
                channels=self._params.audio_out_channels,
                rate=self._profile.sample_rate,
                frames_per_buffer=self._profile.frames_per_buffer,
                output=True,
                output_device_index=self._profile.device_index,
                start=False,
            )
            stream.start_stream()
        except Exception:
            if stream is not None:
                stream.close()
            raise
        self._out_stream = stream
        logger.info(
            "audio-output: device=%r index=%d rate=%d buffer=%d frames (%dms)",
            self._profile.device_name,
            self._profile.device_index,
            self._profile.sample_rate,
            self._profile.frames_per_buffer,
            self._profile.buffer_ms,
        )
        await self.set_transport_ready(frame)


class StableLocalAudioTransport(LocalAudioTransport):
    def __init__(self, params: LocalAudioTransportParams, *, buffer_ms: int) -> None:
        super().__init__(params)
        self._profile = resolve_output_device_profile(
            self._pyaudio,
            output_device_index=params.output_device_index,
            buffer_ms=buffer_ms,
        )
        self._params.audio_out_sample_rate = self._profile.sample_rate

    def output(self) -> StableLocalAudioOutputTransport:
        if not self._output:
            self._output = StableLocalAudioOutputTransport(
                self._pyaudio,
                self._params,
                profile=self._profile,
            )
        return self._output


__all__ = [
    "OutputDeviceProfile",
    "StableLocalAudioOutputTransport",
    "StableLocalAudioTransport",
    "resolve_output_device_profile",
]
```

- [x] **Step 6: 运行适配器测试**

Run:

```bash
uv run pytest tests/test_local_audio_output.py -q
```

Expected: 全部 PASS，不打开真实音频设备。保留改动，Task 4 完整门禁后再提交。

---

### Task 3: 交互工厂接线与显式回退

**Files:**
- Modify: `src/sona/interaction/pipeline_dependencies.py`
- Modify: `tests/test_pipeline_dependencies.py`

**Interfaces:**
- Consumes: Task 1 配置字段和 Task 2 `StableLocalAudioTransport`
- Produces: 默认稳定输出；`audio_output_stable_enabled=False` 时恢复上游 `LocalAudioTransport`

- [x] **Step 1: 写默认路径与回退失败测试**

在 `tests/test_pipeline_dependencies.py` 增加：

```python
def test_default_transport_factory_uses_stable_output(settings: InteractionSettings) -> None:
    factories = default_pipeline_factories(settings)
    with patch(
        "sona.interaction.pipeline_dependencies.StableLocalAudioTransport"
    ) as stable_transport:
        factories.transport_factory(settings=settings, audio_in_enabled=False)

    params = stable_transport.call_args.args[0]
    assert params.audio_in_enabled is False
    assert params.audio_out_enabled is True
    assert params.audio_out_sample_rate == 24_000
    assert stable_transport.call_args.kwargs == {
        "buffer_ms": settings.audio_output_buffer_ms
    }


def test_transport_factory_can_roll_back_to_upstream_local_audio() -> None:
    settings = InteractionSettings(
        _env_file=None,
        audio_output_stable_enabled=False,
    )
    factories = default_pipeline_factories(settings)
    with (
        patch("sona.interaction.pipeline_dependencies.LocalAudioTransport") as upstream,
        patch(
            "sona.interaction.pipeline_dependencies.StableLocalAudioTransport"
        ) as stable,
    ):
        factories.transport_factory(settings=settings, audio_in_enabled=False)

    upstream.assert_called_once()
    stable.assert_not_called()
    assert upstream.call_args.args[0].audio_out_sample_rate == 24_000
```

- [x] **Step 2: 运行测试并确认失败**

Run:

```bash
uv run pytest \
  tests/test_pipeline_dependencies.py::test_default_transport_factory_uses_stable_output \
  tests/test_pipeline_dependencies.py::test_transport_factory_can_roll_back_to_upstream_local_audio \
  -q
```

Expected: FAIL，工厂尚未引用稳定 transport。

- [x] **Step 3: 接入稳定 transport**

在 `pipeline_dependencies.py` 导入 `StableLocalAudioTransport`，保持 factory 签名不变，将 transport
返回逻辑改为：

```python
params = LocalAudioTransportParams(
    audio_in_enabled=audio_in_enabled,
    audio_out_enabled=True,
    audio_in_sample_rate=settings.sample_rate,
    audio_out_sample_rate=TTS_OUTPUT_SAMPLE_RATE,
    input_device_index=input_device,
)
if not settings.audio_output_stable_enabled:
    return LocalAudioTransport(params)
return StableLocalAudioTransport(
    params,
    buffer_ms=settings.audio_output_buffer_ms,
)
```

`TransportFactory` 返回类型继续使用 `LocalAudioTransport`，因为稳定 transport 是其子类。

- [x] **Step 4: 运行交互回归**

Run:

```bash
uv run pytest \
  tests/test_pipeline_dependencies.py \
  tests/test_pipeline.py \
  tests/test_speechrail_tts.py \
  tests/test_speechrail_tts_service.py \
  -q
```

Expected: 全部 PASS；处理器顺序、回声双层防线和 24 kHz TTS frame 不变。保留改动，Task 4
完整门禁后再提交。

---

### Task 4: 运行手册、完整质量门禁与提交

**Files:**
- Create: `docs/operations/语音助手-TTS-爆音排查与验收.md`
- Modify: `docs/README.md`
- Modify: `docs/architecture/实时语音交互与字幕-方案与最佳实践.md`
- Include: `docs/superpowers/specs/2026-09-05-tts-playback-coreaudio-stability-design.md`
- Include: `docs/superpowers/plans/2026-09-05-tts-playback-coreaudio-stability.md`

**Interfaces:**
- Consumes: Tasks 1–3 实现与测试
- Produces: 可重复诊断、验收、回退手册和单一 Sona 修复提交

- [x] **Step 1: 编写运行手册**

手册必须给出以下 CoreAudio 计数命令：

```bash
TTS_ACCEPT_START="$(date '+%Y-%m-%d %H:%M:%S')"
# 在 Sona UI 连续播放 60–90 秒验收文本。
TTS_ACCEPT_END="$(date '+%Y-%m-%d %H:%M:%S')"
/usr/bin/log show \
  --start "$TTS_ACCEPT_START" \
  --end "$TTS_ACCEPT_END" \
  --style compact \
  --predicate 'process == "coreaudiod" AND eventMessage CONTAINS[c] "HALS_OverloadMessage: Overload"' \
  | rg -c 'HALS_OverloadMessage: Overload'
```

写明无匹配时 `rg -c` 可能退出 1，验收依据是计数 `0`。手册还必须包含 `/api/services` 队列字段、
日志隐私边界、三轮长文本验收和以下回退配置：

```text
SONA_INTERACTION_AUDIO_OUTPUT_STABLE_ENABLED=false
```

回退后执行：

```bash
scripts/sona-ctl.sh restart -d
```

- [x] **Step 2: 更新文档索引和架构说明**

在 `docs/README.md` 增加手册链接；在架构文档 TTS 小节记录：源 PCM 固定 24 kHz，Sona 输出默认
解析设备原生采样率并使用 40 ms 显式缓冲，配置开关只用于故障恢复。SpeechRail 实现细节只引用
其仓库文档，不复制到 Sona。

- [x] **Step 3: 运行 focused gate**

Run:

```bash
uv run pytest \
  tests/test_local_audio_output.py \
  tests/test_pipeline_dependencies.py \
  tests/test_config.py \
  tests/test_pipeline.py \
  tests/test_speechrail_tts.py \
  tests/test_speechrail_tts_service.py \
  -q
uv run ruff check \
  src/sona/audio/local_output.py \
  src/sona/config/interaction.py \
  src/sona/interaction/pipeline_dependencies.py \
  tests/test_local_audio_output.py \
  tests/test_config.py \
  tests/test_pipeline_dependencies.py
uv run mypy src/
git diff --check
```

Expected: pytest 0 failures，ruff、mypy、`git diff --check` 退出 0。

- [x] **Step 4: 运行完整质量门禁**

Run:

```bash
SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/
uv run mypy src/
uv run ruff check src/ tests/
(cd ui && npm test -- --run)
(cd ui && npm run build)
```

Expected: 后端满足分支覆盖率门禁；mypy/ruff 全绿；前端测试与生产构建成功。若失败，按文件与
测试精确归因，不得回退 `e548631` 已提交的动态音色闭环。

- [x] **Step 5: 精确暂存并提交 Sona 修复**

Run:

```bash
git add \
  src/sona/audio/local_output.py \
  src/sona/config/interaction.py \
  src/sona/interaction/pipeline_dependencies.py \
  tests/test_local_audio_output.py \
  tests/test_config.py \
  tests/test_pipeline_dependencies.py \
  docs/README.md \
  docs/architecture/实时语音交互与字幕-方案与最佳实践.md \
  docs/operations/语音助手-TTS-爆音排查与验收.md \
  docs/superpowers/specs/2026-09-05-tts-playback-coreaudio-stability-design.md \
  docs/superpowers/plans/2026-09-05-tts-playback-coreaudio-stability.md
git diff --staged
git commit -m "fix(audio): 稳定语音助手本机播放链路"
```

Expected: staged diff 只包含本方案文件，不改写 UI 自定义音色代理或其他用户改动。

---

### Task 5: Sona 真实扬声器联合验收

**Files/State:**
- Runtime change: 重启 `sona-ui`
- Preserve: Sona 私有环境配置、SpeechRail 运行态、会议数据

**Interfaces:**
- Consumes: Task 4 Sona 提交；已通过独立真实 PCM 验收的 SpeechRail 版本
- Produces: 三轮 CoreAudio 与人工听感验收记录

**执行回填（2026-09-05 14:04 CST）：**
- SpeechRail `1.6.9` 的 `/health`、`/readyz`、`/v1/models`、`/v1/voices` 均已实测成功；
- Sona 重启日志已确认 `StableLocalAudioOutputTransport` 使用本机输出设备的 `48000 Hz / 1920 frames / 40 ms`；
- Sona 后端 `1001 passed, 14 skipped`（总覆盖率 `81.91%`），`ruff`、`mypy`、前端 `279` 项测试及生产构建均通过；
- 两次长文本压力样本的 CoreAudio Overload、应用丢块与大于 `200 ms` 的源分片间隙均为 `0`，但因验收脚本把上游 `TTSStoppedFrame` 误作物理播放结束，样本之间可能重叠，故不计入正式三轮；
- 补跑时运行态已进入会议录制，按模式互斥约束不得切换或占用语音助手。Step 3/4 保持未完成，待会议结束后按物理播放完成信号重新执行，不据此声明人工听感通过。

- [x] **Step 1: 核对 SpeechRail 前置状态**

Run:

```bash
curl -s http://127.0.0.1:8201/health
curl -s http://127.0.0.1:8201/readyz
curl -s http://127.0.0.1:8201/v1/models
curl -s http://127.0.0.1:8201/v1/voices
```

Expected: 四个端点成功，且 SpeechRail 自己的边界稳定性计划已有真实 PCM 验收记录。本 Task 不安装、
回退或修改 SpeechRail。

- [x] **Step 2: 重启 Sona 并核对稳定输出身份**

此步骤改变 Sona 运行态，只有获得当前用户明确授权后执行：

```bash
scripts/sona-ctl.sh restart -d
scripts/sona-ctl.sh status
rg -n 'audio-output: device=.*rate=.*buffer=.*frames' runtime/logs/ui.log | tail -n 1
curl -s http://127.0.0.1:8100/api/services
```

Expected: 依赖服务均就绪；日志显示当前设备原生采样率，本机内置扬声器为 48,000 Hz 时应显示
`1920 frames (40ms)`；应用队列丢块为 0。

- [ ] **Step 3: 执行三轮固定长文本验收**

每轮使用同一段 60–90 秒中文文本：冷启动第一轮、连续热播报第二轮、含逗号/句号/数字/英文缩写
的第三轮。每轮单独执行运行手册的 CoreAudio 计数，并保存以下结果：

```text
CoreAudio overload count                         0
audio_hub.pipecat.dropped_chunks                 0
interaction.dropped_chunks                       0
tts.source_chunk_gaps_over_200ms                 0
可闻爆音/噼啪/失速/变调/尾字截断                 0
首包与总播报时长相对基线恶化                     <= 10%
```

- [ ] **Step 4: 验证自然结束和用户打断**

自然结束三次、播报中用户打断三次。每次确认无爆音，`echo-state` 不会永久停在播报态，下一轮输入
能正常触发 LLM 与 TTS。只有全部硬指标满足时才能声明播放爆音修复完成。

- [ ] **Step 5: Sona 失败时回退播放适配器**

在仓库外私有配置设置：

```text
SONA_INTERACTION_AUDIO_OUTPUT_STABLE_ENABLED=false
```

Run:

```bash
scripts/sona-ctl.sh restart -d
scripts/sona-ctl.sh status
```

Expected: Sona 恢复上游 `LocalAudioTransport` 行为；SpeechRail、会议数据和系统音频设备均不变。
