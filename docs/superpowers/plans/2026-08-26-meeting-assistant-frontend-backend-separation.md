---
title: "会议助手前后端分离实施计划"
description: "基于 contracts/ 的公共契约冻结、Mock 服务与独立联调门禁执行任务清单"
status: implemented
type: execution_plan
category: meeting
version: "v1.0.0"
date: 2026-08-26
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-core"
tags:
  - execution-plan
  - meeting-assistant
  - frontend-backend-separation
---

# 会议助手前后端分离式开发实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不立即拆仓的前提下，建立会议助手前后端团队可以并行开发、独立测试和可控发布的版本化公共契约与实现边界。

**Architecture:** 以 `contracts/meeting-assistant/v1/` 为唯一公共接口源，使用 OpenAPI 3.1、AsyncAPI 3.0 和 JSON Schema 2020-12 描述 HTTP/WebSocket/事件 payload。后端作为会议事实和 revision 的权威生产者，前端只消费契约并派生 UI 状态；阅读视图不覆盖 canonical transcript segments。

**Tech Stack:** Python 3.12、Pydantic/JSON Schema、PostgreSQL、React 19、TypeScript、Vite 7、OpenAPI 3.1、AsyncAPI 3.0、pytest、Vitest。

**Spec:** `docs/会议助手前后端分离式开发准备方案.md`

**本次执行范围：** 用户已明确不负责前端实现，因此本次只执行后端及公共工程工作，顺序为
`C0 → B1 → D1 → Q1`。F1（`ui/src/` consumer/UI）保留给前端团队，不能将其未完成误报为本次任务失败。

**本次执行状态（2026-08-26）：** C0、B1、D1、Q1 的后端/公共工程部分已完成并通过对应门禁；F1 及前后端联合 review 保留给前端团队和后续 Q1 汇合阶段。

## Global Constraints

- `contracts/meeting-assistant/v1/` 是公共接口唯一事实源，代码和测试必须与其一致。
- `contract_version` 当前为字符串 `"1"`；同一 major 内只允许向后兼容的 additive 变更。
- 前端不得读取 PostgreSQL、导入后端 Python 实现或解析 `speaker_key` 内部格式。
- PostgreSQL 是会议数据唯一事实源；产品不保存会议音频。
- `meeting_snapshot` 是重连基线；任何无法解释的 revision gap 或 `resync_required` 都必须通过 HTTP 重建基线。
- 阅读层不得跨 `speaker_key` 或 `source_epoch` 合并，也不得覆盖事实层 `TranscriptSegment`。
- 所有 REST 错误使用统一 error envelope，前端只依赖稳定 `error.code`。
- 每个任务完成后运行该任务列出的最小测试，再运行跨团队契约测试。

---

### Task 1: 建立 v1 公共契约基线

**Files:**
- Create: `contracts/meeting-assistant/v1/README.md`
- Create: `contracts/meeting-assistant/CHANGELOG.md`
- Modify: `contracts/meeting-assistant/v1/openapi.json`
- Modify: `contracts/meeting-assistant/v1/asyncapi.yaml`
- Modify: `contracts/meeting-assistant/v1/schemas/event-envelope.schema.json`
- Create: `contracts/meeting-assistant/v1/schemas/event-meeting-snapshot.schema.json`
- Create: `contracts/meeting-assistant/v1/schemas/event-meeting-state-changed.schema.json`
- Create: `contracts/meeting-assistant/v1/schemas/event-transcript-partial.schema.json`
- Create: `contracts/meeting-assistant/v1/schemas/event-transcript-reconciled.schema.json`
- Create: `contracts/meeting-assistant/v1/schemas/event-speaker-updated.schema.json`
- Create: `contracts/meeting-assistant/v1/schemas/event-minutes-state-changed.schema.json`
- Create: `contracts/meeting-assistant/v1/schemas/event-health-changed.schema.json`
- Create: `contracts/meeting-assistant/v1/schemas/event-transcription-gap.schema.json`
- Create: `contracts/meeting-assistant/v1/schemas/event-resync-required.schema.json`
- Modify: `contracts/meeting-assistant/v1/fixtures/transcript-partial.json`
- Modify: `contracts/meeting-assistant/v1/fixtures/transcript-reconciled.json`
- Create: `contracts/meeting-assistant/v1/fixtures/meeting-snapshot-active.json`
- Create: `contracts/meeting-assistant/v1/fixtures/revision-gap.json`
- Test: `tests/test_meeting_api.py`

