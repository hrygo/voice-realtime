# Meeting Assistant Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deliver the meeting-assistant backend, PostgreSQL persistence, real-time transcription lifecycle, AI minutes, versioned HTTP/WebSocket contracts, fixtures, tests, and documentation without implementing the meeting React UI.

**Architecture:** `RuntimeModeCoordinator` makes assistant/meeting/idle mutually exclusive; `SubtitleProxy` evolves into a meeting-capable transcription gateway while retaining its compatibility surface. `MeetingSession` reconciles WLK windows into PostgreSQL, `MeetingSummaryService` creates versioned evidence-backed minutes, and FastAPI exposes contract-first v1 HTTP/WS interfaces.

**Tech Stack:** Python 3.12, asyncio, FastAPI, Pydantic v2, psycopg 3 async pool, PostgreSQL 18, WhisperLiveKit, LM Studio native `/api/v1/chat`, pytest, mypy strict, ruff.

**Spec:** `docs/superpowers/specs/2026-08-21-meeting-assistant-design.md`

## Global Constraints

- Python remains strictly locked to 3.12.
- Backend implementation only: do not modify meeting UI behavior under `ui/src/`.
- Keep `vr-ui` and `vr-interact` single-owner semantics; meeting mode belongs to `vr-ui`.
- Meeting mode must stop the entire Pipecat interaction pipeline; prompts or TTS mute are not sufficient.
- Microphone is the only V1 audio source; never persist raw audio.
- PostgreSQL `knowledge.voice_realtime` is the fact source; recovery journal is transient only.
- All services, PostgreSQL access, CORS, and WebSocket Origins remain loopback-only.
- WLK/LM/PG model or service absence is fail-fast where required; no implicit model download.
- Preserve existing public routes while adding canonical `/api/v1` and `/ws/v1` routes.
- Public JSON uses snake_case, RFC 3339 UTC timestamps, integer relative milliseconds, and opaque UUID strings.
- Use parameterized SQL, a least-privilege application role, bounded queues, stable error codes, and redacted logs.
- Tests must be written before implementation; each worker runs targeted pytest, mypy, and ruff for owned files.
- Agents are not alone in the repository: never revert other agents' edits; adapt to shared changes.

## Parallel Ownership

| Workstream | Exclusive ownership |
|---|---|
| A — domain/storage | `src/voice_realtime/meeting/models.py`, `repository.py`, `migrations.py`, `recovery.py`, `migrations/`, `src/voice_realtime/config.py`, `pyproject.toml`, `uv.lock`, `tests/test_meeting_models.py`, `tests/test_meeting_repository.py`, `tests/test_meeting_recovery.py` |
| B — transcription/runtime | `src/voice_realtime/meeting/transcript.py`, `session.py`, `runtime_mode.py`, `src/voice_realtime/ui/subtitle_proxy.py`, `runtime.py`, `protocol.py`, `control.py`, `src/voice_realtime/subtitles/launcher.py`, corresponding owned tests |
| C — summary/API/contracts | `src/voice_realtime/meeting/summary.py`, `api.py`, `events.py`, `src/voice_realtime/ui/server.py`, `contracts/meeting-assistant/v1/`, corresponding owned tests |
| Main — integration | package exports, compatibility fixes outside owned files, cross-workstream tests, README/architecture docs, full gates, final review and commits |

Workers may read all files but must write only their owned files. If an interface needs changing, message the main agent instead of editing another workstream's file.

## Shared Interfaces

Every worker implements against these names; changes require main-agent approval.

```python
# meeting/models.py
class RuntimeMode(StrEnum):
    ASSISTANT = "assistant"
    MEETING = "meeting"
    IDLE = "idle"

class MeetingStatus(StrEnum):
    RECORDING = "recording"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    STORAGE_ERROR = "storage_error"

class MinutesStatus(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"

class StorageHealth(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"

class NormalizedSegment(BaseModel):
    id: UUID
    order: int
    source_epoch: int
    speaker_key: str
    start_ms: int
    end_ms: int
    text: str
    translation: str | None = None
    detected_language: str | None = None

class TranscriptWindow(BaseModel):
    source_epoch: int
    partial: str = ""
    segments: tuple[NormalizedSegment, ...] = ()

class TranscriptReconcileResult(BaseModel):
    meeting_id: UUID
    transcript_revision: int
    content_revision: int
    replace_from_ms: int
    segments: tuple[NormalizedSegment, ...]
```

