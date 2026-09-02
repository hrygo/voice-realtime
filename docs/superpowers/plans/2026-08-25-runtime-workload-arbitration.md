---
title: "运行时工作负载仲裁实施计划"
description: "实现 RuntimeModeCoordinator 四模式切换与单 PCM owner 仲裁的执行任务清单"
status: implemented
type: execution_plan
category: architecture
version: "v1.0.0"
date: 2026-08-25
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "sona-core"
tags:
  - execution-plan
  - runtime-arbitration
  - workload-arbitration
---

# Runtime Workload Arbitration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将普通字幕提升为服务端一等模式，以两阶段事务保证 `assistant / subtitles / meeting / idle` 任意切换期间最多只有一个 PCM 所有者，并让多浏览器通过 revisioned runtime state 收敛到同一权威状态。

**Architecture:** `RuntimeModeCoordinator` 是唯一模式与 PCM 所有权仲裁者；目标工作负载先建立不接收 PCM 的 preparation，来源静默后同步提升目标，再原子提交 `mode / pcm_owner / runtime_revision`。`UIRuntime` 在 AudioHub sink 边界执行第二层 PCM 门控，`RuntimeStateBroadcaster` 向所有控制连接发送 latest-only 完整快照，字幕 WebSocket 只读且随模式撤销。前端先完成首快照同步，再按 ownership revision 仲裁命令 ack、广播与 `/api/runtime` 对账结果。

**Tech Stack:** Python 3.12、asyncio、Pydantic v2、FastAPI/Starlette WebSocket、Pipecat、WhisperLiveKit、PostgreSQL、React 19、TypeScript、Zustand、Vitest、pytest。

**Spec:** `docs/superpowers/specs/2026-08-25-runtime-workload-arbitration-design.md`

**Code Baseline:** `main`，`HEAD=beff723c7d337a043b71f8f6c932746abe59f33d`；代码图 generation `2026-08-25T14:41:41Z`。本计划涉及的现有源码与测试路径均无已记录索引缺口，该覆盖信号为 best-effort；新增文件在实现时以工作树实测为准。

## Global Constraints

- 本计划是一个原子产品变更：允许分任务提交，发布时前后端必须同时进入同一版本，禁止只部署新字幕 WebSocket 约束或只部署新前端。
- Python 严格保持 `>=3.12,<3.13`；不增加依赖，不修改模型 ID、模型下载策略、公开端口或 LAN/localhost 监听选择。
- 麦克风继续由 `AudioHub` 单源采集；稳定状态只允许一个 `pcm_owner`，转换屏障期间必须为 `none`。
- 保留 `EchoSuppressionProcessor` 与 `SelfEchoFilter` 双层回声防线，不改其参数和调用顺序。
- PostgreSQL 继续作为会议 confirmed 文本、speaker 映射和纪要唯一事实源；会议不写 `current.srt`，任何路径都不保存音频。
- `RuntimeModeCoordinator` 继续位于 `src/sona/meeting/runtime_mode.py`；多客户端状态广播独立放入 `src/sona/ui/runtime_events.py`。
- 用户命令串行且非抢占；控制 WebSocket 断开或客户端 ack 超时不得取消已被服务端接受的转换。
- shutdown 是唯一可取消在途转换的路径；取消后 abort prepared target、尽力停止全部工作负载并提交 `idle/none`。
- `runtime_revision` 只排序 `mode`、`pcm_owner`、`active_meeting_id` 和模式驱动 Tab；persona、mic、pipeline、会议转录继续使用既有更新语义。
- 诊断只输出计数、耗时和状态，不读取 WLK 日志，不记录 PCM、完整转写、模型上下文、凭据或私有环境变量，也不自动归因或提示关闭外部进程。
- 每个任务先运行指定 RED 测试确认因预期缺口失败，再做最小实现并运行 GREEN；提交前保持工作树中用户已有改动不受影响。

---

### Task 1: 冻结模式、PCM owner 与控制协议契约

**Files:**
- Modify: `src/sona/meeting/models.py`
- Modify: `src/sona/ui/protocol.py`
- Modify: `src/sona/ui/control.py`
- Modify: `tests/test_meeting_models.py`
- Modify: `tests/test_control.py`

**Interfaces:**
- Produces: `RuntimeMode.SUBTITLES`、`PCMOwner`、`StartSubtitlesCommand`。
- Extends: `RuntimeStateSnapshot.pcm_owner`，默认值为 `none` 以兼容现有内部构造点。
- Normalizes: `RuntimeStateEvent.event` 默认值改为 `runtime_state`，解析期兼容既有 `state`。
- Routes: `start_subtitles` → `ControlRuntime.start_subtitles()`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_meeting_models.py` 冻结四种模式和四种 owner；在 `tests/test_control.py` 增加严格命令解析与派发：

```python
def test_runtime_mode_and_pcm_owner_include_subtitles() -> None:
    assert RuntimeMode("subtitles") is RuntimeMode.SUBTITLES
    assert PCMOwner("assistant") is PCMOwner.ASSISTANT
    assert PCMOwner("subtitles") is PCMOwner.SUBTITLES
    assert PCMOwner("meeting") is PCMOwner.MEETING
    assert PCMOwner("none") is PCMOwner.NONE


async def test_start_subtitles_dispatches_to_runtime(runtime: AsyncMock) -> None:
    bridge = ControlBridge(runtime, BridgeSettings())
    response = await bridge.handle(
        {"contract_version": "1", "request_id": "sub-1", "cmd": "start_subtitles"}
    )
    assert response["ok"] is True
    runtime.start_subtitles.assert_awaited_once_with()
```

同时用现有 fixture 的完整必填字段构造 `RuntimeStateSnapshot`，断言 `model_dump(mode="json")["pcm_owner"] == "none"`；断言 `RuntimeStateEvent(state=snapshot).event == "runtime_state"` 且显式 `event="state"` 仍可解析；`start_subtitles` 出现未知字段时返回现有 `invalid_payload` 包络。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/test_meeting_models.py tests/test_control.py -q --no-cov`

Expected: `PCMOwner`、`RuntimeMode.SUBTITLES` 或 `StartSubtitlesCommand` 不存在，测试失败。

- [ ] **Step 3: 实现最小契约**

在 `meeting/models.py` 增加：

```python
class RuntimeMode(StrEnum):
    ASSISTANT = "assistant"
    SUBTITLES = "subtitles"
    MEETING = "meeting"
    IDLE = "idle"


class PCMOwner(StrEnum):
    ASSISTANT = "assistant"
    SUBTITLES = "subtitles"
    MEETING = "meeting"
    NONE = "none"
```

在 `ui/protocol.py` 增加严格模型并纳入 `ControlCommand` discriminator：

```python
class StartSubtitlesCommand(CommandBase):
    cmd: Literal["start_subtitles"]
    contract_version: Literal["1"] | None = None


pcm_owner: PCMOwner = PCMOwner.NONE

# RuntimeStateEvent field
event: Literal["state", "runtime_state"] = "runtime_state"
```

扩展 `_COMMAND_NAMES`、`ControlRuntime` 和 `_dispatch()`，调用 `await self._runtime.start_subtitles()`；`ControlBridge._response()` 的 V1 命令集合也加入 `start_subtitles`。错误继续通过现有 `ErrorCode`/统一响应包络返回。

- [ ] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/test_meeting_models.py tests/test_control.py -q --no-cov`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sona/meeting/models.py src/sona/ui/protocol.py src/sona/ui/control.py tests/test_meeting_models.py tests/test_control.py
git commit -m "feat(runtime): 增加字幕模式与 PCM 所有权契约"
```