**Interfaces:**
- Consumes: current `openapi.json`, `asyncapi.yaml`, JSON Schemas and fixtures.
- Produces: machine-validatable v1 REST/WS contract; per-event payload schemas; `start_subtitles` command definition; canonical `/api/v1/runtime` path; documented revision and snapshot rules.

- [ ] **Step 1: Add failing contract assertions**

  Extend `tests/test_meeting_api.py` with assertions that:

  ```python
  assert openapi["paths"]["/api/v1/runtime"]["get"]
  asyncapi_text = (root / "asyncapi.yaml").read_text(encoding="utf-8")
  assert "start_subtitles" in asyncapi_text
  event_schema = json.loads(
      (root / "schemas/event-transcript-reconciled.schema.json").read_text(encoding="utf-8")
  )
  assert event_schema["required"]
  fixture = json.loads((root / "fixtures/transcript-partial.json").read_text(encoding="utf-8"))
  assert fixture["contract_version"] == "1"
  ```

  Add fixtures for an active snapshot, a `transcript_reconciled` payload, a partial with known speaker, and a `resync_required` event.

- [ ] **Step 2: Run the focused contract test**

  Run: `uv run pytest tests/test_meeting_api.py -q`

  Expected: the test identifies any missing path, command, event schema, or fixture before implementation changes are made.

- [ ] **Step 3: Make the contract artifacts consistent**

  Define each event payload as a schema selected by the envelope `type`. Keep the current public fields backward compatible, document the runtime path decision, add `start_subtitles`, and describe empty versus active `meeting_snapshot` semantics in `v1/README.md`.

- [ ] **Step 4: Validate fixtures and schemas**

  Run: `uv run pytest tests/test_meeting_api.py -q`

  Expected: all contract artifact and fixture tests pass; invalid event payloads are rejected by the test validator.

- [ ] **Step 5: Commit the contract baseline**

  ```bash
  git add contracts/meeting-assistant/v1 contracts/meeting-assistant/CHANGELOG.md tests/test_meeting_api.py
  git commit -m "docs(contract): 固化会议助手前后端 v1 契约基线"
  ```

### Task 2: 固化后端 producer 行为

**Files:**
- Modify: `src/voice_realtime/meeting/session.py`
- Modify: `src/voice_realtime/meeting/events.py`
- Modify: `src/voice_realtime/meeting/api.py`
- Modify: `src/voice_realtime/meeting/repository.py`
- Modify: `src/voice_realtime/ui/server.py`
- Modify: `src/voice_realtime/ui/control.py`
- Test: `tests/test_meeting_session.py`
- Test: `tests/test_meeting_events.py`
- Test: `tests/test_meeting_api.py`
- Test: `tests/test_ui_server.py`

**Interfaces:**
- Consumes: schemas and fixtures from Task 1.
- Produces: schema-valid HTTP responses and WS events; strict meeting ID/revision semantics; stable error codes; partial speaker propagation; deterministic resync behavior.

- [ ] **Step 1: Add producer regression tests**

  Cover these exact cases:

  - `test_snapshot_change_cannot_leave_previous_meeting_state`: dispatch an active snapshot for meeting A, then a snapshot for meeting B, and assert the active ID is B while segments, speakers, minutes, gaps, and partial are reset before B baseline data is applied.
  - `test_transcript_revision_gap_emits_resync_required`: produce revisions 1 and 3 for the same meeting and assert the producer emits `resync_required` with the expected revision information instead of silently applying revision 3.
  - `test_partial_preserves_known_speaker_key`: serialize a partial window whose diarization identifies `e1:s1` and assert the event payload preserves that key and its public display name.
  - `test_replace_from_ms_replaces_end_boundary_consistently`: reconcile a segment ending exactly at `replace_from_ms` and assert the old row is replaced once, with no duplicate or missing segment.

  The tests must assert event type, meeting ID, revision, payload shape, and error code rather than human-readable message text.