```python
# meeting/repository.py
class MeetingRepository(Protocol):
    async def check_writable(self) -> bool: ...
    async def create_meeting(self, title: str, *, language: str, audio_source: str) -> MeetingRecord: ...
    async def get_meeting(self, meeting_id: UUID) -> MeetingRecord | None: ...
    async def list_meetings(self, *, cursor: str | None, limit: int) -> MeetingPage: ...
    async def set_status(self, meeting_id: UUID, status: MeetingStatus, *, reason: str | None = None) -> MeetingRecord: ...
    async def reconcile_window(self, meeting_id: UUID, window: TranscriptWindow) -> TranscriptReconcileResult: ...
    async def finalize_transcript(self, meeting_id: UUID) -> MeetingRecord: ...
    async def get_transcript(self, meeting_id: UUID) -> TranscriptDocument: ...
    async def rename_speaker(self, meeting_id: UUID, speaker_key: str, display_name: str) -> MeetingRecord: ...
    async def create_minutes(self, meeting_id: UUID, *, idempotency_key: str | None) -> MinutesRecord: ...
    async def claim_minutes(self) -> MinutesJob | None: ...
    async def complete_minutes(self, minutes_id: UUID, result: MinutesResult) -> None: ...
    async def fail_minutes(self, minutes_id: UUID, *, code: str, message: str, raw_output: str | None = None) -> None: ...
    async def delete_meeting(self, meeting_id: UUID) -> None: ...
    async def close(self) -> None: ...
```

```python
# ui/subtitle_proxy.py compatibility plus meeting API
class SubtitleProxy:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def begin_capture(self, owner: str) -> None: ...
    async def finish_capture(self, *, timeout_secs: float) -> TranscriptWindow: ...
    async def abort_capture(self) -> None: ...
    def add_event_listener(self, listener: Callable[[TranscriptWindow], Awaitable[None]]) -> None: ...
    def remove_event_listener(self, listener: Callable[[TranscriptWindow], Awaitable[None]]) -> None: ...
```

```python
# meeting/session.py
class MeetingSession:
    async def start(self, title: str) -> MeetingRecord: ...
    async def stop(self) -> MeetingRecord: ...
    async def interrupt(self, reason: str) -> MeetingRecord | None: ...
    async def recover_stale(self) -> int: ...
    @property
    def active_meeting_id(self) -> UUID | None: ...
```

```python
# meeting/summary.py
class MeetingSummaryService:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def run_once(self) -> bool: ...
    async def requeue_for_recording(self) -> None: ...
```

---

### Task 1: Domain Models, Configuration, and Dependencies

**Files:**
- Create: `src/voice_realtime/meeting/__init__.py`
- Create: `src/voice_realtime/meeting/models.py`
- Modify: `src/voice_realtime/config.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/test_meeting_models.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: Pydantic v2 and the shared model signatures above.
- Produces: all enums and immutable transport/domain models used by Workstreams B and C; `Settings.meeting: MeetingSettings`.

- [ ] **Step 1: Add failing model/config tests**

```python
def test_meeting_settings_are_local_and_bounded(tmp_path: Path) -> None:
    settings = MeetingSettings(
        database_url="postgresql:///knowledge",
        recovery_dir=tmp_path / "recovery",
    )
    assert settings.schema == "voice_realtime"
    assert settings.summary_model == "qwen/qwen3.8-27b"
    assert settings.summary_reasoning == "off"
    assert settings.finalization_timeout_secs == 30
    assert settings.summary_concurrency == 1

def test_subtitle_diarization_defaults_are_offline_and_bounded(tmp_path: Path) -> None:
    settings = SubtitleSettings(diarization_model_path=tmp_path / "speaker.nemo")
    assert settings.diarization
    assert settings.diarization_backend == "sortformer"
    assert settings.diarization_max_speakers == 4