### Task 2: 建立 latest-only RuntimeStateBroadcaster

**Files:**
- Create: `src/sona/ui/runtime_events.py`
- Create: `tests/test_runtime_events.py`

**Interfaces:**
- Produces: `RuntimeStateClient.receive()`、`RuntimeStateClient.latest_nowait()`、`RuntimeStateBroadcaster.add_client()`、`remove_client()`、`publish()`。
- Queue rule: 每客户端容量固定为 1；满时丢弃旧快照并保留最新快照。
- Initial snapshot: 注册与读取当前快照在同一事件循环临界段完成，供字幕订阅消除检查/注册竞态。

- [ ] **Step 1: 写失败测试**

```python
async def test_slow_client_only_keeps_latest_revision() -> None:
    current = snapshot(revision=1, mode=RuntimeMode.ASSISTANT)
    broadcaster = RuntimeStateBroadcaster(lambda: current)
    client = broadcaster.add_client()

    assert (await client.receive()).runtime_revision == 1
    broadcaster.publish(snapshot(revision=2, mode=RuntimeMode.IDLE))
    broadcaster.publish(snapshot(revision=3, mode=RuntimeMode.SUBTITLES))

    latest = await client.receive()
    assert latest.runtime_revision == 3
    assert latest.mode is RuntimeMode.SUBTITLES


def test_add_client_captures_current_state_without_await() -> None:
    broadcaster = RuntimeStateBroadcaster(
        lambda: snapshot(revision=7, mode=RuntimeMode.MEETING)
    )
    client = broadcaster.add_client()
    assert client.latest_nowait().runtime_revision == 7
```

再覆盖移除客户端后不再入队、重复移除幂等、publish 不阻塞其他客户端。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/test_runtime_events.py -q --no-cov`

Expected: `sona.ui.runtime_events` 不存在。

- [ ] **Step 3: 实现广播器**

使用同步 `publish()`，避免模式已提交而事件入队之间出现额外 await：

```python
@dataclass(eq=False, slots=True)
class RuntimeStateClient:
    _queue: asyncio.Queue[RuntimeStateSnapshot]

    async def receive(self) -> RuntimeStateSnapshot:
        return await self._queue.get()

    def latest_nowait(self) -> RuntimeStateSnapshot:
        latest = self._queue.get_nowait()
        while True:
            try:
                latest = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return latest


class RuntimeStateBroadcaster:
    def __init__(self, snapshot_provider: Callable[[], RuntimeStateSnapshot]) -> None:
        self._snapshot_provider = snapshot_provider
        self._clients: set[RuntimeStateClient] = set()

    def add_client(self) -> RuntimeStateClient:
        client = RuntimeStateClient(asyncio.Queue(maxsize=1))
        self._clients.add(client)
        self._replace_latest(client, self._snapshot_provider())
        return client

    def remove_client(self, client: RuntimeStateClient) -> None:
        self._clients.discard(client)

    def publish(self, state: RuntimeStateSnapshot) -> None:
        for client in tuple(self._clients):
            self._replace_latest(client, state)
```

`_replace_latest()` 使用 `get_nowait()`/`put_nowait()`；不调用 `task_done()`，因为该队列不使用 `join()`。

- [ ] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/test_runtime_events.py -q --no-cov`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sona/ui/runtime_events.py tests/test_runtime_events.py
git commit -m "feat(runtime): 增加权威状态广播器"
```

### Task 3: 将 SubtitleProxy 改为显式 prepared/active 生命周期

**Files:**
- Modify: `src/sona/ui/subtitle_proxy.py`
- Modify: `tests/test_subtitle_proxy.py`

**Interfaces:**
- Produces: `SubtitlePreparation`、`CapturePreparation`。
- Ordinary subtitles: `prepare_browser_capture(timeout_secs)`、同步 `commit_browser_capture()`、`abort_browser_capture()`、`deactivate_browser_capture()`。
- Meeting gateway: `prepare_capture(owner, timeout_secs)`、同步 `commit_capture()`、`abort_prepared_capture()`；活动会议继续使用 `finish_capture()`/`abort_capture()`。
- Invariant: 两类 preparation 在 commit 前均拒绝 PCM；token 只能 commit 或 abort 一次。

- [ ] **Step 1: 写普通字幕 RED 测试**

```python
async def test_start_initializes_without_connecting(proxy, stream_factory) -> None:
    await proxy.start()
    assert stream_factory.call_count == 0
    assert proxy.state == "paused"


async def test_prepare_waits_ready_but_rejects_pcm_until_commit(proxy, stream) -> None:
    await proxy.start()
    preparation_task = asyncio.create_task(
        proxy.prepare_browser_capture(timeout_secs=0.2)
    )
    await stream.connected.wait()
    await stream.emit(ASREvent(kind="ready"))
    preparation = await preparation_task

    proxy.push_audio(b"before")
    await asyncio.sleep(0)
    assert stream.sent_audio == []
    assert proxy.browser_capture_active is False

    proxy.commit_browser_capture(preparation)
    proxy.push_audio(b"after")
    await wait_until(lambda: stream.sent_audio == [b"after"])
    assert proxy.browser_capture_active is True
```

覆盖 prepare timeout 自动关闭 prepared stream、陈旧/重复 token 被拒绝、deactivate 清空 audio buffer、活动普通字幕断线后才允许 supervisor 重连。

- [ ] **Step 2: 写会议 preparation RED 测试**

```python
async def test_meeting_capture_preparation_is_silent_until_commit(proxy, stream) -> None:
    task = asyncio.create_task(
        proxy.prepare_capture("meeting:abc", timeout_secs=0.2)
    )
    await stream.connected.wait()
    await stream.emit(ASREvent(kind="ready"))
    preparation = await task

    proxy.push_audio(b"before")
    await asyncio.sleep(0)
    assert stream.sent_audio == []

    proxy.commit_capture(preparation)
    proxy.push_audio(b"after")
    await wait_until(lambda: stream.sent_audio == [b"after"])
```

增加 `finish_capture()` 和 `abort_capture()` 后 `browser_capture_active` 仍为 false、没有自动创建普通字幕 stream 的断言。

- [ ] **Step 3: 运行 RED**

Run: `uv run pytest tests/test_subtitle_proxy.py -q --no-cov`

Expected: 新 token 和生命周期方法不存在，`start()` 仍自动连接，测试失败。

- [ ] **Step 4: 实现 token 与状态机**

在模块顶部增加冻结 token：

```python
@dataclass(frozen=True, slots=True)
class SubtitlePreparation:
    generation: int


@dataclass(frozen=True, slots=True)
class CapturePreparation:
    owner: str
    generation: int
```

增加独立 generation、prepared token、`_browser_capture_active` 与 `SubtitleProxyState.PAUSED`。`start()` 只创建输出目录并初始化状态；prepare 建立 stream、等待 ready/config，始终保持音频接收 flag 为 false。两个 commit 方法必须是普通同步函数，只校验当前 token、stream connected 和 ready，然后切换内存 flag；不得执行网络 I/O。

`_audio_send_loop()` 的发送条件改为 `self._browser_capture_active`；会议发送循环只在 `_capture_accept_audio` 为 true 时发送。`deactivate_browser_capture()` 取消 supervisor 和收发任务、关闭 stream、清空 buffer 与 ready，并进入 paused。删除 `_close_capture()` 末尾的 `_resume_browser_connection()` 调用；普通字幕重连只由已经 commit 的普通字幕 supervisor 触发。

保留现有 `begin_capture()` 作为一个版本周期内的内部兼容包装器：调用 `prepare_capture()` 后立即 `commit_capture()`；Task 5 将 MeetingSession 改完后删除包装器及其旧测试。

- [ ] **Step 5: 运行 GREEN**

Run: `uv run pytest tests/test_subtitle_proxy.py -q --no-cov`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/sona/ui/subtitle_proxy.py tests/test_subtitle_proxy.py
git commit -m "refactor(subtitles): 拆分字幕与会议采集准备提交"
```