- [ ] **Step 2: Run backend regression tests and record failures**

  Run: `uv run pytest tests/test_meeting_session.py tests/test_meeting_events.py tests/test_meeting_api.py tests/test_ui_server.py -q`

  Expected: failures identify implementation drift from the v1 contract; do not weaken the schemas to make an invalid producer pass.

- [ ] **Step 3: Implement the minimum producer changes**

  Apply the snapshot reset, revision continuity, partial speaker, `replace_from_ms`, error envelope, and per-event serialization rules from the spec. Keep PostgreSQL and recovery journal boundaries unchanged.

- [ ] **Step 4: Re-run focused backend tests**

  Run: `uv run pytest tests/test_meeting_session.py tests/test_meeting_events.py tests/test_meeting_api.py tests/test_ui_server.py -q`

  Expected: all producer tests pass, including duplicate, out-of-order, reconnect, and EOF paths.

- [ ] **Step 5: Commit the producer boundary**

  ```bash
  git add src/voice_realtime/meeting src/voice_realtime/ui tests/test_meeting_session.py tests/test_meeting_events.py tests/test_meeting_api.py tests/test_ui_server.py
  git commit -m "fix(meeting): 固化前后端分离的事件生产语义"
  ```

### Task 3: 建立前端 consumer adapter 和恢复状态机

**Files:**
- Modify: `ui/src/contracts/meetingContract.ts`
- Modify: `ui/src/services/meetingApi.ts`
- Modify: `ui/src/hooks/useMeetingSocket.ts`
- Modify: `ui/src/stores/meetingStore.ts`
- Test: `ui/src/hooks/useMeetingSocket.test.ts`
- Test: `ui/src/services/meetingApi.test.ts`
- Test: `ui/src/stores/meetingStore.test.ts`

**Interfaces:**
- Consumes: Task 1 public schemas and Task 2 producer events.
- Produces: typed HTTP/WS adapter; snapshot reset; stale-event rejection; strict revision gap resync; one partial state; UI-independent canonical transcript state.

- [ ] **Step 1: Add consumer tests from shared fixtures**

  Add tests for:

  ```text
  active snapshot replaces a different active meeting
  old meeting event is ignored
  revision N+2 triggers transcript baseline fetch
  resync_required triggers transcript baseline fetch
  partial does not increase confirmed segment count
  replace_from_ms does not duplicate an exact boundary segment
  ```

- [ ] **Step 2: Run frontend focused tests**

  Run: `cd ui && npm test -- --run src/hooks/useMeetingSocket.test.ts src/services/meetingApi.test.ts src/stores/meetingStore.test.ts`

  Expected: tests fail only for the missing consumer behavior, not because the frontend has to import backend code.

- [ ] **Step 3: Implement the adapter boundary**

  Keep JSON parsing and runtime validation in the API/WS adapter. The store should receive typed domain events and should not know HTTP URL construction, database details, or model internals.

- [ ] **Step 4: Re-run frontend focused tests**

  Run: `cd ui && npm test -- --run src/hooks/useMeetingSocket.test.ts src/services/meetingApi.test.ts src/stores/meetingStore.test.ts`

  Expected: all fixture-based consumer tests pass, including resync and meeting switch behavior.

- [ ] **Step 5: Commit the consumer boundary**

  ```bash
  git add ui/src/contracts ui/src/services/meetingApi.ts ui/src/hooks/useMeetingSocket.ts ui/src/stores/meetingStore.ts ui/src/hooks/useMeetingSocket.test.ts ui/src/services/meetingApi.test.ts ui/src/stores/meetingStore.test.ts
  git commit -m "fix(ui): 建立会议助手契约消费与重同步边界"
  ```

