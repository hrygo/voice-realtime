# Voice Realtime Architecture Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this workspace those skills are unavailable, so the root agent must reproduce their TDD, file-ownership, review, and verification gates with the collaboration subagents.

**Goal:** 修复架构审计发现的全部运行拓扑、字幕、交互算法、资源生命周期、控制、安全、配置、测试和文档缺陷。

**Architecture:** `vr-ui` 是带 UI 场景下交互管道的唯一所有者，`vr-interact` 是复用同一 `InteractionSession` 的互斥 headless 入口。AudioHub、InteractionSession、SubtitleProxy、TTS/LLM 客户端和浏览器控制通道分别拥有明确生命周期，并通过严格状态模型协作。

**Tech Stack:** Python 3.12、asyncio、Pipecat 1.7、FastAPI/Pydantic v2、PyAudio、WhisperLiveKit、httpx、React 19、TypeScript 5.8、Zustand、Vitest。

**Spec:** `docs/superpowers/specs/2026-08-20-voice-realtime-architecture-remediation-design.md`

## Global Constraints

- Python 必须保持 `>=3.12,<3.13`；不得引入仅支持 3.13 的 API。
- LM Studio 必须继续使用原生 `/api/v1/chat`、`reasoning:"off"`、无 `role`/`max_tokens` payload。
- SenseVoice repo ID 必须先经 HuggingFace 本地快照解析；默认离线模式不隐式联网。
- 保留单机同麦同箱的音频域与文本域两道回声防线，但修复错误状态和误杀。
- 所有行为变更遵循 RED→GREEN→REFACTOR；每个 bug 的测试需先在旧实现上失败。
- 不得回退当前工作树中已有未提交修改；并行 worker 只能修改任务声明的文件。
- 子任务完成报告不是验收证据；主 Agent 必须检查 diff 并重新运行相关测试。

---

### Task 1: Freeze shared configuration and protocol contracts

**Files:**
- Modify: `src/voice_realtime/config.py`
- Create: `src/voice_realtime/ui/protocol.py`
- Test: `tests/test_config.py`
- Test: `tests/test_control.py`

**Interfaces:**
- Produces: `DuplexMode`, `RuntimeStateSnapshot`, strict command models, `CommandResponse`.
- Produces: `SubtitleSettings.allow_model_downloads`, loopback-only host validation, fixed 16k/24k sample-rate validation.
- Removes: `InteractionSettings.tts_voice`, `interrupt_echo_suppression_ms`, `SubtitleSettings.device`.

- [ ] **Step 1: Write failing configuration tests**

```python
def test_interaction_rejects_non_16k_sample_rate() -> None:
    with pytest.raises(ValidationError):
        InteractionSettings(sample_rate=24000)

def test_bridge_rejects_non_native_sample_rate() -> None:
    with pytest.raises(ValidationError):
        BridgeSettings(sample_rate=16000)

def test_ui_rejects_non_loopback_host() -> None:
    with pytest.raises(ValidationError):
        UISettings(host="0.0.0.0")
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `uv run pytest tests/test_config.py -q`

- [ ] **Step 3: Add strict protocol models**

```python
class CommandBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=64)
    cmd: str

class RuntimeStateSnapshot(BaseModel):
    pipeline: str
    subtitle: str
    mic_muted: bool
    persona: str | None
    voice: str
    duplex_mode: Literal["speaker_focus", "headphone_duplex"]
    session_started_at: str | None
```

- [ ] **Step 4: Implement validators and remove dead fields**

Use `ipaddress.ip_address()` after normalizing `localhost`; accept only loopback addresses. Validate interaction and bridge sample rates to exact native values.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run: `uv run pytest tests/test_config.py tests/test_control.py -q`

- [ ] **Step 6: Commit the shared contract slice**

```bash
git add src/voice_realtime/config.py src/voice_realtime/ui/protocol.py tests/test_config.py tests/test_control.py
git commit -m "refactor(config): 收敛运行时配置与控制协议"
```

### Task 2: Repair the WhisperLiveKit subtitle path

**Files:**
- Modify: `src/voice_realtime/subtitles/events.py`
- Modify: `src/voice_realtime/subtitles/launcher.py`
- Modify: `src/voice_realtime/subtitles/consumer.py`
- Modify: `src/voice_realtime/ui/subtitle_proxy.py`
- Modify: `tests/test_subtitles.py`
- Modify: `tests/test_subtitle_proxy.py`
- Modify: `tests/test_events_tracker.py`

**Interfaces:**
- Consumes: `SubtitleSettings.allow_model_downloads`, `output_dir`.
- Produces: `SubtitleProxy.state`, reconnecting full-snapshot proxy, atomic SRT output.

- [ ] **Step 1: Write failing PCM and snapshot tests**

```python
def test_build_server_argv_enables_raw_pcm(settings: SubtitleSettings) -> None:
    assert "--pcm-input" in build_server_argv(settings)