def test_normalized_segment_rejects_invalid_time() -> None:
    with pytest.raises(ValidationError):
        NormalizedSegment(
            id=uuid4(), order=0, source_epoch=1, speaker_key="e1:s1",
            start_ms=200, end_ms=100, text="错误",
        )
```

- [ ] **Step 2: Run tests and confirm missing models/settings fail**

Run: `uv run pytest tests/test_meeting_models.py tests/test_config.py -q`

Expected: collection/import failures for `voice_realtime.meeting.models` and `MeetingSettings`.

- [ ] **Step 3: Implement focused Pydantic models and settings**

Implement the shared enums/models plus `MeetingRecord`, `SpeakerRecord`, `TranscriptDocument`, `MeetingPage`, `MinutesRecord`, `MinutesJob`, `MinutesResult`, API payload models, and validators. Add `MeetingSettings` and the `SubtitleSettings` fields `diarization`, `diarization_backend`, `diarization_model_path`, and `diarization_max_speakers`:

```python
@model_validator(mode="after")
def _validate_time(self) -> Self:
    if self.end_ms < self.start_ms:
        raise ValueError("end_ms 必须大于等于 start_ms")
    if not self.text.strip():
        raise ValueError("text 不能为空")
    return self
```

Add `psycopg[binary]>=3.2,<4` and `psycopg-pool>=3.2,<4`, then run `uv lock` rather than editing lock content manually.

- [ ] **Step 4: Run targeted quality checks**

Run:

```bash
uv run pytest tests/test_meeting_models.py tests/test_config.py -q
uv run mypy src/voice_realtime/meeting/models.py src/voice_realtime/config.py
uv run ruff check src/voice_realtime/meeting/models.py src/voice_realtime/config.py tests/test_meeting_models.py tests/test_config.py
```

Expected: all pass.

- [ ] **Step 5: Commit Workstream A model foundation**

```bash
git add pyproject.toml uv.lock src/voice_realtime/config.py src/voice_realtime/meeting tests/test_meeting_models.py tests/test_config.py
git commit -m "feat(meeting): 建立会议领域模型与配置"
```

### Task 2: PostgreSQL Migration, Repository, and Recovery Journal

**Files:**
- Create: `src/voice_realtime/meeting/migrations/0001_initial.sql`
- Create: `src/voice_realtime/meeting/migrations.py`
- Create: `src/voice_realtime/meeting/repository.py`
- Create: `src/voice_realtime/meeting/recovery.py`
- Test: `tests/test_meeting_repository.py`
- Test: `tests/test_meeting_recovery.py`

**Interfaces:**
- Consumes: Task 1 models and `MeetingSettings`.
- Produces: the complete `MeetingRepository` protocol and `PostgresMeetingRepository`; `RecoveryJournal.append()`, `replay()`, and `discard()`.

- [ ] **Step 1: Write failing migration/repository tests**

Use a unique schema name per test session. Tests must skip only when `VR_TEST_DATABASE_URL` is absent; on this machine final verification sets it explicitly.

```python
async def test_reconcile_replaces_only_overlapping_window(repository: PostgresMeetingRepository) -> None:
    meeting = await repository.create_meeting("周会", language="Chinese", audio_source="microphone")
    await repository.reconcile_window(meeting.id, window_at(0, "第一段", 0, 1000))
    await repository.reconcile_window(meeting.id, window_at(1, "修订段", 900, 2000))
    doc = await repository.get_transcript(meeting.id)
    assert [s.text for s in doc.segments] == ["修订段"]
    assert doc.transcript_revision == 2

async def test_speaker_rename_marks_minutes_source_stale(repository: PostgresMeetingRepository) -> None:
    meeting = await seeded_meeting(repository)
    minutes = await repository.create_minutes(meeting.id, idempotency_key="same")
    changed = await repository.rename_speaker(meeting.id, "e1:s1", "张三")
    assert changed.content_revision > minutes.source_content_revision