### Task 4: 提供独立前端 fixture/mock 数据源

**Files:**
- Create: `ui/src/services/meetingDataSource.ts`
- Create: `ui/src/services/meetingMockDataSource.ts`
- Modify: `ui/src/services/meetingApi.ts`
- Modify: `ui/src/hooks/useMeetingSocket.ts`
- Create: `ui/src/test/fixtures/meetingEventSequences.ts`
- Test: `ui/src/services/meetingApi.test.ts`
- Test: `ui/src/hooks/useMeetingSocket.test.ts`

**Interfaces:**
- Consumes: shared JSON fixtures and the adapter types from Task 3.
- Produces: `fixture|mock|backend` data source selection; deterministic event sequence replay; injectable delay, duplicate, out-of-order, disconnect, gap, and error scenarios.

- [ ] **Step 1: Define data source interface tests**

  Require every data source to expose the same operations:

  ```ts
  type Unsubscribe = () => void;

  interface MeetingDataSource {
    getMeeting(id: string): Promise<MeetingDetail>;
    getTranscript(id: string): Promise<TranscriptResponse>;
    subscribeMeetingEvents(id: string, onEvent: (event: MeetingEventEnvelope) => void): Unsubscribe;
  }
  ```

- [ ] **Step 2: Add replay scenarios**

  Include sequences for active snapshot, partial, reconciled segment, speaker rename, `resync_required`, meeting switch, finalizing, completed, interrupted, HTTP 409, and HTTP 503.

- [ ] **Step 3: Implement fixture and mock sources**

  The fixture source returns only schema-valid static data. The mock source adds deterministic controls for delay, duplicate event, revision gap, and disconnect without changing event payloads.

- [ ] **Step 4: Run frontend tests and build**

  Run: `cd ui && npm test -- --run`

  Run: `cd ui && npm run build`

  Expected: all frontend tests pass and the production build succeeds with all three data source modes type-safe.

- [ ] **Step 5: Commit the independent data source**

  ```bash
  git add ui/src/services ui/src/hooks ui/src/test/fixtures ui/src/services/meetingApi.test.ts ui/src/hooks/useMeetingSocket.test.ts
  git commit -m "test(ui): 增加会议助手 fixture 与 mock 联调数据源"
  ```

### Task 5: 实施转录阅读视图和可访问性边界

**Files:**
- Create: `ui/src/components/meeting/transcriptViewModel.ts`
- Modify: `ui/src/components/meeting/MeetingRecordingView.tsx`
- Modify: `ui/src/components/meeting/MeetingTranscriptViewer.tsx`
- Modify: `ui/src/components/meeting/MeetingPanel.css`
- Modify: `ui/src/components/meeting/MeetingComponents.test.tsx`
- Test: `ui/src/components/meeting/transcriptViewModel.test.ts`

**Interfaces:**
- Consumes: canonical `TranscriptSegment[]` from Task 3 and the `TranscriptViewBlock` rules from the spec.
- Produces: time-order view, reading view, `segment_ids` traceability, stable speaker colors, partial/confirmed labels, keyboard and screen-reader-safe actions.

- [ ] **Step 1: Add view-model tests**

  Assert that same-speaker short-gap segments merge within the configured limit, different speakers never merge, different epochs never merge, hard limits split blocks, and every block can expand back to its source IDs.

- [ ] **Step 2: Run component and view-model tests**

  Run: `cd ui && npm test -- --run src/components/meeting/transcriptViewModel.test.ts src/components/meeting/MeetingComponents.test.tsx`

  Expected: failures describe only missing view-model/UI behavior.

- [ ] **Step 3: Implement the two views**

  Keep time-order cards faithful to confirmed segments. Use the initial reading candidates `gap <= 1200ms`, block duration `<= 15s`, and length `<= 180` Chinese characters as configurable presentation defaults; do not write merged blocks back to the server.