async def test_existing_confirmed_line_does_not_hide_new_partial(proxy) -> None:
    await proxy._broadcast_payload({"lines": [LINE], "buffer_transcription": "新内容"})
    await proxy._broadcast_payload({"lines": [LINE], "buffer_transcription": "新内容继续"})
    assert client.await_count == 2
```

- [ ] **Step 2: Run subtitle tests and confirm RED**

Run: `uv run pytest tests/test_subtitles.py tests/test_subtitle_proxy.py tests/test_events_tracker.py -q`

- [ ] **Step 3: Implement service-side PCM and offline fail-fast**

Always append `--pcm-input`. Pass `--model_dir` when present; otherwise raise unless `allow_model_downloads=True`, in which case pass `--model <model_size>`.

- [ ] **Step 4: Replace event-level dedupe with snapshot signature**

```python
signature = (
    tuple((line.get("start"), line.get("end"), line.get("speaker"), line.get("text")) for line in lines),
    payload.get("buffer_transcription") or "",
)
```

Broadcast on signature change. Track all new confirmed lines for CLI consumers and reset partial state when a segment confirms.

- [ ] **Step 5: Implement cancellable reconnect state machine**

Maintain one supervisor task. Use exponential delays `1, 2, 4, 8, 16, 30`; stop must cancel sleep and close the current stream. When no browser client exists, discard incoming microphone chunks before queueing.

- [ ] **Step 6: Implement atomic SRT persistence**

Write `current.srt.tmp`, then `Path.replace(current.srt)` after each confirmed change. On clean stop, copy the final content to `session-YYYYMMDD-HHMMSS.srt`.

- [ ] **Step 7: Run focused tests and confirm GREEN**

Run: `uv run pytest tests/test_subtitles.py tests/test_subtitle_proxy.py tests/test_events_tracker.py -q`

- [ ] **Step 8: Commit subtitle repairs**

```bash
git add src/voice_realtime/subtitles src/voice_realtime/ui/subtitle_proxy.py tests/test_subtitles.py tests/test_subtitle_proxy.py tests/test_events_tracker.py
git commit -m "fix(subtitles): 接通 PCM 字幕并恢复断线快照"
```

### Task 3: Make TTS and LM Studio resources deterministic

**Files:**
- Modify: `src/voice_realtime/tts_bridge/schema.py`
- Modify: `src/voice_realtime/tts_bridge/engine.py`
- Modify: `src/voice_realtime/tts_bridge/server.py`
- Modify: `src/voice_realtime/interaction/reasoning.py`
- Modify: `tests/test_engine.py`
- Modify: `tests/test_server.py`
- Modify: `tests/test_reasoning.py`

**Interfaces:**
- Consumes: fixed 24k BridgeSettings.
- Produces: per-request voice, serial/cancellable model generation, `LmStudioNativeLLMService.close()`.

- [ ] **Step 1: Write failing voice, concurrency, cancellation, and close tests**

```python
async def test_request_voice_overrides_engine_default(client, engine) -> None:
    await client.post("/v1/audio/speech", json={"model": "local", "input": "你好", "voice": "warm", "response_format": "pcm"})
    assert engine.calls[-1]["voice"] == "warm"

async def test_native_client_is_closed(service) -> None:
    await service.close()
    service._http.aclose.assert_awaited_once()
