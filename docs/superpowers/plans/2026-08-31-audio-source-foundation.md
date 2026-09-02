# Audio Source Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可承载麦克风与后续物理输出 Helper 的统一音频帧、采集配置、来源生命周期、有界路由和服务端真实能量链路，同时移除浏览器 `getUserMedia`。

**Architecture:** 保留 `AudioHub` 的麦克风专用职责，在其外新增 `AudioFrame`、`CaptureProfile`、`AudioSource` 与 `AudioSourceRouter`。P0 Router 只提交单来源，明确拒绝尚未具备 Mixer 的 dual 配置；服务端从实际 PCM 计算能量，经现有 latest-only runtime state 广播给前端，前端能量服务退化为纯状态分发器，不再打开浏览器音频设备。

**Tech Stack:** Python 3.12、asyncio、Pydantic v2、pytest/pytest-asyncio、React 19、TypeScript 5.8、Vitest 3。

**Spec:** `docs/superpowers/specs/2026-08-31-physical-output-audio-capture-design.md`

**Status:** P0 已完成并通过全量质量门禁；物理输出 Helper、output-only 与 dual meeting 仍属于 P1–P3。

## Global Constraints

- Python 严格保持 `>=3.12,<3.13`，不新增 Python 或 npm 依赖。
- 统一路由边界固定为 16 kHz、mono、signed 16-bit little-endian、512 samples/frame。
- 同一时刻最多一个重型 PCM 推理 owner；本计划不启动第二套 ASR。
- `AudioHub` 继续只负责麦克风，不改造成系统输出 Hub。
- 所有 PCM 仅驻留有界内存，不进入日志、数据库、journal、SRT 或临时文件。
- v1 控制命令保持兼容；新增 runtime snapshot 字段必须具有默认值且前端可选。
- P0 不伪装 physical-output 已可用，也不接受 dual 进入运行态；对应能力由后续 Helper/Mixer 计划解锁。
- 浏览器不得调用 `navigator.mediaDevices.getUserMedia` 或创建用于输入采集的 `AudioContext`。
- 所有行为变更遵循 RED → GREEN → REFACTOR，并在每个任务通过聚焦测试后形成原子提交。

---

## File Structure

### 新增文件

- `src/sona/audio/frame.py`：统一格式、来源枚举、flags 与不可变 `AudioFrame`。
- `src/sona/audio/profile.py`：严格 `CaptureProfile`、来源布局校验和 v1 `audio_source` 投影。
- `src/sona/audio/source.py`：`AudioSource` Protocol、健康快照和 `MicrophoneSource` 适配器。
- `src/sona/audio/router.py`：两阶段单来源路由、有界队列和丢帧诊断。
- `src/sona/audio/levels.py`：PCM16 RMS 归一化、来源能量和发布节流。
- `tests/test_audio_frame.py`：帧与 profile 合法/非法边界。
- `tests/test_audio_source.py`：麦克风来源状态机、时间戳和背压。
- `tests/test_audio_router.py`：prepare/commit/abort、单来源传递与 bounded drop-oldest。
- `tests/test_audio_levels.py`：能量计算、静音和节流。
- `ui/src/services/audioEnergyService.test.ts`：服务端能量分发及浏览器零采集回归。

### 修改文件

- `src/sona/audio/__init__.py`：导出稳定公共类型。
- `src/sona/audio/hub.py`：只增加只读 `running` 状态，供适配器判断所有权。
- `src/sona/ui/protocol.py`：新增默认安全的 `AudioLevelsSnapshot`。
- `src/sona/ui/runtime.py`：增加实际 PCM level sink、节流广播与诊断。
- `tests/test_audio_hub.py`：验证 `running` 生命周期。
- `tests/test_runtime.py`：验证 level sink、snapshot 和静音广播。
- `tests/test_control.py`：验证控制快照默认能量契约。
- `ui/src/protocol.ts`：增加可选音频能量结构和运行时校验。
- `ui/src/hooks/useCommandSocket.ts`：将权威快照能量投递到前端能量服务。
- `ui/src/hooks/useCommandSocket.test.ts`：验证同 revision 能量更新仍被接受。
- `ui/src/services/audioEnergyService.ts`：删除 Web Audio/getUserMedia，改为纯内存发布器。
- `ui/src/components/UnifiedAcousticWaveform.tsx`：修正语义注释，继续消费同一订阅 API。

---

### Task 1: 统一 `AudioFrame` 与 `CaptureProfile`

**Files:**
- Create: `src/sona/audio/frame.py`
- Create: `src/sona/audio/profile.py`
- Create: `tests/test_audio_frame.py`
- Modify: `src/sona/audio/__init__.py`

**Interfaces:**
- Produces: `AudioSourceKind`, `AudioSourceRole`, `AudioFrameFlag`, `AudioFrame`。
- Produces: `CaptureMode`, `CaptureSourceSpec`, `CaptureProfile`。
- `AudioFrame.pcm` 始终是归一化 PCM；`CaptureProfile.legacy_audio_source` 返回 `microphone | physical_output | mixed`。