```

- [ ] **Step 2: Verify tests fail before migration/repository exists**

Run: `VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/test_meeting_repository.py tests/test_meeting_recovery.py -q`

Expected: imports or missing migration fail; no existing `knowledge` tables are read or changed.

- [ ] **Step 3: Implement additive SQL migration**

Create schema-qualified tables from Spec §7 with checks, foreign keys, indexes, `schema_migrations`, and no vector/AGE objects. The runner must:

```python
async with connection.transaction():
    await connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
    # read applied versions; apply missing SQL resources; record checksum
```

Reject an already-applied version whose checksum differs.

- [ ] **Step 4: Implement repository transactions and claim semantics**

`reconcile_window()` locks the meeting row, deletes `end_ms >= replace_from_ms`, inserts the current window, upserts speakers, increments both revisions, and returns the committed replacement. `claim_minutes()` uses `FOR UPDATE SKIP LOCKED` and a lease timestamp. All public not-found/conflict conditions raise typed domain errors, never raw psycopg exceptions.

- [ ] **Step 5: Implement the transient recovery journal**

Write newline-delimited Pydantic envelopes with meeting ID, monotonic journal sequence, operation, and payload. Create files with mode `0o600`, flush and `os.fsync()` after append, replay idempotently through Repository, then unlink only after the PG transaction commits.

- [ ] **Step 6: Run real PG and static checks**

Run:

```bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/test_meeting_repository.py tests/test_meeting_recovery.py -q
uv run mypy src/voice_realtime/meeting/repository.py src/voice_realtime/meeting/recovery.py src/voice_realtime/meeting/migrations.py
uv run ruff check src/voice_realtime/meeting/repository.py src/voice_realtime/meeting/recovery.py src/voice_realtime/meeting/migrations.py tests/test_meeting_repository.py tests/test_meeting_recovery.py
```

Expected: all pass; temporary schemas are removed.

- [ ] **Step 7: Commit Workstream A persistence**

```bash
git add src/voice_realtime/meeting tests/test_meeting_repository.py tests/test_meeting_recovery.py
git commit -m "feat(meeting): 持久化会议转录与恢复日志"
```

### Task 3: Transcript Accumulator and Meeting-Capable WLK Gateway

**Files:**
- Create: `src/voice_realtime/meeting/transcript.py`
- Modify: `src/voice_realtime/ui/subtitle_proxy.py`
- Modify: `src/voice_realtime/subtitles/launcher.py`
- Test: `tests/test_meeting_transcript.py`
- Modify: `tests/test_subtitle_proxy.py`
- Modify: `tests/test_subtitles.py`

**Interfaces:**
- Consumes: Task 1 `TranscriptWindow`, `NormalizedSegment`; `SubtitleStream` raw events.
- Produces: the shared `SubtitleProxy` meeting API and `TranscriptNormalizer.normalize(payload, source_epoch, offset_ms)`.

- [ ] **Step 1: Write failing normalization and EOF tests**

```python
def test_normalizer_uses_epoch_and_sample_offset() -> None:
    window = TranscriptNormalizer().normalize(snapshot("你好", start="0:00:01.00"), 2, 30_000)
    assert window.segments[0].start_ms == 31_000
    assert window.segments[0].speaker_key != "1"

async def test_finish_capture_sends_empty_pcm_and_waits_ready(settings: SubtitleSettings) -> None:
    stream = FlushableFakeStream(final_snapshot("尾句"))
    proxy = SubtitleProxy(settings, stream_factory=lambda **_: stream)
    await proxy.start()
    await proxy.begin_capture("meeting:test")
    final = await proxy.finish_capture(timeout_secs=1)
    assert stream.sent[-1] == b""
    assert final.segments[-1].text == "尾句"
