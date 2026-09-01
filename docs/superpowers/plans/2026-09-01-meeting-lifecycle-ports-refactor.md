---
title: "会议生命周期与窄 Ports 重构实施计划"
status: draft
type: execution_plan
date: 2026-09-01
owners: ["voice-realtime-core"]
related_documents:
  - "docs/architecture/系统总体架构与详细设计方案.md"
  - "contracts/meeting-assistant/v1/README.md"
---

# Meeting Lifecycle and Ports Refactor Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task, with a review checkpoint after each commit.

**Goal:** 用 typed capture gateway、窄 repository ports、转录持久化服务和 finalization use case 取代 `MeetingSession` 中的 `Any/getattr` 与多职责编排，同时严格保持当前封存顺序、恢复语义和事务边界。

**Architecture:** `meeting/ports.py` 定义 application 所需能力；`PostgresMeetingRepository` 可实现多个窄 protocol，但 SQL 和 transaction 仍留在具体方法；`TranscriptPersistence` 负责在线 reconcile 与 RecoveryJournal fallback/replay；`MeetingFinalizer` 固化 stop 后的业务顺序；`MeetingSession` 只保留状态转换、两阶段 start、event dispatch 和 cleanup。

**Tech Stack:** Python 3.12、asyncio、Pydantic v2、PostgreSQL、pytest、Ruff、mypy。

---

## 执行边界

- 工作目录：`/Users/hrygo/Documents/voice-realtime`
- 前置：完成 `2026-09-01-subtitle-proxy-refactor.md`，`SubtitleProxy.last_window` 已是 typed property。
- 后续：UI backend composition 计划依赖本计划的 repository/context 类型。
- 当前 `MeetingSession.stop()` 的正确顺序必须原样保持：

```text
set FINALIZING + publish
→ gateway.finish_capture
→ persist final window
→ replay pending journal
→ apply speaker remap
→ repository.finalize_transcript
→ repository.create_minutes
→ publish meeting/minutes events
→ release listeners/session state/resume summary worker
```

- 不得改成“先 finalize transcript 再 remap/replay”；这会让终态会议拒绝后续写入或产生错误 revision。
- `RecoveryJournal` 是独立 transient component，不是 repository；不要设计 `RecoveryStore` 混入 PostgreSQL CRUD。
- `PostgresMeetingRepository._connection()` 已存在并只管理 pool connection；各方法显式 `connection.transaction()` 是正确事务边界。不得再创建隐藏 commit/rollback 的 `PgSession`。
- 保持 `MeetingSession(repository, gateway=None, ..., subtitle_proxy=None)` 的兼容入口；`subtitle_proxy=` 仍作为 `gateway` 的 keyword alias，但解析后内部只保存 typed gateway。
- 不改变 schema、migration、meeting HTTP/WS contract、speaker identity scope、minutes idempotency key 或 recovery JSONL 格式。
- start/stop/cancel 仍必须 shield 必要 cleanup；不得吞掉 `CancelledError`。

## 目标文件

- Create: `src/voice_realtime/meeting/ports.py`
- Create: `src/voice_realtime/meeting/persistence.py`
- Create: `src/voice_realtime/meeting/finalization.py`
- Modify: `src/voice_realtime/meeting/session.py`
- Modify: `src/voice_realtime/meeting/repository.py`
- Modify: `src/voice_realtime/meeting/recovery.py`
- Modify: `src/voice_realtime/meeting/runtime_mode.py`
- Modify: `src/voice_realtime/ui/subtitle_proxy.py`
- Create: `tests/test_meeting_ports.py`
- Create: `tests/test_meeting_finalization.py`
- Modify: `tests/test_meeting_session.py`
- Modify: `tests/test_meeting_repository.py`
- Modify: `tests/test_meeting_recovery.py`
- Modify: `tests/test_runtime_mode.py`

## Task 1: 定义 capture 与 repository 窄 ports

**Files:**

- Create: `src/voice_realtime/meeting/ports.py`
- Modify: `src/voice_realtime/meeting/repository.py`
- Modify: `src/voice_realtime/meeting/recovery.py`
- Modify: `src/voice_realtime/ui/subtitle_proxy.py`
- Create: `tests/test_meeting_ports.py`
- Modify: `tests/test_meeting_repository.py`
- Modify: `tests/test_meeting_recovery.py`

- [ ] **Step 1: 写 protocol contract 红灯测试**

使用分别只实现 `MeetingStore`、`TranscriptStore`、`SpeakerStore`、`MinutesStore` 的 fakes；证明 finalizer 不需要 list/update/delete，API 不需要 capture，RecoveryJournal 只需要 replay operation 涉及的方法。