- [x] **Step 1: 写帧格式和 profile 的失败测试**

```python
from uuid import UUID

import pytest
from pydantic import ValidationError

from sona.audio.frame import (
    AudioFrame,
    AudioFrameFlag,
    AudioSourceKind,
    AudioSourceRole,
)
from sona.audio.profile import CaptureProfile


def test_audio_frame_accepts_normalized_pcm() -> None:
    frame = AudioFrame(
        capture_id=UUID("00000000-0000-0000-0000-000000000001"),
        source_id="mic-main",
        source_kind=AudioSourceKind.MICROPHONE,
        source_role=AudioSourceRole.NEAR_END,
        device_generation=0,
        sequence=7,
        host_time_ns=123_000,
        pcm=b"\x00\x00" * 512,
    )
    assert frame.samples_per_channel == 512
    assert frame.duration_ns == 32_000_000


def test_audio_frame_rejects_wrong_payload_size() -> None:
    with pytest.raises(ValueError, match="PCM payload"):
        AudioFrame(
            capture_id=UUID(int=1),
            source_id="mic-main",
            source_kind=AudioSourceKind.MICROPHONE,
            source_role=AudioSourceRole.NEAR_END,
            device_generation=0,
            sequence=0,
            host_time_ns=1,
            pcm=b"\x00\x00",
        )


def test_audio_frame_allows_empty_eof_only() -> None:
    frame = AudioFrame(
        capture_id=UUID(int=1),
        source_id="mic-main",
        source_kind=AudioSourceKind.MICROPHONE,
        source_role=AudioSourceRole.NEAR_END,
        device_generation=0,
        sequence=9,
        host_time_ns=1,
        flags=AudioFrameFlag.END_OF_STREAM,
        pcm=b"",
    )
    assert frame.flags & AudioFrameFlag.END_OF_STREAM


def test_capture_profile_defaults_to_legacy_microphone() -> None:
    profile = CaptureProfile.microphone()
    assert profile.legacy_audio_source == "microphone"
    assert profile.sources[0].role is AudioSourceRole.NEAR_END


def test_capture_profile_rejects_invalid_dual_layout() -> None:
    with pytest.raises(ValidationError, match="dual"):
        CaptureProfile.model_validate(
            {
                "mode": "dual",
                "sources": [
                    {"kind": "microphone", "role": "near_end"},
                    {"kind": "microphone", "role": "near_end"},
                ],
            }
        )
```

- [x] **Step 2: 运行测试，确认导入失败**

Run: `uv run pytest tests/test_audio_frame.py -q --no-cov`

Expected: FAIL，提示 `sona.audio.frame` 尚不存在。

- [x] **Step 3: 实现不可变帧、枚举与严格 profile**

```python
# frame.py 的公共形态
class AudioSourceKind(StrEnum):
    MICROPHONE = "microphone"
    PHYSICAL_OUTPUT = "physical_output"


class AudioSourceRole(StrEnum):
    NEAR_END = "near_end"
    FAR_END = "far_end"


class AudioFrameFlag(IntFlag):
    NONE = 0
    DISCONTINUITY = auto()
    SILENCE_FILL = auto()
    END_OF_STREAM = auto()


@dataclass(frozen=True, slots=True)
class AudioFrame:
    capture_id: UUID
    source_id: str
    source_kind: AudioSourceKind
    source_role: AudioSourceRole
    device_generation: int
    sequence: int
    host_time_ns: int
    pcm: bytes
    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2
    samples_per_channel: int = 512
    flags: AudioFrameFlag = AudioFrameFlag.NONE
```

```python
# profile.py 的公共形态
class CaptureMode(StrEnum):
    SINGLE = "single"
    DUAL = "dual"


class CaptureSourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: AudioSourceKind
    role: AudioSourceRole


class CaptureProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: CaptureMode = CaptureMode.SINGLE
    follow_default_output: bool = True
    exclude_own_audio: bool = True
    sources: tuple[CaptureSourceSpec, ...]

    @classmethod
    def microphone(cls) -> "CaptureProfile":
        return cls(
            sources=(
                CaptureSourceSpec(
                    kind=AudioSourceKind.MICROPHONE,
                    role=AudioSourceRole.NEAR_END,
                ),
            )
        )
```

实现 `model_validator(mode="after")`：single 恰好一个来源；dual 恰好一个 near-end microphone 和一个 far-end physical-output；重复 kind/role 拒绝。实现 EOF payload 例外及所有非负字段校验。

- [x] **Step 4: 运行聚焦测试并修正类型导出**

Run: `uv run pytest tests/test_audio_frame.py -q --no-cov`

Expected: PASS，5 tests passed。

- [x] **Step 5: 运行静态检查并提交**

Run: `uv run mypy src/sona/audio/frame.py src/sona/audio/profile.py`