```

- [ ] **Step 2: Run tests and verify current browser-coupled behavior fails**

Run: `uv run pytest tests/test_meeting_transcript.py tests/test_subtitle_proxy.py tests/test_subtitles.py -q`

Expected: missing meeting APIs and missing diarization argv assertions fail.

- [ ] **Step 3: Implement normalization and capture leases**

Preserve existing `add_client/remove_client/push_audio` behavior for ordinary subtitles. A meeting capture lease makes audio admission independent of browser clients. `begin_capture()` closes any temporary subtitle stream, clears signatures/queues, opens a new WLK session, waits for config, and increments epoch. Only one owner may hold the lease.

- [ ] **Step 4: Implement EOF, final window, and reconnect gaps**

`finish_capture()` stops admission, sends `b""`, keeps receiving until `ready_to_stop`, returns the last normalized window, and always closes the epoch. Timeout raises typed `FinalizationTimeout` carrying the last window. Reconnect increments epoch, applies cumulative sample offset, and emits a typed gap event instead of hiding loss.

- [ ] **Step 5: Enable bounded offline diarization configuration**

Add launcher argv for `--diarization`, `--diarization-backend sortformer`, local model path, `--sortformer-max-speakers 4`, and explicit `--retention-seconds 0` for meeting full sessions. Missing local diarization model raises before process launch when downloads are disabled.

- [ ] **Step 6: Run targeted checks**

Run:

```bash
uv run pytest tests/test_meeting_transcript.py tests/test_subtitle_proxy.py tests/test_subtitles.py -q
uv run mypy src/voice_realtime/meeting/transcript.py src/voice_realtime/ui/subtitle_proxy.py src/voice_realtime/subtitles/launcher.py
uv run ruff check src/voice_realtime/meeting/transcript.py src/voice_realtime/ui/subtitle_proxy.py src/voice_realtime/subtitles/launcher.py tests/test_meeting_transcript.py tests/test_subtitle_proxy.py tests/test_subtitles.py
```

- [ ] **Step 7: Commit Workstream B gateway**

```bash
git add src/voice_realtime/meeting/transcript.py src/voice_realtime/ui/subtitle_proxy.py src/voice_realtime/subtitles/launcher.py tests/test_meeting_transcript.py tests/test_subtitle_proxy.py tests/test_subtitles.py
git commit -m "feat(meeting): 建立独立会议转录会话"
```

### Task 4: Meeting Session, Runtime Mode, and Control Protocol

**Files:**
- Create: `src/voice_realtime/meeting/session.py`
- Create: `src/voice_realtime/meeting/runtime_mode.py`
- Modify: `src/voice_realtime/ui/runtime.py`
- Modify: `src/voice_realtime/ui/protocol.py`
- Modify: `src/voice_realtime/ui/control.py`
- Test: `tests/test_meeting_session.py`
- Test: `tests/test_runtime_mode.py`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_control.py`

**Interfaces:**
- Consumes: shared Repository, SubtitleProxy meeting API, `InteractionSession`.
- Produces: `MeetingSession`, `RuntimeModeCoordinator`, v1 control commands, extended `RuntimeStateSnapshot`.

- [ ] **Step 1: Write failing mode isolation tests**

```python
async def test_start_meeting_stops_interaction_before_capture(coordinator: RuntimeModeCoordinator) -> None:
    calls: list[str] = []
    coordinator.interaction.stop.side_effect = lambda **_: calls.append("interaction.stop")
    coordinator.gateway.begin_capture.side_effect = lambda *_: calls.append("gateway.begin_capture")
    await coordinator.start_meeting("周会")
    assert coordinator.mode is RuntimeMode.MEETING
    assert calls.index("interaction.stop") < calls.index("gateway.begin_capture")

async def test_end_meeting_returns_idle_and_does_not_restart_assistant(coordinator: RuntimeModeCoordinator) -> None:
    await coordinator.start_meeting("周会")
    await coordinator.end_meeting(coordinator.active_meeting_id)
    assert coordinator.mode is RuntimeMode.IDLE
    coordinator.interaction.start.assert_not_awaited()
```

- [ ] **Step 2: Verify tests fail on the current always-on interaction runtime**

Run: `uv run pytest tests/test_meeting_session.py tests/test_runtime_mode.py tests/test_runtime.py tests/test_control.py -q`

Expected: missing modes and control commands fail.

- [ ] **Step 3: Implement MeetingSession lifecycle**

`start()` checks repository writeability, creates the meeting, begins WLK capture, registers its window listener, then returns `recording`. `stop()` sets finalizing, calls gateway EOF, reconciles the final window, finalizes transcript, creates queued minutes, and unregisters listeners in `finally`. PG write failures append to RecoveryJournal and emit storage health; unrecoverable journal failure interrupts capture.