- [ ] **Step 2: 定义 capture value 和 exception**

```python
@dataclass(frozen=True, slots=True)
class CaptureLease:
    owner: str
    generation: int


@dataclass(frozen=True, slots=True)
class CaptureGap:
    source_epoch: int
    start_ms: int
    end_ms: int


class CaptureFinalizationTimeout(TimeoutError):
    def __init__(self, last_window: TranscriptWindow | None) -> None:
        super().__init__("capture finalization timed out")
        self.last_window = last_window
```

`SubtitleProxy` 使用这些类型并继续 re-export 当前 `CapturePreparation`、`TranscriptionGap`、`FinalizationTimeoutError` 名称作为兼容 alias；内部只保留一个对象定义。

- [ ] **Step 3: 定义精确 gateway protocol**

```python
class MeetingCaptureGateway(Protocol):
    @property
    def last_window(self) -> TranscriptWindow | None: ...
    def add_event_listener(self, listener: WindowListener) -> None: ...
    def remove_event_listener(self, listener: WindowListener) -> None: ...
    def add_gap_listener(self, listener: GapListener) -> None: ...
    def remove_gap_listener(self, listener: GapListener) -> None: ...
    async def prepare_capture(
        self,
        owner: str,
        *,
        timeout_secs: float,
        speaker_count_hint: int | None,
    ) -> CaptureLease: ...
    def commit_capture(self, preparation: CaptureLease) -> None: ...
    async def abort_prepared_capture(self, preparation: CaptureLease) -> None: ...
    async def finish_capture(self, *, timeout_secs: float) -> TranscriptWindow: ...
    async def abort_capture(self) -> None: ...
```

- [ ] **Step 4: 拆分 repository protocols，保留兼容 aggregate**

`MeetingStore` 包含 `check_writable/create/get/list/update_title/set_status/delete`；`TranscriptStore` 包含 `reconcile_window/finalize_transcript/get_transcript`；`SpeakerStore` 包含 `get_speakers/rename_speaker/apply_speaker_remapping`；`MinutesStore` 包含 `get_latest_minutes/get_minutes/create/claim/complete/fail`；`RepositoryMaintenance` 只含 `recover_stale`；`ClosableStore` 只含 `close`。`MeetingRepository` 作为这些 protocol 的兼容 aggregate 保留原 import，并覆盖当前 concrete repository 的实际消费面。

`RecoveryReplayRepository` 只组合 journal 实际需要的 `get_meeting/reconcile_window/set_status/finalize_transcript/create_minutes`。将 `recovery.py` 的参数注解改为该 protocol，不把 journal 方法加进 repository。

- [ ] **Step 5: 运行并提交 ports**

```bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run --extra dev pytest \
  tests/test_meeting_ports.py tests/test_meeting_repository.py tests/test_meeting_recovery.py \
  -q --no-cov
uv run --extra dev ruff check src/voice_realtime/meeting/ports.py \
  src/voice_realtime/meeting/repository.py src/voice_realtime/meeting/recovery.py \
  src/voice_realtime/ui/subtitle_proxy.py tests/test_meeting_ports.py
uv run --extra dev mypy src/voice_realtime/meeting src/voice_realtime/ui/subtitle_proxy.py
git add src/voice_realtime/meeting/ports.py src/voice_realtime/meeting/repository.py \
  src/voice_realtime/meeting/recovery.py src/voice_realtime/ui/subtitle_proxy.py \
  tests/test_meeting_ports.py tests/test_meeting_repository.py tests/test_meeting_recovery.py
git commit -m "refactor: define narrow meeting ports"
```

## Task 2: 提取转录持久化与 journal fallback

**Files:**

- Create: `src/voice_realtime/meeting/persistence.py`
- Modify: `src/voice_realtime/meeting/session.py`
- Create: `tests/test_meeting_finalization.py`
- Modify: `tests/test_meeting_session.py`
- Modify: `tests/test_meeting_recovery.py`

- [ ] **Step 1: 写 persistence 红灯测试**

覆盖相同 window signature 去重、先 replay 再 reconcile、repository 失败写 journal、journal 也失败时向上抛出、成功后清除 degraded、每个 meeting 的 signature 隔离。测试不用真实 PostgreSQL。

- [ ] **Step 2: 定义 RecoveryJournalPort 与 persistence**