Run: `uv run ruff check src/sona/audio/frame.py src/sona/audio/profile.py tests/test_audio_frame.py`

Commit:

```bash
git add src/sona/audio/__init__.py src/sona/audio/frame.py src/sona/audio/profile.py tests/test_audio_frame.py
git commit -m "feat(audio): 建立统一音频帧与采集配置"
```

---

### Task 2: `AudioSource` 生命周期与麦克风适配器

**Files:**
- Create: `src/sona/audio/source.py`
- Create: `tests/test_audio_source.py`
- Modify: `src/sona/audio/hub.py`
- Modify: `src/sona/audio/__init__.py`
- Modify: `tests/test_audio_hub.py`

**Interfaces:**
- Consumes: Task 1 的 `AudioFrame`、`AudioSourceKind`、`AudioSourceRole`。
- Produces: `AudioSourceState`, `AudioSourceHealth`, runtime-checkable `AudioSource`, `MicrophoneSource`。
- `MicrophoneSource` 适配现有已启动或待启动的 `AudioHub`，不夺取 Hub 的启动/停止所有权。

- [x] **Step 1: 写来源状态机失败测试**

```python
import asyncio
from uuid import UUID

import pytest

from sona.audio.hub import AudioHub
from sona.audio.source import AudioSourceState, MicrophoneSource


async def test_microphone_source_prepare_commit_and_frame() -> None:
    hub = AudioHub()
    source = MicrophoneSource(hub, source_id="mic-main", queue_size=2)
    await source.prepare(UUID(int=1))
    assert source.state is AudioSourceState.READY

    await source.commit()
    hub._loop = asyncio.get_running_loop()
    hub._running = True
    hub._start_sink_workers()
    hub._on_chunk_received(b"\x01\x00" * 512)
    frame = await anext(source.frames())

    assert frame.source_id == "mic-main"
    assert frame.sequence == 0
    assert frame.host_time_ns > 0
    await source.stop()
    assert source.state is AudioSourceState.STOPPED


async def test_microphone_source_abort_removes_sink() -> None:
    hub = AudioHub()
    source = MicrophoneSource(hub, source_id="mic-main")
    await source.prepare(UUID(int=1))
    await source.abort()
    assert source.state is AudioSourceState.STOPPED
    assert not hub._sinks


async def test_microphone_source_drops_oldest_when_full() -> None:
    hub = AudioHub()
    source = MicrophoneSource(hub, source_id="mic-main", queue_size=1)
    await source.prepare(UUID(int=1))
    await source.commit()
    await source._receive_pcm(b"\x01\x00" * 512)
    await source._receive_pcm(b"\x02\x00" * 512)
    frame = await anext(source.frames())
    assert frame.pcm == b"\x02\x00" * 512
    assert source.health().dropped_frames == 1
    await source.stop()


async def test_microphone_source_rejects_commit_before_prepare() -> None:
    source = MicrophoneSource(AudioHub(), source_id="mic-main")
    with pytest.raises(RuntimeError, match="ready"):
        await source.commit()
```

- [x] **Step 2: 运行测试，确认来源模块缺失**

Run: `uv run pytest tests/test_audio_source.py -q --no-cov`

Expected: FAIL，提示 `sona.audio.source` 尚不存在。

- [x] **Step 3: 实现 Protocol、健康快照与麦克风适配器**

```python
class AudioSourceState(StrEnum):
    STOPPED = "stopped"
    PREPARING = "preparing"
    READY = "ready"
    ACTIVE = "active"
    DEGRADED = "degraded"
    RECOVERING = "recovering"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AudioSourceHealth:
    state: AudioSourceState
    queued_frames: int
    dropped_frames: int
    last_sequence: int | None
    last_host_time_ns: int | None


@runtime_checkable
class AudioSource(Protocol):
    @property
    def kind(self) -> AudioSourceKind:
        raise NotImplementedError

    @property
    def role(self) -> AudioSourceRole:
        raise NotImplementedError

    @property
    def state(self) -> AudioSourceState:
        raise NotImplementedError

    async def prepare(self, capture_id: UUID) -> None:
        raise NotImplementedError

    async def commit(self) -> None:
        raise NotImplementedError

    async def abort(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    def frames(self) -> AsyncIterator[AudioFrame]:
        raise NotImplementedError

    def health(self) -> AudioSourceHealth:
        raise NotImplementedError
```

`MicrophoneSource.prepare()` 注册唯一 sink，`commit()` 只允许从 ready 进入 active；接收回调用 `time.monotonic_ns()`、递增 sequence、构造 `AudioFrame`，队满时 drop-oldest；`abort()` 和 `stop()` 幂等移除 sink 并清空队列。

`AudioHub` 只新增：

```python
@property
def running(self) -> bool:
    return self._running
```

- [x] **Step 4: 运行来源与 Hub 聚焦测试**

Run: `uv run pytest tests/test_audio_source.py tests/test_audio_hub.py -q --no-cov`

Expected: PASS。