```

- [ ] **Step 2: Run TTS/LLM tests and confirm RED**

Run: `uv run pytest tests/test_engine.py tests/test_server.py tests/test_reasoning.py -q`

- [ ] **Step 3: Serialize MLX generation and bound the bridge queue**

Use `asyncio.Lock` around one generation. Use a bounded `asyncio.Queue(maxsize=8)` and a `threading.Event` checked by the producer before scheduling each chunk. Set the event in generator cancellation/finally.

- [ ] **Step 4: Honor request voice and remove double WAV aggregation**

Resolve `voice = req.voice or engine.voice`. PCM returns the async generator. WAV consumes PCM exactly once into a bytearray, constructs one correct header, and returns the single body.

- [ ] **Step 5: Add explicit LM Studio lifecycle and SSE validation**

Configure `httpx.Timeout(connect=5, read=None, write=10, pool=5)`. Reject `error` SSE events and non-string delta content. Close the custom client from the Pipecat stop hook and from explicit `close()`.

- [ ] **Step 6: Run focused tests and confirm GREEN**

Run: `uv run pytest tests/test_engine.py tests/test_server.py tests/test_reasoning.py -q`

- [ ] **Step 7: Commit TTS/LLM repairs**

```bash
git add src/voice_realtime/tts_bridge src/voice_realtime/interaction/reasoning.py tests/test_engine.py tests/test_server.py tests/test_reasoning.py
git commit -m "fix(tts): 收敛音色流式生成与 LLM 资源生命周期"
```

### Task 4: Build an acknowledged frontend control channel

**Files:**
- Modify: `ui/package.json`
- Modify: `ui/package-lock.json`
- Modify: `ui/src/hooks/useEventSocket.ts`
- Create: `ui/src/hooks/useCommandSocket.ts`
- Modify: `ui/src/stores/assistantStore.ts`
- Modify: `ui/src/stores/uiSettingsStore.ts`
- Modify: `ui/src/components/AssistantPanel.tsx`
- Modify: `ui/src/components/StatusBar.tsx`
- Modify: `ui/src/stores/subtitleStore.ts`
- Create: `ui/src/hooks/useEventSocket.test.ts`
- Create: `ui/src/stores/assistantStore.test.ts`
- Create: `ui/src/stores/subtitleStore.test.ts`

**Interfaces:**
- Consumes: command response/state shapes defined in `ui/protocol.py`; JSON field names are snake_case on the wire.
- Produces: `sendCommand(payload): Promise<RuntimeStateSnapshot>` with reconnect and acknowledgement.

- [ ] **Step 1: Add Vitest and failing reducer/socket tests**

```typescript
it("enters thinking after user silence", () => {
  const next = reduceAssistantEvent(listeningSnapshot, { type: "vad", state: "user_silence" });
  expect(next.phase).toBe("thinking");
});