```python
class RecoveryJournalPort(Protocol):
    async def append(self, meeting_id: UUID, window: TranscriptWindow) -> object: ...
    async def replay_meeting(
        self, repository: RecoveryReplayRepository, meeting_id: UUID
    ) -> int: ...


class TranscriptPersistence:
    @property
    def degraded(self) -> bool: ...
    async def reconcile(
        self, meeting_id: UUID, window: TranscriptWindow
    ) -> TranscriptReconcileResult | None: ...
    async def replay_pending(self, meeting_id: UUID) -> int: ...
```

它只处理 transcript/recovery，不调用 capture gateway、不发布 UI event、不 finalize meeting。`MeetingSession._on_window` 在 fatal persistence error 时继续负责 abort capture。

- [ ] **Step 3: 迁移在线 window 处理**

`MeetingSession` 使用 `TranscriptPersistence.reconcile()`，根据返回结果加载 speaker names 并 publish `transcript_reconciled`。`storage_health` 委托 persistence 的 degraded 状态；删除 `_last_window_signature`、`_persist_window` 和动态 journal method probing。

- [ ] **Step 4: 运行并提交 persistence**

```bash
uv run --extra dev pytest tests/test_meeting_finalization.py tests/test_meeting_session.py tests/test_meeting_recovery.py -q --no-cov
uv run --extra dev ruff check src/voice_realtime/meeting/persistence.py src/voice_realtime/meeting/session.py \
  tests/test_meeting_finalization.py tests/test_meeting_session.py
uv run --extra dev mypy src/voice_realtime/meeting
git add src/voice_realtime/meeting/persistence.py src/voice_realtime/meeting/session.py \
  tests/test_meeting_finalization.py tests/test_meeting_session.py tests/test_meeting_recovery.py
git commit -m "refactor: isolate meeting transcript persistence"
```

## Task 3: 提取正确顺序的 MeetingFinalizer

**Files:**

- Create: `src/voice_realtime/meeting/finalization.py`
- Modify: `src/voice_realtime/meeting/session.py`
- Modify: `tests/test_meeting_finalization.py`
- Modify: `tests/test_meeting_session.py`

- [ ] **Step 1: 写严格调用顺序与失败清理测试**

测试 normal、typed timeout、final window 为 None、journal replay、speaker remap、reconcile failure、remap failure、finalize failure、minutes failure、caller cancellation。用调用日志断言每个步骤最多一次；timeout 仍以 `INTERRUPTED/finalization_timeout` finalize。若 `finish_capture` 尚未成功/typed-timeout 就失败，finalizer 必须 shield 一次 `abort_capture()`；若 capture 已关闭后下游失败，不得重复 abort。

- [ ] **Step 2: 定义结果和值对象**

```python
@dataclass(frozen=True, slots=True)
class MeetingFinalizationResult:
    record: MeetingRecord
    minutes: MinutesRecord
    final_window: TranscriptWindow | None
    timed_out: bool


class MeetingFinalizer:
    async def finalize(self, meeting_id: UUID) -> MeetingFinalizationResult: ...
```

不要返回当前 stop 流程从未读取的 `TranscriptDocument`，也不要把 cleanup/listener/event publisher 塞入结果。

- [ ] **Step 3: 按当前代码顺序实现**

```python
final_window = await gateway.finish_capture(timeout_secs=timeout_secs)
if final_window is not None:
    await persistence.reconcile(meeting_id, final_window)
await persistence.replay_pending(meeting_id)
if final_window is not None and final_window.speaker_remap:
    await speakers.apply_speaker_remapping(meeting_id, dict(final_window.speaker_remap))
record = await transcripts.finalize_transcript(
    meeting_id,
    final_status=MeetingStatus.INTERRUPTED if timed_out else MeetingStatus.COMPLETED,
    reason="finalization_timeout" if timed_out else None,
)
minutes = await minutes_store.create_minutes(
    meeting_id,
    idempotency_key=f"meeting:{meeting_id}:minutes:v1",
)
```

typed timeout 从 exception 读取 `last_window` 后执行同样的 persist/replay/remap/finalize/minutes 顺序。不要捕获其他 `TimeoutError` 伪装成 capture timeout。

`MeetingFinalizer` 内部维护 `capture_closed`。在异常或 cancellation 路径中，仅当它仍为 false 时创建 cleanup task、`asyncio.shield()` 等待 `gateway.abort_capture()`，并在 cleanup error 时保留原异常。这样失败发生在 remap/finalize/minutes 时也不会要求 `MeetingSession` 猜测 capture 状态。

- [ ] **Step 4: 让 MeetingSession 只协调状态与事件**