- [x] **Step 5: 运行静态检查并提交**

Run: `uv run mypy src/sona/audio/source.py src/sona/audio/hub.py`

Run: `uv run ruff check src/sona/audio/source.py src/sona/audio/hub.py tests/test_audio_source.py tests/test_audio_hub.py`

Commit:

```bash
git add src/sona/audio/__init__.py src/sona/audio/hub.py src/sona/audio/source.py tests/test_audio_hub.py tests/test_audio_source.py
git commit -m "feat(audio): 增加麦克风音频源生命周期"
```

---

### Task 3: 两阶段有界 `AudioSourceRouter`

**Files:**
- Create: `src/sona/audio/router.py`
- Create: `tests/test_audio_router.py`
- Modify: `src/sona/audio/__init__.py`

**Interfaces:**
- Consumes: `CaptureProfile`、`AudioSource`、`AudioFrame`。
- Produces: `AudioSourceRouter`, `RouterHealth`, `UnsupportedCaptureProfileError`。
- P0 明确支持 single profile；dual 返回稳定错误，不启动任一来源。

- [x] **Step 1: 写 Router 两阶段和背压失败测试**

```python
import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from uuid import UUID

import pytest

from sona.audio.frame import AudioFrame, AudioSourceKind, AudioSourceRole
from sona.audio.profile import CaptureProfile
from sona.audio.router import AudioSourceRouter, UnsupportedCaptureProfileError
from sona.audio.source import AudioSourceHealth, AudioSourceState


VALID_DUAL = {
    "mode": "dual",
    "sources": [
        {"kind": "microphone", "role": "near_end"},
        {"kind": "physical_output", "role": "far_end"},
    ],
}


def make_frame(capture_id: UUID, *, sequence: int, sample: int) -> AudioFrame:
    return AudioFrame(
        capture_id=capture_id,
        source_id="mic-main",
        source_kind=AudioSourceKind.MICROPHONE,
        source_role=AudioSourceRole.NEAR_END,
        device_generation=0,
        sequence=sequence,
        host_time_ns=sequence + 1,
        pcm=sample.to_bytes(2, "little", signed=True) * 512,
    )


async def wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1.0):
        while not predicate():
            await asyncio.sleep(0)


@dataclass
class FakeSource:
    kind: AudioSourceKind
    role: AudioSourceRole
    state: AudioSourceState = AudioSourceState.STOPPED

    def __post_init__(self) -> None:
        self.queue: asyncio.Queue[AudioFrame] = asyncio.Queue()

    async def prepare(self, capture_id: UUID) -> None:
        self.capture_id = capture_id
        self.state = AudioSourceState.READY

    async def commit(self) -> None:
        self.state = AudioSourceState.ACTIVE

    async def abort(self) -> None:
        self.state = AudioSourceState.STOPPED

    async def stop(self) -> None:
        self.state = AudioSourceState.STOPPED

    async def frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            yield await self.queue.get()

    def health(self) -> AudioSourceHealth:
        return AudioSourceHealth(self.state, self.queue.qsize(), 0, None, None)


async def test_router_prepare_commit_and_forward_single_source() -> None:
    source = FakeSource(AudioSourceKind.MICROPHONE, AudioSourceRole.NEAR_END)
    router = AudioSourceRouter([source], queue_size=2)
    capture_id = UUID(int=1)
    await router.prepare(CaptureProfile.microphone(), capture_id)
    await router.commit()
    await source.queue.put(make_frame(capture_id, sequence=0, sample=1))
    frame = await anext(router.frames())
    assert frame.sequence == 0
    await router.stop()


async def test_router_rejects_dual_before_preparing_sources() -> None:
    source = FakeSource(AudioSourceKind.MICROPHONE, AudioSourceRole.NEAR_END)
    router = AudioSourceRouter([source])
    with pytest.raises(UnsupportedCaptureProfileError, match="dual"):
        await router.prepare(CaptureProfile.model_validate(VALID_DUAL), UUID(int=1))
    assert source.state is AudioSourceState.STOPPED


async def test_router_drops_oldest_without_growing_queue() -> None:
    source = FakeSource(AudioSourceKind.MICROPHONE, AudioSourceRole.NEAR_END)
    router = AudioSourceRouter([source], queue_size=1)
    capture_id = UUID(int=1)
    await router.prepare(CaptureProfile.microphone(), capture_id)
    await router.commit()
    await source.queue.put(make_frame(capture_id, sequence=1, sample=1))
    await source.queue.put(make_frame(capture_id, sequence=2, sample=2))
    await wait_until(lambda: router.health().dropped_frames == 1)
    assert (await anext(router.frames())).sequence == 2
    await router.stop()
```

上述 `wait_until()` 使用一秒硬超时，确保失败时不会形成时间型死等。

- [x] **Step 2: 运行测试，确认 Router 模块缺失**

Run: `uv run pytest tests/test_audio_router.py -q --no-cov`