### Task 4: 建立普通字幕 epoch、SRT 归档和 Proxy 诊断

**Files:**
- Modify: `src/sona/ui/subtitle_proxy.py`
- Modify: `tests/test_subtitle_proxy.py`

**Interfaces:**
- Epoch: 每次普通字幕激活和活动模式重连都递增 `subtitle_epoch`。
- Reset event: `{"type":"reset","source_epoch":N}`。
- Diagnostics: `SubtitleProxyDiagnostics(workload, ws_state, reconnect_count, last_event_age_ms, dropped_chunks, gap_count)`。
- Clock: 构造器接受 `clock: Callable[[], float] = time.monotonic`，测试不依赖真实等待。

- [ ] **Step 1: 写 epoch/SRT RED 测试**

```python
async def test_reconnect_archives_and_resets_previous_epoch(proxy, stream_factory, tmp_path) -> None:
    client_messages: list[str] = []
    async def collect(message: str) -> None:
        client_messages.append(message)

    proxy.add_client(collect)
    first = await prepare_and_commit_subtitles(proxy, stream_factory.first)
    await stream_factory.first.emit(snapshot_event("第一段", source_epoch=first.generation))
    await stream_factory.first.disconnect()
    await stream_factory.second_ready()

    archives = list(tmp_path.glob("session-*.srt"))
    assert len(archives) == 1
    assert "第一段" in archives[0].read_text(encoding="utf-8")
    assert json.loads(client_messages[-1]) == {
        "type": "reset",
        "source_epoch": first.generation,
    }
    assert not (tmp_path / "current.srt").read_text(encoding="utf-8")
```

覆盖：无 confirmed 时不归档；deactivate 归档一次；归档后新客户端不回放旧 payload；会议事件不写 `current.srt`；第二个零时间轴快照不能覆盖第一个 epoch 的归档。

- [ ] **Step 2: 写诊断 RED 测试**

注入 fake clock，断言 ready/event 更新 `last_event_age_ms`，普通字幕与会议重连累计 `reconnect_count`，buffer 丢最旧块时累计 `dropped_chunks`，会议 gap 累计 `gap_count`。同时断言长时间静音只增加 age，不自动把 ready workload 标成 degraded。

- [ ] **Step 3: 运行 RED**

Run: `uv run pytest tests/test_subtitle_proxy.py -q --no-cov`

Expected: epoch reset、原子清空和诊断快照尚未实现。

- [ ] **Step 4: 实现单一 `_close_subtitle_epoch()`**

```python
async def _close_subtitle_epoch(self) -> None:
    epoch = self._subtitle_epoch
    self._archive_current_srt()
    self._atomic_clear_current_srt()
    self._last_payload = None
    self._snapshot_signature = None
    self._persisted_confirmed_signature = None
    self._session_has_confirmed = False
    self._browser_ready.clear()
    if epoch > 0 and self._clients:
        await self._broadcast_untracked(
            {"type": "reset", "source_epoch": epoch}
        )
```

`_atomic_clear_current_srt()` 在相同目录写空临时文件并 `replace(current.srt)`；目录不可写时保留现有 fail-fast 行为。激活/重连先关闭旧 epoch，再递增 `_subtitle_epoch` 并把该值传给 `ASRSessionContext.source_epoch`。会议 capture 不调用此方法。

实现 `diagnostics(expected_owner: PCMOwner) -> SubtitleProxyDiagnostics`：`expected_owner=none` 时 workload 为 paused；已提交 owner 但没有 ready stream 时为 starting/degraded/error；backoff/error 才触发 degraded/error。`last_event_age_ms` 只在活动 workload 且已记录事件时间时返回。

- [ ] **Step 5: 运行 GREEN**