- [ ] **Step 4: Implement interaction semantics**

  Use native buttons, `aria-pressed` for the star toggle, visible focus, accessible names, a polite status region for partial/resync states, and a label such as “已确认发言片段” instead of “段落”。

- [ ] **Step 5: Run frontend quality checks**

  Run: `cd ui && npm test -- --run`

  Run: `cd ui && npm run build`

  Expected: all UI tests pass and production build succeeds without importing backend implementation modules.

- [ ] **Step 6: Commit the presentation boundary**

  ```bash
  git add ui/src/components/meeting ui/src/components/meeting/MeetingPanel.css
  git commit -m "feat(ui): 增加会议转录时序与阅读视图"
  ```

### Task 6: 建立跨团队契约门禁与发布记录

**Files:**
- Modify: `tests/test_meeting_api.py`
- Modify: `ui/src/hooks/useMeetingSocket.test.ts`
- Modify: `ui/src/services/meetingApi.test.ts`
- Create: `docs/联调记录模板.md`
- Modify: `contracts/meeting-assistant/CHANGELOG.md`
- Create: `scripts/validate-meeting-contract.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1 schemas/fixtures, Task 2 producer, Task 3/4 consumer and Task 5 UI.
- Produces: repeatable producer/consumer/integration gate; release record with contract version and both commit SHAs; documented rollback evidence.

- [ ] **Step 1: Add producer-consumer fixture parity tests**

  For each shared fixture, validate that the backend serializer accepts it and the frontend adapter can consume it. Include negative fixtures for missing envelope fields, unsupported contract version, invalid revision, and invalid payload type.

- [ ] **Step 2: Run the complete contract and frontend/backend focused gate**

  Run: `uv run pytest tests/test_meeting_api.py tests/test_meeting_events.py tests/test_meeting_session.py -q`

  Run: `cd ui && npm test -- --run`

  Expected: all producer and consumer checks pass against the same v1 artifact.

- [ ] **Step 3: Write the release and rollback record**

  Record contract version, backend SHA, frontend SHA, schema validation output, test output, runtime URLs, and the exact whole-stack rollback procedure in `docs/联调记录模板.md`.

- [ ] **Step 4: Run the repository quality gate**

  Run: `uv run python3 scripts/validate-meeting-contract.py`

  Run: `VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/`

  Run: `uv run mypy src/`

  Run: `uv run ruff check src/ tests/`

  Run: `cd ui && npm test -- --run`

  Run: `cd ui && npm run build`

  Expected: all required gates pass, or any pre-existing unrelated failure is recorded with its exact command output and scope.

- [ ] **Step 5: Commit the cross-team gate**

  ```bash
  git add tests docs/联调记录模板.md contracts/meeting-assistant/CHANGELOG.md
  git commit -m "test(contract): 增加前后端分离契约发布门禁"
  ```

## Parallel Execution Order

```text
Task 1: Contract baseline
        ├── Task 2: Backend producer
        └── Task 3: Frontend consumer
                └── Task 4: Fixture/mock data source
                        └── Task 5: Transcript UX
Task 2 + Task 4 + Task 5 → Task 6: Cross-team gate
```

Task 2 and Task 3 can proceed in parallel only after Task 1's schemas and fixtures are reviewed by both teams. Task 5 can start with mock data but cannot change the public contract without returning to Task 1.

## Self-Review Checklist

- [ ] Every public endpoint and channel has an owner and schema location.
- [ ] `meeting_snapshot`, `transcript_partial`, `transcript_reconciled`, `resync_required`, error envelope and revision rules have explicit tasks.
- [ ] Frontend and backend can each run meaningful tests without the other implementation.
- [ ] No task asks an implementer to infer an unspecified type, path, or error behavior.
- [ ] No task stores audio or turns internal model output into a frontend dependency.
- [ ] The plan preserves PostgreSQL, EOF, source epoch, anonymous speaker and runtime ownership constraints.