Expected: FAIL，提示 `sona.audio.router` 尚不存在。

- [x] **Step 3: 实现 prepare/commit/abort/stop 与 pump**

```python
class UnsupportedCaptureProfileError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RouterHealth:
    state: AudioSourceState
    active_kind: AudioSourceKind | None
    queued_frames: int
    dropped_frames: int


class AudioSourceRouter:
    def __init__(self, sources: Iterable[AudioSource], *, queue_size: int = 8) -> None:
        source_map: dict[AudioSourceKind, AudioSource] = {}
        for source in sources:
            if source.kind in source_map:
                raise ValueError(f"duplicate audio source kind: {source.kind}")
            source_map[source.kind] = source
        self._sources = source_map
        self._queue: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=max(1, queue_size))
        self._state = AudioSourceState.STOPPED
        self._active_source: AudioSource | None = None
        self._capture_id: UUID | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._dropped_frames = 0

    async def prepare(self, profile: CaptureProfile, capture_id: UUID) -> None:
        if self._state is not AudioSourceState.STOPPED:
            raise RuntimeError("audio source router is not stopped")
        if profile.mode is CaptureMode.DUAL:
            raise UnsupportedCaptureProfileError("dual capture requires DualSourceMixer")
        spec = profile.sources[0]
        source = self._sources.get(spec.kind)
        if source is None or source.role is not spec.role:
            raise RuntimeError(f"audio source unavailable: {spec.kind}")
        try:
            await source.prepare(capture_id)
        except BaseException:
            await source.abort()
            raise
        self._active_source = source
        self._capture_id = capture_id
        self._state = AudioSourceState.READY

    async def commit(self) -> None:
        source = self._active_source
        capture_id = self._capture_id
        if self._state is not AudioSourceState.READY or source is None or capture_id is None:
            raise RuntimeError("audio source router is not ready")
        await source.commit()
        self._pump_task = asyncio.create_task(self._pump(source, capture_id))
        self._state = AudioSourceState.ACTIVE

    async def abort(self) -> None:
        await self._cancel_pump()
        if self._active_source is not None:
            await self._active_source.abort()
        self._reset()

    async def stop(self) -> None:
        await self._cancel_pump()
        if self._active_source is not None:
            await self._active_source.stop()
        self._reset()

    async def frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            frame = await self._queue.get()
            try:
                yield frame
            finally:
                self._queue.task_done()

    def health(self) -> RouterHealth:
        kind = self._active_source.kind if self._active_source is not None else None
        return RouterHealth(self._state, kind, self._queue.qsize(), self._dropped_frames)
```

实现规则：

- 构造时拒绝重复 `kind`，队列容量至少为 1。
- `prepare()` 在验证 single profile 与来源存在后调用来源 prepare；失败时调用 abort 并回到 stopped。
- `commit()` 先提交来源，再建立唯一 pump task；没有 ready 来源时拒绝。
- pump 只接收 capture_id 匹配的帧，队满时 drop-oldest 并累计计数。
- `abort()`/`stop()` 取消 pump、清空路由队列、释放来源并幂等回到 stopped。
- dual 在任何来源 prepare 之前抛 `UnsupportedCaptureProfileError("dual capture requires DualSourceMixer")`。

- [x] **Step 4: 运行 Router 聚焦测试与前两任务回归**

Run: `uv run pytest tests/test_audio_frame.py tests/test_audio_source.py tests/test_audio_router.py -q --no-cov`

Expected: PASS。

- [x] **Step 5: 运行静态检查并提交**

Run: `uv run mypy src/sona/audio/`

Run: `uv run ruff check src/sona/audio/ tests/test_audio_frame.py tests/test_audio_source.py tests/test_audio_router.py`

Commit:

```bash
git add src/sona/audio/__init__.py src/sona/audio/router.py tests/test_audio_router.py
git commit -m "feat(audio): 建立两阶段有界来源路由"
```

---

### Task 4: 服务端真实 PCM 能量与 runtime snapshot

**Files:**
- Create: `src/sona/audio/levels.py`
- Create: `tests/test_audio_levels.py`
- Modify: `src/sona/audio/__init__.py`
- Modify: `src/sona/ui/protocol.py`
- Modify: `src/sona/ui/runtime.py`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_control.py`

**Interfaces:**
- Produces: `AudioLevels`, `AudioLevelMeter`, `pcm16_level()`。
- Extends: `RuntimeStateSnapshot.audio_levels: AudioLevelsSnapshot`，默认全零。
- `UIRuntime` 注册 `levels` sink，并以最多 20 Hz 发布 latest-only 快照。

- [x] **Step 1: 写 PCM 能量失败测试**

```python
from sona.audio.frame import AudioSourceKind
from sona.audio.levels import AudioLevelMeter, pcm16_level


def test_pcm16_level_maps_silence_to_zero() -> None:
    assert pcm16_level(b"\x00\x00" * 512) == 0.0