Run: `uv run pytest tests/test_subtitle_proxy.py -q --no-cov`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/sona/ui/subtitle_proxy.py tests/test_subtitle_proxy.py
git commit -m "feat(subtitles): 隔离连接 epoch 与 SRT 边界"
```

### Task 5: 将 MeetingSession 启动拆为 prepare/commit/publish/abort

**Files:**
- Modify: `src/sona/meeting/session.py`
- Modify: `tests/test_meeting_session.py`

**Interfaces:**
- Produces: `MeetingPreparation`。
- Lifecycle: `prepare_start(title)` → 同步 `commit_start(preparation)` → `publish_started(preparation)`。
- Compensation: `abort_start(preparation)` 标记 `interrupted/mode_switch_aborted`，保留记录。
- Event order: prepare/commit 都不发布 recording；只有 coordinator 已提交后调用 publish。

- [ ] **Step 1: 改造 FakeGateway 并写 RED 测试**

`FakeGateway` 提供 `prepare_capture` AsyncMock、`commit_capture` Mock、`abort_prepared_capture` AsyncMock。增加：

```python
async def test_prepare_creates_record_without_activating_or_publishing(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    publish = AsyncMock()
    session = MeetingSession(repository, gateway, event_publisher=publish)

    preparation = await session.prepare_start("周会")

    assert preparation.record.status is MeetingStatus.RECORDING
    assert session.active_meeting_id is None
    gateway.prepare_capture.assert_awaited_once()
    gateway.commit_capture.assert_not_called()
    publish.assert_not_awaited()


async def test_abort_marks_created_record_interrupted(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway)
    preparation = await session.prepare_start("周会")
    await session.abort_start(preparation)
    assert repository.record.status is MeetingStatus.INTERRUPTED
    assert repository.record.interruption_reason == "mode_switch_aborted"
    assert session.active_meeting_id is None
```

再覆盖：commit 同步启用 capture 并设置 active id；publish 在 commit 后发 `meeting_state_changed`；publish 异常不回滚；token 重复/陈旧操作失败；prepare capture 失败使用 `mode_switch_aborted`；listener/gap listener 总能释放。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/test_meeting_session.py -q --no-cov`

Expected: 新生命周期方法不存在。

- [ ] **Step 3: 实现 MeetingPreparation**

```python
@dataclass(frozen=True, slots=True)
class MeetingPreparation:
    record: MeetingRecord
    capture: object


async def prepare_start(self, title: str | None = None) -> MeetingPreparation:
    # 在 self._lock 内完成 writable、create_meeting、listener 注册和无 PCM capture prepare。
    capture = await self.gateway.prepare_capture(
        f"meeting:{record.id}", timeout_secs=5.0
    )
    preparation = MeetingPreparation(record=record, capture=capture)
    self._preparation = preparation
    return preparation


def commit_start(self, preparation: MeetingPreparation) -> MeetingRecord:
    self._require_current_preparation(preparation)
    self.gateway.commit_capture(preparation.capture)
    self._active_meeting_id = preparation.record.id
    self._record = preparation.record
    self._preparation = None
    return preparation.record
```

`publish_started()` 执行 summary requeue 和 recording event，所有调用者必须在 mode commit 后调用。`abort_start()` 对当前 preparation 调用 `abort_prepared_capture()`、释放 listeners，并调用 `repository.set_status(preparation.record.id, MeetingStatus.INTERRUPTED, reason="mode_switch_aborted")`，把 interrupted record 留在 `_record`。保留 `start()` 作为 prepare→commit→publish 的兼容包装器，Task 6 改完 coordinator 后移除包装器和旧调用断言。

- [ ] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/test_meeting_session.py -q --no-cov`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sona/meeting/session.py tests/test_meeting_session.py
git commit -m "refactor(meeting): 拆分会议启动事务与事件发布"
```

### Task 6: 重写 RuntimeModeCoordinator 两阶段状态机

**Files:**
- Modify: `src/sona/meeting/runtime_mode.py`
- Modify: `tests/test_runtime_mode.py`

**Interfaces:**
- Constructor consumes: `InteractionSession`、`SubtitleWorkload`、可选 `MeetingSession`、同步 `state_publisher`；`configure_meeting()` 支持 lifespan 启动后注入会议服务。
- Produces: `mode`、`pcm_owner`、`runtime_revision`、`last_transition`。
- Commands: `start_assistant()`、`start_subtitles()`、`start_meeting()`、`end_meeting()`、`stop_active_mode()`、`stop()`。
- Ordering: target prepare → owner none → source quiesce/drain → target sync commit → mode/owner/revision commit → broadcast → meeting publish。

- [ ] **Step 1: 建立可记录调用顺序的 fake workloads**

在 `tests/test_runtime_mode.py` 用共享 `calls: list[str]` 记录 interaction、subtitle、meeting 和 publisher。普通字幕 fake 必须区分 `prepared` 与 `active`；会议 fake 必须保存 `MeetingPreparation`。

- [ ] **Step 2: 写成功与幂等 RED 测试**

```python
async def test_assistant_to_subtitles_uses_two_phase_order(coordinator, calls) -> None:
    await coordinator.start_subtitles()
    assert calls == [
        "subtitles.prepare",
        "owner.none",
        "interaction.stop",
        "subtitles.commit",
        "owner.subtitles",
        "state.publish:subtitles:subtitles:1",
    ]
    assert coordinator.mode is RuntimeMode.SUBTITLES
    assert coordinator.pcm_owner is PCMOwner.SUBTITLES


async def test_target_prepare_failure_keeps_assistant_chain(coordinator) -> None:
    coordinator.subtitles.prepare_browser_capture.side_effect = RuntimeError("WLK not ready")
    with pytest.raises(RuntimeError, match="WLK"):
        await coordinator.start_subtitles()
    coordinator.interaction.stop.assert_not_awaited()
    assert coordinator.mode is RuntimeMode.ASSISTANT
    assert coordinator.pcm_owner is PCMOwner.ASSISTANT
    assert coordinator.runtime_revision == 0
```

覆盖 idle→assistant/subtitles、subtitles→assistant、assistant/subtitles→meeting、meeting→idle、所有幂等规则和 meeting mode conflict。

- [ ] **Step 3: 写失败补偿、串行与 shutdown RED 测试**

覆盖以下可观察结果：

- source quiesce 失败会 abort target；来源仍 active 时恢复原 owner、revision+1 并广播恢复快照；
- 来源补偿失败时停止全部 workload，提交 idle/none、revision+1，并抛稳定 `service_unavailable`；
- 两个并发命令按同一 lock 串行，第二个命令依据第一个已提交模式执行；
- 取消客户端等待任务不会取消用 `asyncio.create_task()` 接受的 coordinator 转换；
- `stop()` 设置 closing，取消当前 transition，abort target，停止全部 workload，提交 idle/none 且 revision+1；
- 每次状态 publisher 看到的 `mode/pcm_owner/revision` 是一次性一致组合，不出现双 owner。

- [ ] **Step 4: 运行 RED**

Run: `uv run pytest tests/test_runtime_mode.py -q --no-cov`

Expected: 现有 coordinator 没有 subtitle workload、PCM owner 和两阶段补偿，测试失败。

- [ ] **Step 5: 实现原子提交与转换记录**

使用一个 `_command_lock` 串行用户命令，另存 `_transition_task` 仅供 shutdown 取消。核心提交函数保持同步：

```python
def _commit_state(self, mode: RuntimeMode, owner: PCMOwner) -> None:
    self._mode = mode
    self._pcm_owner = owner
    self._on_owner_changed(owner)
    self._runtime_revision += 1
    self._state_publisher()


def _set_transition_barrier(self) -> None:
    self._pcm_owner = PCMOwner.NONE
    self._on_owner_changed(PCMOwner.NONE)
```

`_set_transition_barrier()` 不增加 revision，也不广播非稳定中间态；它必须立即通知 `UIRuntime` 的 PCM gate。目标 commit 和 `_commit_state()` 之间不得 await。目标 prepare 异常只 abort target 且不碰来源；来源静默后异常调用 `_restore_source()`，按实际 workload active 状态决定恢复 owner 或强制 idle。

助手 preparation 调用 `InteractionSession.start()`；来源助手只在目标 prepare 成功后调用 `stop(reason="切换运行时模式")`，有意离开助手即结束当前 LM response chain。meeting commit 后先 `_commit_state(MEETING, MEETING)`，再 best-effort `await meeting.publish_started(preparation)`。

`last_transition` 保存 `target`、`duration_ms`、`result`、`rollback_result`，错误文本只保存异常类型和稳定错误码。

coordinator 必须在会议后端未初始化时也能仲裁 assistant/subtitles；`start_meeting()` 在 meeting session 为空时抛现有 meeting unavailable 错误。`configure_meeting()` 只允许注入一次，不替换 coordinator、PCM owner、revision 或已注册的 runtime broadcaster。

`stop()` 先在 lock 外设置 `_closing=True`，取消并等待不是当前 task 的 `_transition_task`；转换协程在 `CancelledError` 分支 abort 当前 preparation 后重新抛出。随后 `stop()` 获取 `_command_lock`，尽力停止 interaction、普通字幕和活动/准备中会议，最后提交 idle/none。这样 shutdown 不会等待一个持锁且仍在外部 prepare 的任务而死锁。

- [ ] **Step 6: 运行 GREEN**

Run: `uv run pytest tests/test_runtime_mode.py -q --no-cov`

Expected: PASS。

- [ ] **Step 7: 删除兼容启动包装器并复测会议/字幕**

删除 `MeetingSession.start()` 和 `SubtitleProxy.begin_capture()`；确认仓库内不存在生产调用。保留活动会议的 `abort_capture()`，它与 prepared capture abort 不是同一接口。

Run: `uv run pytest tests/test_runtime_mode.py tests/test_meeting_session.py tests/test_subtitle_proxy.py -q --no-cov`

Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add src/sona/meeting/runtime_mode.py src/sona/meeting/session.py src/sona/ui/subtitle_proxy.py tests/test_runtime_mode.py tests/test_meeting_session.py tests/test_subtitle_proxy.py
git commit -m "feat(runtime): 实现两阶段工作负载仲裁"
```

### Task 7: 在 UIRuntime 接入 coordinator、广播器与双重 PCM 门控

**Files:**
- Modify: `src/sona/ui/runtime.py`
- Modify: `tests/test_runtime.py`

**Interfaces:**
- Produces: `runtime_events: RuntimeStateBroadcaster`、`start_subtitles()`、`diagnostics()`。
- Gate: `_enqueue_audio()` 只允许 owner assistant；`_push_subtitle_audio()` 只允许 owner subtitles/meeting。
- Snapshot: 始终包含 coordinator 的 `mode`、`pcm_owner`、`runtime_revision`。

- [ ] **Step 1: 更新 runtime fixture 并写启动 RED 测试**

```python
async def test_start_keeps_wlk_paused_in_assistant_mode(runtime) -> None:
    await runtime.start()
    runtime.subtitle_proxy.start.assert_awaited_once_with()
    runtime.subtitle_proxy.prepare_browser_capture.assert_not_awaited()
    assert runtime.snapshot().mode is RuntimeMode.ASSISTANT
    assert runtime.snapshot().pcm_owner is PCMOwner.ASSISTANT
```

若 interaction 启动失败，断言 runtime 稳定为 idle/none；WLK 进程探活不阻塞 assistant 启动。

- [ ] **Step 2: 写 PCM owner 矩阵 RED 测试**

参数化 owner 为 assistant/subtitles/meeting/none，直接调用两个 sink callback：

```python
@pytest.mark.parametrize(
    ("owner", "interaction_chunks", "subtitle_chunks"),
    [
        (PCMOwner.ASSISTANT, 1, 0),
        (PCMOwner.SUBTITLES, 0, 1),
        (PCMOwner.MEETING, 0, 1),
        (PCMOwner.NONE, 0, 0),
    ],
)
async def test_pcm_gate_matrix(runtime, owner, interaction_chunks, subtitle_chunks):
    runtime._set_pcm_owner(owner)
    await runtime._enqueue_audio(b"pcm")
    await runtime._push_subtitle_audio(b"pcm")
    assert runtime.audio_queue.qsize() == interaction_chunks
    assert runtime.subtitle_proxy.push_audio.call_count == subtitle_chunks
```

再覆盖 owner 切到 none 时清空 interaction queue 与 SubtitleProxy buffer；prepared target 的发送计数保持零；mic mute 和 TTS echo gate 继续优先丢弃字幕 PCM。

- [ ] **Step 3: 写广播 RED 测试**

注册两个 runtime event client，执行成功转换，断言两者收到相同 revision 的完整快照；target prepare 失败不广播；补偿、强制 idle 和 shutdown 各广播一次稳定快照。

- [ ] **Step 4: 运行 RED**

Run: `uv run pytest tests/test_runtime.py -q --no-cov`

Expected: runtime 仍按 meeting 与连接状态门控，且没有 broadcaster/start_subtitles。

- [ ] **Step 5: 实现 UIRuntime 装配**

初始化顺序固定为：observer/queues → AudioHub/SubtitleProxy → InteractionSession → RuntimeModeCoordinator(initial idle/none) → RuntimeStateBroadcaster。coordinator 从 UIRuntime 构造起始终存在，会议后端稍后通过 `configure_meeting()` 注入：

```python
self._coordinator = RuntimeModeCoordinator(
    self.session,
    self.subtitle_proxy,
    meeting_session=meeting_session,
    initial_mode=RuntimeMode.IDLE,
    on_owner_changed=self._set_pcm_owner,
    state_publisher=self._publish_runtime_state,
)
self.runtime_events = RuntimeStateBroadcaster(self.snapshot)
```

`_publish_runtime_state()` 在 `runtime_events` 已初始化时同步执行 `publish(self.snapshot())`。`configure_meeting()` 委托 `_coordinator.configure_meeting(meeting_session)`，不得重建 coordinator。

`UIRuntime.start()` 调用 `subtitle_proxy.start()` 但不 prepare 普通字幕；再启动 hub；hub 成功后通过 coordinator `start_assistant()` 从 idle/none 进入 assistant，失败时保持 idle/none。`_set_pcm_owner(NONE)` 立即阻止两个 sink 接受新 PCM，并同步 drain interaction queue；普通字幕由 `deactivate_browser_capture()` 清 buffer，会议由 `finish_capture()` 冲刷已接受音频，禁止在通用 owner callback 中丢弃会议 buffer。`stop()` 先 coordinator.stop()，再停止 hub/proxy，避免 proxy shutdown 恢复任何 workload。

- [ ] **Step 6: 运行 GREEN**

Run: `uv run pytest tests/test_runtime.py tests/test_runtime_mode.py tests/test_subtitle_proxy.py -q --no-cov`

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add src/sona/ui/runtime.py tests/test_runtime.py
git commit -m "feat(runtime): 接入 PCM 门控与状态广播"
```

### Task 8: 改造控制 WebSocket 并撤销失去资格的字幕订阅

**Files:**
- Modify: `src/sona/ui/server.py`
- Modify: `tests/test_ui_server.py`

**Interfaces:**
- Control sockets: `/ws/v1/control` 与 `/ws/assistant/cmd` 均注册 runtime event client。
- Ack: 只进入请求连接的响应队列；runtime broadcast 进入每连接 latest-only 队列，二者允许任意顺序。
- Subtitle socket: subtitles/meeting 接受；assistant/idle 使用 `4409` 和固定 reason；存续期间监听 runtime state 并主动撤销。
- Accepted command: 使用独立 task + `asyncio.shield()`，连接断开不取消转换。

- [ ] **Step 1: 写控制广播与断线 RED 测试**

增加两条 `/ws/v1/control` 连接，模拟 runtime publish revision 2；两者都收到 `event=runtime_state`，只有请求连接收到对应 `request_id` ack。测试不得假定 ack 与 broadcast 的固定顺序，而是接收两条消息后按 `event/request_id` 分类。对 `/ws/assistant/cmd` 重复一条广播断言，确保兼容入口也注册同一 broadcaster，而不只发送初始握手。

用 blocking fake transition 接受命令，关闭请求 WebSocket，再释放 transition；断言服务端模式仍提交且另一个控制连接收到新 revision。

- [ ] **Step 2: 写字幕资格与竞态 RED 测试**

覆盖：

```python
def test_subtitles_socket_rejected_in_assistant_mode(client) -> None:
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect("/ws/subtitles") as ws:
            ws.receive_text()
    assert caught.value.code == 4409


def test_existing_subtitle_socket_is_revoked_on_idle(client) -> None:
    client.app.state.runtime.force_state(RuntimeMode.SUBTITLES, PCMOwner.SUBTITLES, 1)
    with client.websocket_connect("/ws/subtitles") as ws:
        client.app.state.runtime.force_state(RuntimeMode.IDLE, PCMOwner.NONE, 2)
        with pytest.raises(WebSocketDisconnect) as caught:
            ws.receive_text()
    assert caught.value.code == 4409
```

再覆盖 subtitles→meeting 保持连接、meeting→idle 撤销，以及模式提交发生在 runtime client 注册后/SubtitleProxy client 注册前时不会留下订阅。

- [ ] **Step 3: 运行 RED**

Run: `uv run pytest tests/test_ui_server.py -q --no-cov`

Expected: 控制连接没有持续广播，字幕连接不检查模式。

- [ ] **Step 4: 实现单 writer 控制通道**

每个控制连接创建 `responses: asyncio.Queue[dict[str, Any]]` 和 runtime client；唯一 sender task 同时等待两者并调用 `websocket.send_json()`，避免两个协程并发写同一 WebSocket。每轮 `asyncio.wait(..., return_when=FIRST_COMPLETED)` 后取消并 await 未完成的临时 `queue.get()` task，不能累积悬挂读取。receiver 解析命令后创建 task：

```python
command_task = asyncio.create_task(bridge.handle(payload))
_accepted_control_tasks.add(command_task)
command_task.add_done_callback(_accepted_control_tasks.discard)
response = await asyncio.shield(command_task)
await responses.put(response)
```

连接 finally 只取消 sender、移除 runtime client，不取消 `_accepted_control_tasks` 中已接受的事务；task 的异常必须由 done callback 消费并记录稳定错误，不泄漏完整外部响应。

- [ ] **Step 5: 实现 race-safe 字幕订阅**

先 `runtime.runtime_events.add_client()`，同步读取 initial snapshot；非法则直接 close 4409。`await websocket.accept()` 后再次 `latest_nowait()`（队列为空时回退 initial）检查资格；合法时再同步注册 SubtitleProxy client。并发 revoker 等待后续状态，发现 assistant/idle 时 close 4409。finally 同时移除两个 client。字幕客户端发送的文本只用于维持连接，不路由控制命令。

- [ ] **Step 6: 运行 GREEN**

Run: `uv run pytest tests/test_ui_server.py tests/test_control.py tests/test_runtime_events.py -q --no-cov`

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add src/sona/ui/server.py tests/test_ui_server.py
git commit -m "feat(ui): 广播运行时状态并约束字幕订阅"
```

### Task 9: 在前端 CommandChannel 实现 ownership revision 仲裁

**Files:**
- Modify: `ui/src/contracts/meetingContract.ts`
- Modify: `ui/src/protocol.ts`
- Modify: `ui/src/hooks/useCommandSocket.ts`
- Modify: `ui/src/hooks/useCommandSocket.test.ts`
- Modify: `ui/src/stores/uiSettingsStore.ts`
- Modify: `ui/src/stores/uiSettingsStore.test.ts`

**Interfaces:**
- RuntimeMode: 增加 `subtitles`；新增 `PCMOwner`。
- `RuntimeStateSnapshot`: `mode`、`pcm_owner`、`runtime_revision` 成为合法首快照必需字段。
- Command: 增加 `start_subtitles`。
- Hook: 暴露 `snapshot`、`highestRuntimeRevision`、`reconcileRuntime()`。

- [ ] **Step 1: 写协议与乱序 RED 测试**

```typescript
it("keeps ownership from the highest revision while accepting fresh UI fields", () => {
  const applyState = vi.fn();
  const channel = new CommandChannel({ applyState });
  channel.receive(runtimeEvent(snapshot({ mode: "subtitles", pcm_owner: "subtitles", runtime_revision: 9 })));
  channel.receive(runtimeEvent(snapshot({ mode: "assistant", pcm_owner: "assistant", runtime_revision: 8, mic_muted: true })));

  expect(applyState).toHaveBeenLastCalledWith(expect.objectContaining({
    mode: "subtitles",
    pcm_owner: "subtitles",
    runtime_revision: 9,
    mic_muted: true,
  }));
});
```

覆盖 broadcast→ack、ack→broadcast、相同 revision ownership 完全一致、旧 ack 不回退 mode、`start_subtitles` 自动带 contract version、非法首快照不把 channel 标 ready。

- [ ] **Step 2: 写 reconcileRuntime RED 测试**

mock `fetch("/api/runtime")` 返回更高 revision，断言 hook 应用并清除 reconciling；返回低 revision 时不得覆盖 ownership；HTTP 失败只返回 `CommandError(service_unavailable)`，不发送反向模式命令。

- [ ] **Step 3: 运行 RED**

Run: `cd ui && npm test -- --run src/hooks/useCommandSocket.test.ts src/stores/uiSettingsStore.test.ts`

Expected: TypeScript contract 不接受 subtitles/pcm_owner，channel 未保存最高 revision。

- [ ] **Step 4: 实现 ownership merge**

在 `CommandChannel` 保存 `latestState` 与 `highestRuntimeRevision`：

```typescript
const OWNERSHIP_KEYS = [
  "mode",
  "pcm_owner",
  "active_meeting_id",
  "meeting_state",
  "meeting_started_at",
  "runtime_revision",
] as const;

function mergeRuntimeState(
  current: RuntimeStateSnapshot | null,
  incoming: RuntimeStateSnapshot,
): RuntimeStateSnapshot {
  if (!current || incoming.runtime_revision >= current.runtime_revision) return incoming;
  return {
    ...incoming,
    mode: current.mode,
    pcm_owner: current.pcm_owner,
    active_meeting_id: current.active_meeting_id,
    meeting_state: current.meeting_state,
    meeting_started_at: current.meeting_started_at,
    runtime_revision: current.runtime_revision,
  };
}
```

相同 revision 如果 ownership 字段不一致，忽略该消息的 ownership 并调用可注入 `onProtocolError`；非 ownership 字段仍按最新到达消息更新。`send()` timeout 保持拒绝 `CommandError("timeout")`，由 App 决定进入对账。

`useCommandSocket` 保存最后合法 snapshot，并实现 `reconcileRuntime()` GET `/api/runtime` 后交给同一 `channel.receiveState()`，禁止旁路 revision 规则。

- [ ] **Step 5: 运行 GREEN**

Run: `cd ui && npm test -- --run src/hooks/useCommandSocket.test.ts src/stores/uiSettingsStore.test.ts`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add ui/src/contracts/meetingContract.ts ui/src/protocol.ts ui/src/hooks/useCommandSocket.ts ui/src/hooks/useCommandSocket.test.ts ui/src/stores/uiSettingsStore.ts ui/src/stores/uiSettingsStore.test.ts
git commit -m "feat(ui): 按 revision 仲裁运行时所有权"
```

### Task 10: 让 App 首快照门控并在 ack 后提交工作区

**Files:**
- Modify: `ui/src/App.tsx`
- Modify: `ui/src/App.test.tsx`
- Modify: `ui/src/components/StatusBar.tsx`
- Modify: `ui/src/components/StatusBar.css`
- Modify: `ui/src/components/StatusBar.test.ts`

**Interfaces:**
- Pure resolver: `resolveWorkspaceTab(mode, persistedTab, currentTab)`。
- Pending state: `pendingTab`、`reconciling`、`switchError`。
- Navigation: meeting Tab 只导航；assistant/subtitles 才发送模式命令。
- Mount rule: 首个合法 snapshot 前不挂载 SubtitleStream；只有 server mode subtitles 才挂载。

- [ ] **Step 1: 改写首屏 RED 测试**

把现有“localStorage subtitles 立即挂载”断言改为：首快照未就绪时字幕不挂载、不发命令；首快照 assistant 到达后显示 assistant；首快照 subtitles 到达后才显示字幕。

```typescript
it("does not mount subtitles from localStorage before authoritative state", () => {
  localStorage.setItem("sona:workspace-tab", "subtitles");
  commandSocket.ready = false;
  commandSocket.snapshot = null;
  act(() => root.render(<App />));
  expect(container.querySelector("[data-testid='subtitles-panel']")).toBeNull();
  expect(commandSocket.sendCommand).not.toHaveBeenCalled();
});
```

覆盖 mode meeting 强制会议 Tab；assistant/idle + persisted subtitles 回退 assistant；assistant/idle 保留 meeting 历史导航。

- [ ] **Step 2: 写显式切换 RED 测试**

点击字幕后让 `sendCommand` promise 保持 pending，断言 active tab 和 SubtitleStream 不变；resolve 为 mode subtitles 后才切换。reject 时保留原 tab 并显示错误。timeout 时显示“正在对账”，调用 `reconcileRuntime()`；随后更高 revision broadcast 为 subtitles 时完成切换，broadcast 为其他模式时清除 pending 并采用服务端模式。

覆盖快速重复点击只发送一次；pending assistant/subtitles 期间到达 meeting 状态立即强制会议 Tab；点击 meeting Tab 不发任何模式命令。

- [ ] **Step 3: 运行 RED**

Run: `cd ui && npm test -- --run src/App.test.tsx src/components/StatusBar.test.ts`

Expected: App 当前先写 activeTab，且会按 localStorage 直接挂载字幕。

- [ ] **Step 4: 实现 resolver 与 pending 状态机**

```typescript
export function resolveWorkspaceTab(
  mode: RuntimeMode,
  persisted: WorkspaceTab,
  current: WorkspaceTab | null,
): WorkspaceTab {
  if (mode === "meeting") return "meeting";
  if (mode === "subtitles") return "subtitles";
  const candidate = current ?? persisted;
  return candidate === "subtitles" ? "assistant" : candidate;
}
```

`activeTab` 初值改为 null；首 snapshot effect 通过 resolver 设置。`handleTabChange("meeting")` 只提交导航和 localStorage。assistant/subtitles 路径先设置 pending，再发送 `start_assistant`/`start_subtitles`；仅当 hook 的最高已知 snapshot mode 等于目标且 revision 不低于发起时 revision 才提交 Tab。

timeout catch 调用 `reconcileRuntime()` 并保持 pending；明确 `mode_conflict`/`service_unavailable` 清除 pending、保留原 Tab并展示现有 Toast。StatusBar 接收 pending/reconciling props，禁用 assistant/subtitle 重复点击并显示“切换中”或“正在对账”。

- [ ] **Step 5: 运行 GREEN**

Run: `cd ui && npm test -- --run src/App.test.tsx src/components/StatusBar.test.ts src/hooks/useCommandSocket.test.ts`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add ui/src/App.tsx ui/src/App.test.tsx ui/src/components/StatusBar.tsx ui/src/components/StatusBar.css ui/src/components/StatusBar.test.ts
git commit -m "feat(ui): 按服务端状态提交工作区切换"
```

### Task 11: 增加 AudioHub、interaction 与 TTS 源块诊断

**Files:**
- Modify: `src/sona/audio/hub.py`
- Modify: `src/sona/ui/runtime.py`
- Modify: `src/sona/ui/assistant_bridge.py`
- Modify: `tests/test_audio_hub.py`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_assistant_bridge.py`

**Interfaces:**
- `AudioHub.sink_diagnostics()` 返回每 sink 的 `queued_chunks` 与 `dropped_chunks`。
- `UIRuntime.diagnostics()` 包含 interaction queue drops 与 coordinator `last_transition`。
- `StatusBridgeObserver.tts_source_diagnostics` 包含 `first_chunk_ms`、`chunk_count`、`max_source_chunk_gap_ms`、`median_source_chunk_gap_ms`、`source_chunk_gaps_over_200ms`。

- [ ] **Step 1: 写 AudioHub 与 interaction drop RED 测试**

填满容量 1 的队列后推入第二块：AudioHub sink 保持现有“丢最旧块、保留最新块”，interaction queue 保持现有“保留已排队块、拒绝新块”；两者计数都精确加 1。读取 diagnostics 不清零计数，不暴露音频 bytes。

- [ ] **Step 2: 写 TTS 源块节奏 RED 测试**

给 `StatusBridgeObserver` 注入 fake monotonic clock，依次发送 TTS started、三个 `TTSAudioRawFrame`（间隔 50ms、250ms）、TTS stopped。断言 chunk_count=3、max gap=250、median gap=150、over_200ms=1；新一轮 TTS started 重置上一轮采样。字段名必须包含 `source_chunk_gap`，不得出现 `underrun`。

- [ ] **Step 3: 运行 RED**

Run: `uv run pytest tests/test_audio_hub.py tests/test_runtime.py tests/test_assistant_bridge.py -q --no-cov`

Expected: 现有内部 drop 计数未公开，TTS 仅统计 chunk 数。

- [ ] **Step 4: 实现只读诊断 dataclass**

```python
@dataclass(frozen=True, slots=True)
class SinkDiagnostics:
    queued_chunks: int
    dropped_chunks: int


@dataclass(frozen=True, slots=True)
class TTSSourceDiagnostics:
    first_chunk_ms: float | None
    chunk_count: int
    max_source_chunk_gap_ms: float | None
    median_source_chunk_gap_ms: float | None
    source_chunk_gaps_over_200ms: int
```

AudioHub 返回 name→`SinkDiagnostics` 的新 dict，不能返回内部 queue 或 `_SinkState`。UIRuntime 在 `put_nowait()` 抛 `QueueFull` 并拒绝新 interaction chunk 时递增 `_interaction_dropped_chunks`，不改变既有背压策略。TTS 使用 `statistics.median` 计算已完成间隔；`first_chunk_ms` 从 TTS started 到第一个源音频 frame，和现有用户 turn `tts_ttfb_ms` 指标并存。

- [ ] **Step 5: 运行 GREEN**

Run: `uv run pytest tests/test_audio_hub.py tests/test_runtime.py tests/test_assistant_bridge.py -q --no-cov`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/sona/audio/hub.py src/sona/ui/runtime.py src/sona/ui/assistant_bridge.py tests/test_audio_hub.py tests/test_runtime.py tests/test_assistant_bridge.py
git commit -m "feat(diagnostics): 暴露音频队列与 TTS 源块指标"
```

### Task 12: 扩展 `/api/services` 工作负载健康与原始诊断

**Files:**
- Modify: `src/sona/ui/server.py`
- Modify: `tests/test_ui_server.py`
- Modify: `ui/src/components/StatusBar.tsx`
- Modify: `ui/src/components/StatusBar.test.ts`

**Interfaces:**
- WLK service additive fields: `workload`、`ws_state`、`reconnect_count`、`last_event_age_ms`。
- Top-level additive field: `diagnostics`，包含 audio hub、interaction、subtitles、tts、last transition。
- Compatibility: 现有 `services[]`、`name/status/url/target_model/model_present` 字段保持。

- [ ] **Step 1: 写 API RED 测试**

```python
def test_services_adds_runtime_workload_diagnostics(client) -> None:
    response = client.get("/api/services")
    payload = response.json()
    wlk = next(item for item in payload["services"] if item["name"] == "wlk")
    assert wlk["workload"] == "paused"
    assert wlk["ws_state"] == "paused"
    assert wlk["reconnect_count"] == 0
    assert wlk["last_event_age_ms"] is None
    assert set(payload["diagnostics"]) == {
        "audio_hub", "interaction", "subtitles", "tts", "last_transition"
    }
```

覆盖 HTTP status 与 workload 独立：WLK HTTP ok + paused 仍为 status ok；HTTP ok + committed subtitle backoff 时 workload degraded；长时间无 event 但连接 ready 时不 degraded；runtime 未就绪时保留旧三服务响应并返回空诊断。

- [ ] **Step 2: 写 StatusBar RED 测试**

mock additive response，断言 WLK popover 同时显示进程状态与 workload/ws_state；不根据 `last_event_age_ms` 显示“关闭游戏”或其他外部负载归因文字。旧 response 不含新字段时仍可渲染。

- [ ] **Step 3: 运行 RED**

Run: `uv run pytest tests/test_ui_server.py -q --no-cov && cd ui && npm test -- --run src/components/StatusBar.test.ts`

Expected: API 未聚合 runtime diagnostics，前端类型没有 additive 字段。

- [ ] **Step 4: 实现健康聚合**

`services()` 保持三项 HTTP probe 并发；探活完成后定位 name=wlk，将 `runtime.subtitle_proxy.diagnostics(runtime.snapshot().pcm_owner)` 的四个规定字段合并进去。`status` 不被 workload 覆盖。top-level `diagnostics` 使用 `runtime.diagnostics()` 的 JSON-safe 副本；runtime 不存在或方法异常时返回五个键、值为 null/空对象，并记录异常类型。

StatusBar 只增加原始状态文本；现有 service status 色灯仍由 HTTP `status` 决定，workload degraded 可在详情中单独标注，不把 `last_event_age_ms` 单独变成红灯。

- [ ] **Step 5: 运行 GREEN**

Run: `uv run pytest tests/test_ui_server.py -q --no-cov && cd ui && npm test -- --run src/components/StatusBar.test.ts`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/sona/ui/server.py tests/test_ui_server.py ui/src/components/StatusBar.tsx ui/src/components/StatusBar.test.ts
git commit -m "feat(diagnostics): 区分进程与语音工作负载健康"
```

### Task 13: 完成跨层回归、文档、真实验收与发布准备

**Files:**
- Modify: `docs/系统总体架构与详细设计方案.md`
- Modify: `docs/实时语音交互与字幕-方案与最佳实践.md`
- Modify: `docs/会议助手后端运行与前后端联调.md`
- Modify: `README.md` only if its launch/behavior summary contradicts the implemented runtime modes.

**Interfaces:**
- Documents: 四种模式、PCM owner、两阶段切换、控制广播、字幕 WS 4409、SRT epoch、诊断字段、整体回退。
- Release boundary: 前后端同版发布，整体回退，不删除 interrupted meeting record。

- [ ] **Step 1: 运行专项跨层回归**

Run:

```bash
uv run pytest tests/test_runtime_events.py tests/test_runtime_mode.py tests/test_meeting_session.py tests/test_subtitle_proxy.py tests/test_runtime.py tests/test_control.py tests/test_ui_server.py tests/test_audio_hub.py tests/test_assistant_bridge.py -q --no-cov
cd ui && npm test -- --run src/hooks/useCommandSocket.test.ts src/App.test.tsx src/components/StatusBar.test.ts src/components/SubtitleStream.test.tsx
```

Expected: PASS；测试覆盖 target prepare 失败不停止来源、source quiesce 补偿、meeting publish 顺序、存量字幕 WS 撤销、乱序 revision 与首快照门控。

- [ ] **Step 2: 更新权威文档**

文档中的运行拓扑明确：服务进程可常驻，但 PCM workload 单 owner；`SubtitleProxy.start()` 不代表普通字幕连接已激活；会议结束进入 idle 且不会自动恢复字幕。记录 `/ws/subtitles` 只读和 4409 行为、`start_subtitles` 命令、revision 范围、SRT epoch 归档、`/api/services` additive 字段及整体回退方法。

所有持久化命令使用 `git`、`uv`、`npm`、`python3` 等原生命令，不写本机 wrapper、凭据、私人路径或实际终端历史。

- [ ] **Step 3: 执行后端全量质量门禁**

Run:

```bash
SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/
uv run mypy src/
uv run ruff check src/ tests/
```

Expected: 全部退出码为 0；pytest 分支覆盖率不低于 80%。

- [ ] **Step 4: 执行前端全量质量门禁**

Run:

```bash
cd ui && npm test -- --run
cd ui && npm run build
```

Expected: 全部退出码为 0，生产构建成功。

- [ ] **Step 5: 执行 localhost 与 LAN 真实闭环**

先用默认 localhost 启动，再用 `SONA_BIND_HOST=lan scripts/run-all.sh` 启动；每轮完成以下场景并记录时间、最高 revision、mode、pcm_owner 和诊断计数，不记录音频或完整转写：

1. 启动后 assistant 可用且 WLK 普通字幕 workload 为 paused；assistant PCM 不进入 WLK。
2. assistant→subtitles→assistant，UI 只在 ack/权威状态后切换，SRT epoch 正确归档，TTS 恢复正常。
3. assistant→meeting→idle，recording 事件晚于 mode commit，EOF、PostgreSQL confirmed、speaker 和纪要正常，会议后字幕不自动恢复。
4. subtitles→meeting，既有只读字幕 WS 可保留；meeting→idle 后收到 4409。
5. 两个浏览器窗口发起相反模式命令，最终应用相同最高 revision，服务端无双 PCM owner。
6. WLK 未 ready 时请求字幕，assistant 会话和 LM response chain 不变；人工延迟 ack 后通过广播或 `/api/runtime` 收敛。
7. 活动字幕 WLK 断线重连，新 epoch 不覆盖旧 SRT，workload 显示 degraded 后恢复，控制命令仍可用。
8. 外部高负载下只核对 drop/gap/source_chunk_gap 原始诊断可读，不要求自动提示。
9. 检查 runtime 输出目录和 PostgreSQL：没有新增音频持久化；prepared meeting abort 记录为 `interrupted/mode_switch_aborted` 且未删除。

- [ ] **Step 6: 执行回退演练**

确认本变更不含数据库 migration、模型或端口变化。使用一个临时测试 meeting 触发 prepared abort，验证旧字段可读取；随后停止服务、切回发布前完整前后端 commit、重启并确认 assistant 基础链路。不得只回退前端或后端，不删除会议数据。

- [ ] **Step 7: 提交文档**

```bash
git add docs/系统总体架构与详细设计方案.md docs/实时语音交互与字幕-方案与最佳实践.md docs/会议助手后端运行与前后端联调.md README.md
git commit -m "docs(runtime): 更新工作负载仲裁与验收手册"
```

若 `README.md` 实测无需修改，从 `git add` 命令中移除该路径。

## Final Verification Checklist

- [ ] 规格 §5 的每条核心不变量均有自动化测试或真实验收步骤。
- [ ] 规格 §7 转换表中的每条来源/命令/目标路径均被 `tests/test_runtime_mode.py` 覆盖。
- [ ] 规格 §7.4 的非抢占、断线不取消和 shutdown 取消语义均有独立测试。
- [ ] 规格 §8 的双控制通道广播、ack 任意顺序、前端首快照和 timeout reconcile 均有测试。
- [ ] 规格 §8.3 的字幕 WS 建连、存续、竞态、subtitles→meeting 保留和 meeting→idle 撤销均有测试。
- [ ] 规格 §9 的双层 PCM gate、切换 drain、SRT epoch、reset 和会议不写 SRT 均有测试。
- [ ] 规格 §10 的 WLK additive health、drop/gap/source_chunk_gap 与 last transition 均有测试，且无外部进程归因。
- [ ] 规格 §13 的数据、安全和隐私边界未改变。
- [ ] 默认模型、下载策略、LAN/localhost 监听、端口和回声双防线未改变。
- [ ] 后端 pytest、mypy、ruff 与前端 Vitest、build 全绿。
- [ ] git diff 中不存在音频文件、数据库 dump、日志、凭据、私有环境变量或本机 wrapper 命令。

## Commit Plan

1. `feat(runtime): 增加字幕模式与 PCM 所有权契约`
2. `feat(runtime): 增加权威状态广播器`
3. `refactor(subtitles): 拆分字幕与会议采集准备提交`
4. `feat(subtitles): 隔离连接 epoch 与 SRT 边界`
5. `refactor(meeting): 拆分会议启动事务与事件发布`
6. `feat(runtime): 实现两阶段工作负载仲裁`
7. `feat(runtime): 接入 PCM 门控与状态广播`
8. `feat(ui): 广播运行时状态并约束字幕订阅`
9. `feat(ui): 按 revision 仲裁运行时所有权`
10. `feat(ui): 按服务端状态提交工作区切换`
11. `feat(diagnostics): 暴露音频队列与 TTS 源块指标`
12. `feat(diagnostics): 区分进程与语音工作负载健康`
13. `docs(runtime): 更新工作负载仲裁与验收手册`

这些提交用于开发审查与回归定位；产品发布必须将 1-13 作为同一前后端版本整体发布或整体回退。