- [ ] **Step 4: Implement atomic RuntimeModeCoordinator**

One `asyncio.Lock` protects transitions. `start_meeting()` must stop interaction and drain its queue before MeetingSession starts. On preflight failure, restore assistant only if it was running and the meeting never acquired capture. `end_meeting()` always ends in idle. `start_assistant()` refuses while meeting/finalizing is active.

- [ ] **Step 5: Extend strict control contracts without breaking old commands**

Add discriminated Pydantic commands `start_meeting`, `end_meeting`, `start_assistant`, `stop_active_mode`; retain all old command names. Add `mode`, `active_meeting_id`, `meeting_state`, `meeting_started_at`, `storage`, and `runtime_revision` to snapshots. Cache command results by request ID for bounded idempotency.

- [ ] **Step 6: Run targeted checks**

Run:

```bash
uv run pytest tests/test_meeting_session.py tests/test_runtime_mode.py tests/test_runtime.py tests/test_control.py -q
uv run mypy src/voice_realtime/meeting/session.py src/voice_realtime/meeting/runtime_mode.py src/voice_realtime/ui/runtime.py src/voice_realtime/ui/protocol.py src/voice_realtime/ui/control.py
uv run ruff check src/voice_realtime/meeting/session.py src/voice_realtime/meeting/runtime_mode.py src/voice_realtime/ui/runtime.py src/voice_realtime/ui/protocol.py src/voice_realtime/ui/control.py tests/test_meeting_session.py tests/test_runtime_mode.py tests/test_runtime.py tests/test_control.py
```

- [ ] **Step 7: Commit Workstream B runtime**

```bash
git add src/voice_realtime/meeting/session.py src/voice_realtime/meeting/runtime_mode.py src/voice_realtime/ui tests/test_meeting_session.py tests/test_runtime_mode.py tests/test_runtime.py tests/test_control.py
git commit -m "feat(meeting): 隔离会议与语音助手运行模式"
```

### Task 5: Evidence-Backed AI Minutes Service

**Files:**
- Create: `src/voice_realtime/meeting/summary.py`
- Test: `tests/test_meeting_summary.py`

**Interfaces:**
- Consumes: Repository minutes job API and Task 1 result models.
- Produces: shared `MeetingSummaryService` plus `MeetingSummaryClient.generate(document, speakers)`.

- [ ] **Step 1: Write failing summary validation tests**

```python
async def test_summary_rejects_unknown_evidence(repository: FakeRepository) -> None:
    client = FakeSummaryClient(result_with_evidence([uuid4()]))
    service = MeetingSummaryService(repository, client, settings())
    assert await service.run_once()
    assert repository.failed_code == "invalid_evidence"

async def test_new_recording_requeues_active_summary(service: MeetingSummaryService) -> None:
    task = asyncio.create_task(service.run_once())
    await service.requeue_for_recording()
    await task
    assert service.repository.requeued
```

- [ ] **Step 2: Verify tests fail before the service exists**

Run: `uv run pytest tests/test_meeting_summary.py -q`

- [ ] **Step 3: Implement the native LM Studio client**

Use `local_async_client()` with root URL normalization and `/api/v1/chat`. Payload contains `model`, role-free text `input`, `reasoning`, `temperature`, and `stream: true`; it must not contain `role` or `max_tokens`. Consume only `message.delta`, reject empty content and explicit error events, and close the client idempotently.

- [ ] **Step 4: Implement schema validation, evidence checks, and Markdown rendering**

Define exact Pydantic result types for overview/topics/decisions/action items/risks/questions/highlights. Validate every evidence UUID against the finalized transcript. Permit one format-repair request; never repair unsupported factual claims by inventing evidence. Render Markdown server-side from validated data.

- [ ] **Step 5: Implement long-meeting map/reduce and worker recovery**

Use a conservative token estimator, split only at segment boundaries, retain overlap IDs, map chunks into the same evidence schema, then reduce and deduplicate by normalized content plus evidence set. `run_once()` claims one row, persists completed/failed state, and requeues safely when recording priority interrupts it.

- [ ] **Step 6: Run targeted checks**

Run:

```bash
uv run pytest tests/test_meeting_summary.py -q
uv run mypy src/voice_realtime/meeting/summary.py
uv run ruff check src/voice_realtime/meeting/summary.py tests/test_meeting_summary.py
```

- [ ] **Step 7: Commit Workstream C summary service**

```bash
git add src/voice_realtime/meeting/summary.py tests/test_meeting_summary.py
git commit -m "feat(meeting): 生成可追溯的结构化会议纪要"
```

### Task 6: V1 HTTP, WebSocket Events, Contracts, and Fixtures

**Files:**
- Create: `src/voice_realtime/meeting/api.py`
- Create: `src/voice_realtime/meeting/events.py`
- Modify: `src/voice_realtime/ui/server.py`
- Create: `contracts/meeting-assistant/v1/openapi.json`
- Create: `contracts/meeting-assistant/v1/asyncapi.yaml`
- Create: `contracts/meeting-assistant/v1/schemas/`
- Create: `contracts/meeting-assistant/v1/fixtures/`
- Test: `tests/test_meeting_api.py`
- Test: `tests/test_meeting_events.py`
- Modify: `tests/test_ui_server.py`

**Interfaces:**
- Consumes: Repository, RuntimeModeCoordinator, SummaryService, public Task 1 models.
- Produces: canonical `/api/v1`, `/ws/v1/control`, `/ws/v1/meetings`, checked-in contract artifacts and fixtures.

- [ ] **Step 1: Write failing API/error/event tests**

```python
async def test_transcript_response_has_revision_and_public_segments(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/meetings/{MEETING_ID}/transcript")
    body = response.json()
    assert body["transcript_revision"] == 3
    assert set(body["segments"][0]) >= {"id", "speaker_key", "speaker_name", "start_ms", "end_ms"}

async def test_slow_meeting_client_receives_resync_required(broadcaster: MeetingEventBroadcaster) -> None:
    client = broadcaster.add_test_client(queue_size=1)
    await broadcaster.publish(durable_event(1))
    await broadcaster.publish(durable_event(2))
    assert (await client.receive())["type"] == "resync_required"
```

- [ ] **Step 2: Verify missing v1 routes and schemas fail**

Run: `uv run pytest tests/test_meeting_api.py tests/test_meeting_events.py tests/test_ui_server.py -q`

- [ ] **Step 3: Implement routers and stable errors**

Build an `APIRouter(prefix="/api/v1")` for Spec §13. Convert typed domain errors to the exact error envelope; never expose exceptions. Validate cursor, limit, title, speaker name, format, UUID, and `Idempotency-Key`. Refuse deletion of recording/finalizing meetings.

- [ ] **Step 4: Implement bounded meeting event broadcaster and v1 sockets**

Every connection receives a snapshot. partial may be dropped; if a durable revision cannot be delivered, clear the per-client queue and enqueue `resync_required`. Add `/ws/v1/control` as canonical while preserving `/ws/assistant/cmd` compatibility. Apply existing loopback Origin policy plus configured allowed origins.

- [ ] **Step 5: Generate and check contract artifacts**

Generate OpenAPI from FastAPI, JSON Schemas from Pydantic public models, author AsyncAPI channels matching the JSON Schemas, and add fixtures for idle, recording, finalizing, completed, interrupted, transcript reconciliation, gap, resync, minutes completed, and minutes failed. A test loads every fixture and validates it against its schema.

- [ ] **Step 6: Run targeted checks**

Run:

```bash
uv run pytest tests/test_meeting_api.py tests/test_meeting_events.py tests/test_ui_server.py -q
uv run mypy src/voice_realtime/meeting/api.py src/voice_realtime/meeting/events.py src/voice_realtime/ui/server.py
uv run ruff check src/voice_realtime/meeting/api.py src/voice_realtime/meeting/events.py src/voice_realtime/ui/server.py tests/test_meeting_api.py tests/test_meeting_events.py tests/test_ui_server.py
git diff --exit-code -- contracts/meeting-assistant/v1
```

- [ ] **Step 7: Commit Workstream C contracts/API**