def test_pcm16_level_is_bounded_and_monotonic() -> None:
    quiet = pcm16_level((100).to_bytes(2, "little", signed=True) * 512)
    loud = pcm16_level((10_000).to_bytes(2, "little", signed=True) * 512)
    assert 0.0 <= quiet < loud <= 1.0


def test_meter_mirrors_microphone_into_mixed_and_throttles() -> None:
    meter = AudioLevelMeter(publish_interval_ns=50_000_000)
    assert meter.update(AudioSourceKind.MICROPHONE, b"\x10\x00" * 512, now_ns=1)
    assert not meter.update(AudioSourceKind.MICROPHONE, b"\x10\x00" * 512, now_ns=2)
    levels = meter.snapshot()
    assert levels.microphone == levels.mixed
    assert levels.updated_at_ns == 2


def test_meter_mute_clears_microphone_and_mixed() -> None:
    meter = AudioLevelMeter()
    meter.update(AudioSourceKind.MICROPHONE, b"\xff\x7f" * 512, now_ns=1)
    meter.clear(AudioSourceKind.MICROPHONE, now_ns=2)
    assert meter.snapshot().microphone == 0.0
    assert meter.snapshot().mixed == 0.0
```

- [x] **Step 2: 运行能量测试，确认模块缺失**

Run: `uv run pytest tests/test_audio_levels.py -q --no-cov`

Expected: FAIL，提示 `sona.audio.levels` 尚不存在。

- [x] **Step 3: 实现纯函数和节流 meter**

```python
@dataclass(frozen=True, slots=True)
class AudioLevels:
    microphone: float = 0.0
    physical_output: float = 0.0
    mixed: float = 0.0
    updated_at_ns: int = 0


def pcm16_level(pcm: bytes) -> float:
    if len(pcm) % 2:
        raise ValueError("PCM16 payload length must be even")
    if not pcm:
        return 0.0
    samples = memoryview(pcm).cast("h")
    rms = math.sqrt(sum(int(sample) ** 2 for sample in samples) / len(samples))
    if rms == 0:
        return 0.0
    dbfs = 20.0 * math.log10(rms / 32768.0)
    return min(1.0, max(0.0, (dbfs + 60.0) / 60.0))
```

`AudioLevelMeter.update()` 按来源更新 level；当前单来源麦克风同时作为 mixed。第一次更新立即允许发布，之后使用 monotonic ns 控制 50 ms 间隔。`clear()` 立即允许发布。

- [x] **Step 4: 写 runtime snapshot 与广播失败测试**

在 `tests/test_runtime.py` 增加：

```python
async def test_pcm_level_sink_updates_snapshot_and_publishes(settings: Settings) -> None:
    with ExitStack() as stack:
        _patched(stack)
        runtime = UIRuntime(settings)
        client = runtime.runtime_events.add_client()
        client.latest_nowait()
        runtime._audio_levels = AudioLevelMeter(publish_interval_ns=0)
        await runtime._observe_mic_audio(b"\xff\x7f" * 512)
        state = client.latest_nowait()
    assert state.audio_levels.microphone > 0.0
    assert state.audio_levels.mixed == state.audio_levels.microphone
```

在启动装配测试中增加 `hub.add_sink.assert_any_call("levels", runtime._observe_mic_audio)`；在静音测试中验证 `audio_levels.microphone == 0.0`。在 `tests/test_control.py` 验证默认快照的三个 level 均为零。

- [x] **Step 5: 运行 runtime 测试，确认字段和 sink 尚不存在**

Run: `uv run pytest tests/test_runtime.py tests/test_control.py -q --no-cov`

Expected: FAIL，分别指出 `audio_levels` 或 `_observe_mic_audio` 不存在。

- [x] **Step 6: 接入 Pydantic 快照、Hub level sink 和 latest-only 发布**

```python
class AudioLevelsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    microphone: float = Field(default=0.0, ge=0.0, le=1.0)
    physical_output: float = Field(default=0.0, ge=0.0, le=1.0)
    mixed: float = Field(default=0.0, ge=0.0, le=1.0)
    updated_at_ns: int = Field(default=0, ge=0)


class RuntimeStateSnapshot(BaseModel):
    audio_levels: AudioLevelsSnapshot = Field(default_factory=AudioLevelsSnapshot)