it("does not reconnect after unmount", () => {
  cleanup();
  vi.advanceTimersByTime(60_000);
  expect(MockWebSocket.instances).toHaveLength(1);
});
```

- [ ] **Step 2: Run frontend tests and confirm RED**

Run: `npm test -- --run`

- [ ] **Step 3: Fix event socket cancellation and stale URL closure**

Use a `disposedRef`, clear the active retry timer before every new schedule, and make `connect` the stable callback that owns the current URL. `onclose` must not schedule after cleanup.

- [ ] **Step 4: Implement command request/response correlation**

Maintain a `Map<requestId, {resolve,reject,timer}>`. Resolve only matching `ok=true`; reject stable server errors and timeouts. On reconnect, consume the server state snapshot before enabling controls.

- [ ] **Step 5: Make server state authoritative**

Persist persona, voice, duplex, and mute only after acknowledgement. Apply handshake state on page load. Replace the page-load timer with `session_started_at`.

- [ ] **Step 6: Correct reducer and SRT time behavior**

`user_silence` and final STT enter thinking; stopped/error reset deterministically. Parse both comma and dot fractions and always output `HH:MM:SS,mmm`.

- [ ] **Step 7: Run tests and production build**

Run: `npm test -- --run && npm run build`

- [ ] **Step 8: Commit frontend control repairs**

```bash
git add ui/package.json ui/package-lock.json ui/src
git commit -m "fix(ui): 以确认式控制通道同步真实运行状态"
```

### Task 5: Introduce the single-owner InteractionSession

**Files:**
- Create: `src/voice_realtime/interaction/ownership.py`
- Create: `src/voice_realtime/interaction/session.py`
- Modify: `src/voice_realtime/interaction/runner.py`
- Modify: `src/voice_realtime/ui/runtime.py`
- Create: `tests/test_interaction_session.py`
- Modify: `tests/test_runtime.py`

**Interfaces:**
- Consumes: protocol state enums and current pipeline builder.
- Produces: `InteractionSession` used by both UI and headless runner.

- [ ] **Step 1: Write failing ownership and lifecycle tests**

Test two ownership objects against one temporary lock path; the second must raise `InteractionOwnershipError`. Test `stop()` awaits `runner.end()`, clears audio, cancels only after timeout, and preserves persona/duplex across restart.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `uv run pytest tests/test_interaction_session.py tests/test_runtime.py -q`

- [ ] **Step 3: Implement `InteractionOwnership`**

Open the explicit lock file and call `fcntl.flock(fd, LOCK_EX | LOCK_NB)`. Keep the descriptor for the ownership lifetime and release it in `close()`/context exit.

- [ ] **Step 4: Implement `InteractionSession` state machine**

Create states `stopped/starting/running/stopping/error/ownership_conflict`. Store runner, worker, task, timeout task, persona, duplex, and started timestamp. All public methods serialize through one `asyncio.Lock`.

- [ ] **Step 5: Reuse the session from both entrypoints**

`runner.py` creates a direct-input session. `UIRuntime` owns an injected-audio session. Remove duplicated WorkerRunner and timeout code from both entrypoints.

- [ ] **Step 6: Run focused tests and confirm GREEN**

Run: `uv run pytest tests/test_interaction_session.py tests/test_runtime.py -q`

- [ ] **Step 7: Commit session architecture**

```bash
git add src/voice_realtime/interaction/ownership.py src/voice_realtime/interaction/session.py src/voice_realtime/interaction/runner.py src/voice_realtime/ui/runtime.py tests/test_interaction_session.py tests/test_runtime.py
git commit -m "refactor(interaction): 统一 UI 与 headless 会话生命周期"
```

### Task 6: Repair AudioHub backpressure and real microphone mute

**Files:**
- Modify: `src/voice_realtime/audio/hub.py`
- Modify: `src/voice_realtime/audio/audio_injector.py`
- Modify: `tests/test_audio_hub.py`
- Modify: `tests/test_audio_injector.py`

**Interfaces:**
- Produces: `AudioHub.muted`, `set_muted(bool)`, trustworthy `start()`, bounded per-sink delivery.

- [ ] **Step 1: Write failing tests**

Cover open failure propagation, 512 frames producing the documented 32ms semantics, muted chunks not reaching sinks, queue drain on mute, and slow sink queue remaining bounded.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `uv run pytest tests/test_audio_hub.py tests/test_audio_injector.py -q`

- [ ] **Step 3: Move stream open acknowledgement across the thread boundary**

Use a thread-safe future/event to report either the opened stream or the exception before `start()` returns.

- [ ] **Step 4: Replace per-chunk task creation with bounded sink workers**

Each sink owns `asyncio.Queue(maxsize=8)` and one task. On overflow, drop the oldest frame and increment a counter. Removal cancels and awaits its worker.

- [ ] **Step 5: Implement actual mute and injector drain**

Mute prevents all sink delivery. Runtime additionally disables its interaction sink and drains `audio_queue` before restart.

- [ ] **Step 6: Run focused tests and confirm GREEN**

Run: `uv run pytest tests/test_audio_hub.py tests/test_audio_injector.py -q`

- [ ] **Step 7: Commit audio lifecycle repairs**

```bash
git add src/voice_realtime/audio tests/test_audio_hub.py tests/test_audio_injector.py
git commit -m "fix(audio): 实现有界扇出与真实麦克风静音"
```

### Task 7: Correct echo, interruption, and observer algorithms

**Files:**
- Modify: `src/voice_realtime/interaction/pipeline.py`
- Modify: `src/voice_realtime/ui/assistant_bridge.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_assistant_bridge.py`

**Interfaces:**
- Consumes: current duplex mode from InteractionSession.
- Produces: single-writer EchoState and nullable per-turn metrics.

- [ ] **Step 1: Write failing algorithm tests**

Add tests proving LLMTextFrame does not mute input, one-character/common acknowledgements pass, self-echo only drops during the active/tail window, headphone speech remains unlocked past 20 frames, and timestamps reset on every user turn.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `uv run pytest tests/test_pipeline.py tests/test_assistant_bridge.py -q`

- [ ] **Step 3: Make TTSStateObserver the only EchoState writer**

Remove EchoState writes from BotTextRecorder and mute strategy. The latter reads `EchoState.is_suppressing()` only.

- [ ] **Step 4: Correct headphone thresholds and text minimums**

Use trigger gain above the sustain threshold; re-lock only below `peak_envelope * 0.8` for the configured streak. Apply `min_chars`; pass common acknowledgements and all text outside the active/tail echo window.

- [ ] **Step 5: Correct assistant metrics and broadcasting**

Reset every turn timestamp on UserStartedSpeaking. Emit null for unavailable stages. First TTS audio, not TTSStarted, closes TTS TTFB. Use a bounded client broadcaster rather than awaiting all sockets inline.

- [ ] **Step 6: Run focused tests and confirm GREEN**

Run: `uv run pytest tests/test_pipeline.py tests/test_assistant_bridge.py -q`

- [ ] **Step 7: Commit algorithm repairs**

```bash
git add src/voice_realtime/interaction/pipeline.py src/voice_realtime/ui/assistant_bridge.py tests/test_pipeline.py tests/test_assistant_bridge.py
git commit -m "fix(interaction): 修正回声打断状态与延迟指标"
```

### Task 8: Harden UI control and runtime health

**Files:**
- Modify: `src/voice_realtime/ui/control.py`
- Modify: `src/voice_realtime/ui/server.py`
- Modify: `src/voice_realtime/ui/runtime.py`
- Modify: `tests/test_control.py`
- Modify: `tests/test_ui_server.py`
- Modify: `tests/test_runtime.py`

**Interfaces:**
- Consumes: strict protocol models, InteractionSession, AudioHub and SubtitleProxy state.
- Produces: state handshake, acknowledged commands, Origin enforcement, security headers, `/api/runtime`.

- [ ] **Step 1: Write failing security and command tests**

Test malicious Origin rejection, allowed production/dev origins, extra payload field rejection, persona length cap, stable error codes, initial state event, mute reaching AudioHub, and no raw exception text.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `uv run pytest tests/test_control.py tests/test_ui_server.py tests/test_runtime.py -q`

- [ ] **Step 3: Implement typed dispatch and state responses**

Parse JSON through the strict command union. Each handler returns `CommandResponse(request_id, cmd, ok, state, error_code, message)`. `set_mic_muted` calls the runtime; all state-changing commands return a fresh snapshot.

- [ ] **Step 4: Enforce browser boundary and headers**

Validate Origin before `accept()`. Allow `http://127.0.0.1:<ui-port>`, `http://localhost:<ui-port>`, and Vite `:5173`. Add CSP self/connect ws/wss, nosniff, no-referrer, and DENY frame headers.