`MeetingSession.stop()` 仍先 set/publish FINALIZING，然后调用 finalizer，按 result publish meeting/minutes events；`_settle_failed_stop` 收敛为标记 interrupted（capture cleanup 已由 finalizer 完成），shielded `_release_stopped_session` 保留。`last_window` 直接返回 `gateway.last_window`。

- [ ] **Step 5: 运行并提交 finalizer**

```bash
uv run --extra dev pytest tests/test_meeting_finalization.py tests/test_meeting_session.py tests/test_runtime_mode.py -q --no-cov
uv run --extra dev ruff check src/voice_realtime/meeting/finalization.py src/voice_realtime/meeting/session.py \
  tests/test_meeting_finalization.py tests/test_meeting_session.py
uv run --extra dev mypy src/voice_realtime/meeting
git add src/voice_realtime/meeting/finalization.py src/voice_realtime/meeting/session.py \
  tests/test_meeting_finalization.py tests/test_meeting_session.py tests/test_runtime_mode.py
git commit -m "refactor: isolate meeting finalization"
```

## Task 4: 收紧 start/runtime protocols 与 repository 小型 DRY

**Files:**

- Modify: `src/voice_realtime/meeting/session.py`
- Modify: `src/voice_realtime/meeting/runtime_mode.py`
- Modify: `src/voice_realtime/meeting/repository.py`
- Modify: `tests/test_runtime_mode.py`
- Modify: `tests/test_meeting_repository.py`

- [ ] **Step 1: 移除 start/summary 的动态探测**

`MeetingPreparation.capture` 改为 `CaptureLease`；`MeetingWorkload.prepare_start/commit_start/publish_started/abort_start` 使用 `MeetingPreparation` 而非 `Any`。定义可选 `SummaryWorkloadControl`（async `requeue_for_recording` / `resume_after_recording`）；无 summary 时注入 Null Object，不用 `getattr`。

- [ ] **Step 2: 只提取真正重复的 bounded text validator**

```python
def _validate_bounded_text(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not 1 <= len(normalized) <= 200:
        raise ValueError(f"{label}长度必须为 1–200")
    if any(ord(char) < 32 for char in normalized):
        raise ValueError(f"{label}不能包含控制字符")
    return normalized
```

`_validate_title` 和 `_validate_display_name` 调用它以保留原错误消息。不要移动 SQL、合并不同 transaction 或增加 repository 自动 commit。

- [ ] **Step 3: 运行数据库与 runtime 回归并提交**

```bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run --extra dev pytest \
  tests/test_meeting_repository.py tests/test_meeting_session.py tests/test_meeting_recovery.py \
  tests/test_meeting_finalization.py tests/test_runtime_mode.py -q --no-cov
uv run --extra dev ruff check src/voice_realtime/meeting tests/test_meeting_repository.py tests/test_runtime_mode.py
uv run --extra dev mypy src
git diff --check
git add src/voice_realtime/meeting/session.py src/voice_realtime/meeting/runtime_mode.py \
  src/voice_realtime/meeting/repository.py tests/test_runtime_mode.py tests/test_meeting_repository.py
git commit -m "refactor: type meeting lifecycle boundaries"
```

## Task 5: 完整门禁

- [ ] **Step 1: 运行会议聚焦矩阵**

```bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run --extra dev pytest \
  tests/test_meeting_ports.py tests/test_meeting_finalization.py tests/test_meeting_session.py \
  tests/test_meeting_repository.py tests/test_meeting_recovery.py tests/test_meeting_api.py \
  tests/test_runtime_mode.py tests/test_ui_server.py -q --no-cov
```

- [ ] **Step 2: 运行项目门禁**

```bash
uv run --extra dev ruff check src tests
uv run --extra dev mypy src
git diff --check
```

- [ ] **Step 3: 人工核对事务与恢复**

确认 remap 在 finalize 前、journal replay 在 remap 前、minutes 在 finalize 后；每个 PostgreSQL transaction 仍在具体 repository 方法内；journal 仍只保存规范化文本/lifecycle，不保存 PCM 或 embedding。

## 完成标准

- [ ] `MeetingSession` constructor 和 runtime mode public command 行为兼容，内部无 repository/gateway/journal `Any/getattr`。
- [ ] finalization 调用顺序由测试精确锁定。
- [ ] repository 按消费者拆成窄 ports，`PostgresMeetingRepository` 继续兼容 aggregate。
- [ ] RecoveryJournal 仍是独立组件，SQL transaction 边界未隐藏。
- [ ] normal、timeout、failure、cancel cleanup 均最多执行一次。