```

`UIRuntime.__init__()` 创建 meter；`_wire_sinks()` 增加 `levels`；`_observe_mic_audio()` 更新并在节流到期时调用 `_publish_runtime_state()`；`snapshot()` 复制 meter 当前值；`set_mic_muted(True)` 清零并发布；`diagnostics()` 只输出数值快照，不输出 PCM。

- [x] **Step 7: 运行后端聚焦测试和静态检查**

Run: `uv run pytest tests/test_audio_levels.py tests/test_runtime.py tests/test_control.py -q --no-cov`

Run: `uv run mypy src/sona/audio/levels.py src/sona/ui/protocol.py src/sona/ui/runtime.py`

Run: `uv run ruff check src/sona/audio/levels.py src/sona/ui/protocol.py src/sona/ui/runtime.py tests/test_audio_levels.py tests/test_runtime.py tests/test_control.py`

Expected: 全部 exit 0。

- [x] **Step 8: 提交服务端能量链路**

```bash
git add src/sona/audio/__init__.py src/sona/audio/levels.py src/sona/ui/protocol.py src/sona/ui/runtime.py tests/test_audio_levels.py tests/test_runtime.py tests/test_control.py
git commit -m "feat(ui): 广播服务端真实音频能量"
```

---

### Task 5: 前端移除浏览器采音并消费权威能量

**Files:**
- Modify: `ui/src/protocol.ts`
- Modify: `ui/src/hooks/useCommandSocket.ts`
- Modify: `ui/src/hooks/useCommandSocket.test.ts`
- Modify: `ui/src/services/audioEnergyService.ts`
- Create: `ui/src/services/audioEnergyService.test.ts`
- Modify: `ui/src/components/UnifiedAcousticWaveform.tsx`

**Interfaces:**
- Consumes: `RuntimeStateSnapshot.audio_levels`。
- Produces: `AudioEnergyService.updateFromRuntimeState(state)`；既有 `subscribe()`、`setMuted()`、`getEnergy()` 保持兼容。
- `getUserMedia`、`AudioContext`、`MediaStream` 和 requestAnimationFrame 分析循环全部从服务中删除。

- [x] **Step 1: 写零浏览器采集与服务端更新失败测试**

```typescript
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RuntimeStateSnapshot } from "../protocol";
import { AudioEnergyService } from "./audioEnergyService";

const STATE: RuntimeStateSnapshot = {
  mode: "meeting",
  pcm_owner: "meeting",
  pipeline: "stopped",
  subtitle: "connected",
  mic_muted: false,
  runtime_revision: 1,
  audio_levels: {
    microphone: 0.25,
    physical_output: 0.5,
    mixed: 0.625,
    updated_at_ns: 10,
  },
};

describe("AudioEnergyService", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("publishes the mixed server level without opening browser audio", () => {
    const getUserMedia = vi.fn();
    vi.stubGlobal("navigator", { mediaDevices: { getUserMedia } });
    const service = new AudioEnergyService();
    const subscriber = vi.fn();
    const unsubscribe = service.subscribe(subscriber);
    service.updateFromRuntimeState(STATE);
    expect(subscriber).toHaveBeenLastCalledWith(0.625);
    expect(getUserMedia).not.toHaveBeenCalled();
    unsubscribe();
  });

  it("publishes zero while muted", () => {
    const service = new AudioEnergyService();
    const subscriber = vi.fn();
    service.subscribe(subscriber);
    service.setMuted(true);
    service.updateFromRuntimeState(STATE);
    expect(service.getEnergy()).toBe(0);
    expect(subscriber).toHaveBeenLastCalledWith(0);
  });
});
```

- [x] **Step 2: 运行测试，确认当前服务会尝试浏览器采音且没有更新 API**

Run: `cd ui && npm test -- --run src/services/audioEnergyService.test.ts`

Expected: FAIL，指出 `AudioEnergyService` 未导出或 `updateFromRuntimeState` 不存在。

- [x] **Step 3: 将能量服务改为同步内存发布器**

```typescript
type EnergySubscriber = (energy: number) => void;

export class AudioEnergyService {
  private readonly subscribers = new Set<EnergySubscriber>();
  private currentEnergy = 0;
  private muted = false;

  subscribe(callback: EnergySubscriber): () => void {
    this.subscribers.add(callback);
    callback(this.currentEnergy);
    return () => this.subscribers.delete(callback);
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
    if (muted) this.publish(0);
  }

  getEnergy(): number {
    return this.currentEnergy;
  }

  updateFromRuntimeState(state: RuntimeStateSnapshot): void {
    const levels = state.audio_levels;
    this.publish(this.muted || state.mic_muted ? 0 : (levels?.mixed ?? 0));
  }