- [ ] **Step 5: Expose truthful runtime health**

`/api/runtime` returns component states and started timestamp. `/api/services` retains external probes but includes target model presence when available.

- [ ] **Step 6: Run focused tests and confirm GREEN**

Run: `uv run pytest tests/test_control.py tests/test_ui_server.py tests/test_runtime.py -q`

- [ ] **Step 7: Commit control hardening**

```bash
git add src/voice_realtime/ui tests/test_control.py tests/test_ui_server.py tests/test_runtime.py
git commit -m "fix(ui): 加固控制边界并暴露真实运行状态"
```

### Task 9: Integrate, document, and prove completion

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `todo.md`
- Modify: `docs/Voice-Studio-UI-设计方案.md`
- Modify: `docs/实时语音交互与字幕-方案与最佳实践.md`
- Modify: `docs/架构图与流程图.md`
- Modify: relevant tests for integration gaps found during verification

**Interfaces:**
- Consumes: all preceding tasks.
- Produces: one documented four-unit topology and enforced default quality gate.

- [ ] **Step 1: Enable coverage in the default pytest gate and Python 3.12 metadata**

Set mypy `python_version = "3.12"`, classifier 3.12, and pytest addopts to include `--cov=src --cov-report=term-missing` while preserving strict markers and timeout.

- [ ] **Step 2: Remove test coroutine warnings**

Run the full suite with warnings visible, identify every project-owned unawaited coroutine, and repair the fake/fixture lifecycle rather than suppressing warnings.

- [ ] **Step 3: Update all operational documentation**

Document `vr-ui` + `vr-subtitles` + `vr-bridge` + LM Studio. State that `vr-interact` is an alternative, never a fifth parallel service. Document PCM, offline, mute, control and health semantics.

- [ ] **Step 4: Run all automated gates**

```bash
uv run pytest tests/
uv run mypy src/
uv run ruff check src/ tests/
cd ui && npm test -- --run
cd ui && npm run build
cd ui && npm audit --audit-level=high
```

- [ ] **Step 5: Perform bounded runtime verification**

Use temporary ports or existing controlled local services to verify ownership conflict, `--pcm-input`, live partial after confirmed, stop/restart queue freshness, command reconnect/state, echo/barge-in manual behavior, and rejected hostile Origin. Do not overwrite user logs; use a temporary runtime directory.

- [ ] **Step 6: Audit every specification requirement**

Create a checklist from the design spec and link each item to a test, command output, or manual runtime observation. Any item without direct evidence remains incomplete and must be implemented or explicitly proven inapplicable.

- [ ] **Step 7: Commit integration and docs**

```bash
git add pyproject.toml README.md AGENTS.md todo.md docs tests
git commit -m "docs: 收敛 Voice Studio 单一所有者运行架构"
```