```bash
git add src/voice_realtime/meeting/api.py src/voice_realtime/meeting/events.py src/voice_realtime/ui/server.py contracts/meeting-assistant/v1 tests/test_meeting_api.py tests/test_meeting_events.py tests/test_ui_server.py
git commit -m "feat(meeting): 发布会议助手 v1 后端契约"
```

### Task 7: Cross-Workstream Integration and Compatibility

**Files:**
- Modify: `src/voice_realtime/meeting/__init__.py`
- Modify only as required: package files outside exclusive ownership after workers finish
- Create: `tests/test_meeting_integration.py`
- Modify: backend documentation and architecture diagrams

**Interfaces:**
- Consumes: all three workstreams.
- Produces: one coherent backend runtime with clean startup/shutdown and no frontend business changes.

- [ ] **Step 1: Add an end-to-end fake-services test**

```python
async def test_meeting_end_to_end_without_llm_or_tts_during_recording(app_harness: MeetingHarness) -> None:
    meeting = await app_harness.start_meeting("架构评审")
    await app_harness.feed_confirmed("确认采用 PostgreSQL", speaker=1)
    assert app_harness.interaction_stopped
    assert app_harness.tts_requests == []
    await app_harness.end_meeting(meeting.id)
    assert (await app_harness.transcript(meeting.id)).segments
    assert await app_harness.wait_for_minutes(meeting.id)
```

- [ ] **Step 2: Wire dependencies and lifespan order**

Startup order: migrations/recovery → repository → gateway → AudioHub → interaction default → summary worker → routes ready. Shutdown order: active meeting EOF/interruption → summary worker → interaction → AudioHub/gateway → repository pool. A meeting subsystem failure must not prevent ordinary assistant/static UI startup unless the user explicitly starts meeting mode.

- [ ] **Step 3: Resolve shared-interface drift and regenerate contracts**

Compare concrete signatures against the Shared Interfaces section. Fix adapters rather than allowing duplicate types. Regenerate contracts from final code and validate every fixture. Do not edit frontend meeting behavior.

- [ ] **Step 4: Update project documentation**

Update README, `docs/架构图与流程图.md`, and the main best-practices document with V1 backend topology, PG bootstrap/migration commands, WLK diarization prerequisites, API/WS paths, recovery behavior, and the explicit frontend-team boundary.

- [ ] **Step 5: Commit integration**

```bash
git add src tests contracts README.md docs
git commit -m "feat(meeting): 集成会议助手后端运行链路"
```

### Task 8: Full Verification and Delivery

**Files:**
- Create: `docs/superpowers/specs/2026-08-21-meeting-assistant-backend-verification.md`
- No source changes except root-cause fixes found by verification.

- [ ] **Step 1: Run Python gates**

```bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/
uv run mypy src/
uv run ruff check src/ tests/
```

Expected: all green; default branch coverage remains at least 80%.

- [ ] **Step 2: Prove existing frontend compatibility**

```bash
cd ui
npm test -- --run
npm run build
```

Expected: current frontend tests and production build pass without implementing meeting UI.

- [ ] **Step 3: Run bounded real-service acceptance**

Verify current PG readiness and temporary schema cleanup. Start WLK with local Qwen3 streaming and local Sortformer diarization, confirm config, feed bounded PCM, send EOF, receive final snapshot and `ready_to_stop`. Call LM Studio native chat with the configured summary model and validate a small evidence-backed result. Never print credentials or full private transcripts.

- [ ] **Step 4: Run failure matrix**

Exercise PG unavailable/journal replay, WLK disconnect/new epoch/gap, browser slow client/resync, LM unavailable/minutes retry, and shutdown during finalizing. Record exact observed evidence and limitations.

- [ ] **Step 5: Review diff for scope, security, and contract compatibility**

Confirm no meeting UI implementation, no raw audio persistence, no transcript/prompt logs, no superuser runtime assumption, no unbounded queue/task, no OpenAI-compatible reasoning workaround, and no contract drift.

- [ ] **Step 6: Write verification report and commit**

```bash
git add docs/superpowers/specs/2026-08-21-meeting-assistant-backend-verification.md
git commit -m "docs: 记录会议助手后端验收结果"
```