  private publish(value: number): void {
    this.currentEnergy = Math.min(1, Math.max(0, value));
    this.subscribers.forEach((callback) => callback(this.currentEnergy));
  }
}
```

- [x] **Step 4: 扩展前端协议校验并接入 command state**

在 `RuntimeStateSnapshot` 增加可选 `audio_levels`，并实现 `isAudioLevels()`，要求三个 level 是 `[0,1]` 有限数且 `updated_at_ns` 是非负整数。`isRuntimeState()` 在字段存在时调用该校验。

在 `useCommandSocket` 的 `applyState` 第一段加入：

```typescript
audioEnergyService.updateFromRuntimeState(snapshot);
```

在 `useCommandSocket.test.ts` 增加相同 runtime revision、相同 owner、仅 `audio_levels` 变化时 `applyState` 获得新值的测试。

- [x] **Step 5: 修正波形语义并运行前端聚焦测试**

将 `UnifiedAcousticWaveform` 中“浏览器麦克风”“10ms 零延迟”等注释改为“服务端实际送入推理链的能量”，不修改绘制算法和公共 props。

Run: `cd ui && npm test -- --run src/services/audioEnergyService.test.ts src/hooks/useCommandSocket.test.ts`

Expected: PASS。

- [x] **Step 6: 运行前端构建并提交**

Run: `cd ui && npm run build`

Expected: TypeScript 和 Vite build 均 exit 0。

Commit:

```bash
git add ui/src/protocol.ts ui/src/hooks/useCommandSocket.ts ui/src/hooks/useCommandSocket.test.ts ui/src/services/audioEnergyService.ts ui/src/services/audioEnergyService.test.ts ui/src/components/UnifiedAcousticWaveform.tsx
git commit -m "refactor(ui): 移除浏览器麦克风能量采集"
```

---

### Task 6: P0 回归门禁与状态归档

**Files:**
- Modify: `docs/superpowers/plans/2026-08-31-audio-source-foundation.md`
- Modify: `docs/README.md`

**Interfaces:**
- Verifies: P0 新公共类型、现有 mic-only 行为、控制协议兼容和前端零浏览器采集。
- Does not expose: physical-output 启动命令、设备选择、dual Mixer 或数据库 v2 字段。

- [x] **Step 1: 运行后端完整质量门禁**

Run: `SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/`

Expected: 全部测试通过，分支覆盖率不低于 80%。

Run: `uv run mypy src/`

Expected: exit 0。

Run: `uv run ruff check src/ tests/`

Expected: exit 0。

- [x] **Step 2: 运行前端完整质量门禁**

Run: `cd ui && npm test -- --run`

Expected: 全部测试通过。

Run: `cd ui && npm run build`

Expected: exit 0。

- [x] **Step 3: 验证浏览器采集调用已归零**

Run: `rg -n "getUserMedia|createMediaStreamSource" ui/src`

Expected: 无生产代码匹配；允许 `audioEnergyService.test.ts` 中的负向断言出现 `getUserMedia`。

Run: `rg -n "getUserMedia|createMediaStreamSource" ui/src --glob '!**/*.test.ts' --glob '!**/*.test.tsx'`

Expected: 无输出。

- [x] **Step 4: 对照规格完成 P0 范围复核**

逐项确认：统一帧含 capture/source/generation/sequence/host time；profile 保持 v1 mic 默认；队列有界且 drop-oldest；dual 被显式拒绝；runtime snapshot 能量来自后端 PCM；浏览器零采音；PCM 无持久化路径。

- [x] **Step 5: 更新计划状态和文档索引**

将已执行 checkbox 更新为 `[x]`，将 `docs/README.md` 中 plans 数量校准为 13，并保持本计划为当前实施计划。不得将物理输出设计规格从 `under_review` 改为 `implemented`，因为 P1–P3 尚未完成。

- [x] **Step 6: 检查 staged diff、敏感信息与提交**

Run: `git diff --check`

Run: `git status --short`

Run: `git diff --cached`

确认无凭据、PCM fixture、构建输出、`.env` 或与 P0 无关的文件。

Commit:

```bash
git add docs/README.md docs/superpowers/plans/2026-08-31-audio-source-foundation.md
git commit -m "docs(audio): 归档音频源基础设施实施结果"
```

---

## Spec Coverage Review

- 规格 §8 的 `AudioFrame`、来源枚举、32 ms 统一格式与 `CaptureProfile`：Task 1。
- 规格 §11.1 的 `AudioSource` / `MicrophoneSource` 边界：Task 2。
- 规格 §10.3 与 §11 的有界队列、drop-oldest、两阶段 lifecycle：Task 2–3。
- 规格 §14 的浏览器控制面与服务端能量：Task 4–5。
- 规格 §15 的无 PCM 诊断与有界能量指标：Task 4、Task 6。
- 规格 §16 的 P0 阶段门禁：Task 6。

本计划不覆盖规格 §9–10 的 Swift Helper / UDS、§12–13 的 output/dual 会议接线、数据库 v2 迁移与设备选择 UI，也不覆盖 WebRTC AEC；这些边界分别进入 P1、P2/P3 和 P4 计划。P0 对 dual 返回稳定错误，避免未实现能力被误用。

---

## P0 Completion Boundary

P0 完成后，系统仍默认并且仅运行 microphone capture，但已经具备：

- 可序列化和校验的来源/帧/profile 语义；
- 可被 Core Audio Helper 复用的两阶段 `AudioSource` 接口；
- 单来源有界 Router 与稳定背压诊断；
- 来自真实服务端 PCM 的可视化能量；
- 不再打开浏览器麦克风的控制面。

后续计划以这些接口为固定输入：P1 实现 `PhysicalOutputSource` 与 Swift Helper；P2 解锁 output-only 字幕；P3 增加 `DualSourceMixer`、会议 v2 持久化和三态页面。
