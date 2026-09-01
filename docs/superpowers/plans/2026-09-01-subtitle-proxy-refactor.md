---
title: "SubtitleProxy 职责拆分实施计划"
status: draft
type: execution_plan
date: 2026-09-01
owners: ["voice-realtime-core"]
related_documents:
  - "docs/architecture/实时语音交互与字幕-方案与最佳实践.md"
  - "docs/decisions/0011-speechrail-only-asr.md"
---

# SubtitleProxy Responsibility Refactor Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task, with a review checkpoint after each commit.

**Goal:** 保留 `SubtitleProxy` 的 public façade 和单 PCM owner 行为，把浏览器订阅 fan-out、普通字幕 SpeechRail session、会议 capture session 与 SRT snapshot/archive 拆成独立组件。

**Architecture:** 浏览器不是一个由 `SubtitleProxy` 接管的 WebSocket session；UI server 继续把 `websocket.send_text` 作为 callback 交给 façade。`SubtitleClientHub` 管理每客户端有界队列；`StandardSubtitleSession` 管理 `_supervise_connection/_serve_connection/_audio_send_loop`；`MeetingCaptureSession` 管理 capture event/send/reconnect/gap/finalize；`SrtArchive` 管理 `current.srt` 原子替换与 epoch archive。Façade 仍协调互斥所有权和兼容方法。

**Tech Stack:** Python 3.12、asyncio、WebSocket callbacks、SpeechRail streaming ASR、pytest、Ruff、mypy。

---

## 执行边界

- 工作目录：`/Users/hrygo/Documents/voice-realtime`
- 前置：完成 `2026-09-01-speechrail-asr-boundary-refactor.md`。
- 后续：meeting lifecycle 计划依赖 façade 的 typed `last_window` 与 capture methods。
- 保持 `SubtitleProxy(settings, *, transcriber_factory=None, speechrail_connection_factory=None, backoff_delays=..., clock=...)` constructor。
- 保持 public 方法：listener add/remove、`add_client/remove_client`、diagnostics、`start/stop`、browser prepare/commit/abort、meeting prepare/commit/abort/finish、`push_audio`、`clear_subtitles`。
- 浏览器 disconnect 只移除自己的 sender；不得关闭 SpeechRail capture owner。最后一个浏览器离开时，仅在没有 browser capture 和 meeting capture 时 drain PCM queue。
- source reconnect 必须创建新 epoch 并发出 gap；断线不复用旧时间轴，不重放 PCM。
- façade 创建唯一一个 maxsize=512 PCM queue 并注入两类 session；任一时刻只有 committed owner 的 send loop 可以 drain。browser/meeting 所有权切换前必须停止旧 loop 并 drain 旧 epoch，禁止给两类 session 各建一份可并行消费的 queue。
- `current.srt` 继续原子写入，只有 confirmed snapshot 变化才落盘；close epoch 最多 archive 一次并原子清空。
- 不在本计划改变 meeting persistence、UI WebSocket protocol、队列大小、backoff 序列、错误 code 或隐私记录。

## 目标文件

- Create: `src/voice_realtime/ui/subtitle_clients.py`
- Create: `src/voice_realtime/ui/subtitle_archive.py`
- Create: `src/voice_realtime/ui/subtitle_sessions.py`
- Modify: `src/voice_realtime/ui/subtitle_proxy.py`
- Create: `tests/test_subtitle_components.py`
- Modify: `tests/asr/test_proxy_contract.py`
- Modify: `tests/test_ui_server.py`
- Modify: `tests/test_meeting_session.py`
- Modify: `tests/test_runtime_mode.py`

## Task 1: 固化 façade 可观察行为

**Files:**

- Create: `tests/test_subtitle_components.py`
- Modify: `tests/asr/test_proxy_contract.py`
- Modify: `tests/test_ui_server.py`
- Modify: `tests/test_meeting_session.py`
- Modify: `tests/test_runtime_mode.py`

- [ ] **Step 1: 增加 ownership 与 lifecycle 保护测试**

覆盖：每客户端独立 maxsize=8 queue、慢客户端只被自身清理、late subscriber 立即收到快照、browser disconnect 不关闭 meeting capture、browser/meeting prepare 互斥、capture reconnect 增加 source epoch 并报告 gap、finish timeout 携带最后 window、stop/abort 幂等。

- [ ] **Step 2: 增加 SRT epoch 保护测试**

确认 partial-only 不写 SRT，重复 confirmed snapshot 不重写，`current.srt.tmp → current.srt` 原子替换，close epoch 只 archive 一次，文件名冲突使用数字 suffix，clear 后广播 reset 的既有时机不变。

- [ ] **Step 3: 运行基线**

```bash
uv run --extra dev pytest tests/asr/test_proxy_contract.py tests/test_ui_server.py \
  tests/test_meeting_session.py tests/test_runtime_mode.py -q --no-cov
```

预期：现有基线通过；新组件测试因模块不存在而失败。

## Task 2: 提取浏览器 client hub 与 SRT archive

**Files:**

- Create: `src/voice_realtime/ui/subtitle_clients.py`
- Create: `src/voice_realtime/ui/subtitle_archive.py`
- Modify: `src/voice_realtime/ui/subtitle_proxy.py`
- Modify: `tests/test_subtitle_components.py`
- Modify: `tests/asr/test_proxy_contract.py`

- [ ] **Step 1: 实现 callback-based client hub**

```python
class SubtitleClientHub:
    def add(self, sender: ClientSender, *, snapshot: Mapping[str, object] | None) -> str: ...
    def remove(self, sender: ClientSender) -> None: ...
    async def publish(self, payload: Mapping[str, object]) -> None: ...
    async def close(self) -> None: ...

    @property
    def has_clients(self) -> bool: ...
```

hub 为每个 sender 建立自己的 bounded queue/task，publish 不等待慢 sender；send task 失败只删除该 channel。它不导入 FastAPI `WebSocket`、SpeechRail client、meeting repository 或文件系统。

- [ ] **Step 2: 实现原子 SRT archive**

```python
class SrtArchive:
    def persist_confirmed(self, payload: Mapping[str, object]) -> None: ...
    def close_epoch(self) -> Path | None: ...
    def clear_current(self) -> None: ...
```

保留现有 timestamp rendering、confirmed signature、0600 之外的当前文件语义和 `shutil.copy2` archive 行为；类不广播 UI event。

- [ ] **Step 3: 让 façade 委托纯组件并提交**

```bash
uv run --extra dev pytest tests/test_subtitle_components.py tests/asr/test_proxy_contract.py -q --no-cov
uv run --extra dev ruff check src/voice_realtime/ui/subtitle_clients.py \
  src/voice_realtime/ui/subtitle_archive.py src/voice_realtime/ui/subtitle_proxy.py \
  tests/test_subtitle_components.py
uv run --extra dev mypy src/voice_realtime/ui
git add src/voice_realtime/ui/subtitle_clients.py src/voice_realtime/ui/subtitle_archive.py \
  src/voice_realtime/ui/subtitle_proxy.py tests/test_subtitle_components.py tests/asr/test_proxy_contract.py
git commit -m "refactor: extract subtitle clients and archive"
```

## Task 3: 提取普通字幕 SpeechRail session

**Files:**

- Create: `src/voice_realtime/ui/subtitle_sessions.py`
- Modify: `src/voice_realtime/ui/subtitle_proxy.py`
- Modify: `tests/test_subtitle_components.py`
- Modify: `tests/asr/test_proxy_contract.py`

- [ ] **Step 1: 定义 StandardSubtitleSession**

```python
class StandardSubtitleSession:
    async def prepare(self, *, timeout_secs: float) -> SubtitlePreparation: ...
    def commit(self, preparation: SubtitlePreparation) -> None: ...
    async def abort_prepared(self, preparation: SubtitlePreparation) -> None: ...
    async def push_audio(self, data: bytes) -> None: ...
    async def stop(self) -> None: ...
```

该类拥有普通字幕的 stream、ready/active、supervisor、send loop、backoff 和 subtitle epoch；构造时接收 façade 的共享 PCM queue，通过 callbacks 交付 `ASRWindow`/gap/state，不直接管理浏览器 sender 或 SRT。

- [ ] **Step 2: 迁移 `_supervise_connection/_serve_connection/_audio_send_loop`**

这些方法是 SpeechRail 普通字幕连接，不得命名为 `BrowserSubtitleSession`。重连后创建新 epoch；close 旧 stream 后 drain 只属于旧 epoch 的待发送 PCM，禁止把旧 PCM 注入新连接。

- [ ] **Step 3: 运行并提交普通字幕 session**

```bash
uv run --extra dev pytest tests/test_subtitle_components.py tests/asr/test_proxy_contract.py tests/test_ui_server.py -q --no-cov
uv run --extra dev ruff check src/voice_realtime/ui/subtitle_sessions.py src/voice_realtime/ui/subtitle_proxy.py
uv run --extra dev mypy src/voice_realtime/ui
git add src/voice_realtime/ui/subtitle_sessions.py src/voice_realtime/ui/subtitle_proxy.py \
  tests/test_subtitle_components.py tests/asr/test_proxy_contract.py tests/test_ui_server.py
git commit -m "refactor: extract standard subtitle session"
```

## Task 4: 提取会议 capture session

**Files:**

- Modify: `src/voice_realtime/ui/subtitle_sessions.py`
- Modify: `src/voice_realtime/ui/subtitle_proxy.py`
- Modify: `tests/test_subtitle_components.py`
- Modify: `tests/test_meeting_session.py`
- Modify: `tests/test_runtime_mode.py`

- [ ] **Step 1: 定义 MeetingCaptureSession**

```python
class MeetingCaptureSession:
    async def prepare(
        self,
        owner: str,
        *,
        timeout_secs: float,
        speaker_count_hint: int | None,
    ) -> CapturePreparation: ...
    def commit(self, preparation: CapturePreparation) -> None: ...
    async def abort_prepared(self, preparation: CapturePreparation) -> None: ...
    async def finish(self, *, timeout_secs: float) -> TranscriptWindow: ...
    async def abort(self) -> None: ...

    @property
    def last_window(self) -> TranscriptWindow | None: ...
```

构造时注入同一个共享 PCM queue 以及 `to_transcript_window: Callable[[ASRWindow], TranscriptWindow]`；ASR neutral DTO 只在该明确边界转换一次。meeting session 内部和 listener 始终看到 `TranscriptWindow`，普通字幕路径仍使用 `ASRWindow`。

- [ ] **Step 2: 迁移 capture event/send/reconnect/gap**

capture session 独占 `_capture_stream`、event/send tasks、stream-available/ready-to-stop、generation、offset/audio/input ms、last window 和 listener callbacks。重连关闭旧 source epoch并报告精确 gap；finish 仍先停止接收新 PCM、等待 queue join、commit/EOF、等待 final window，再关闭 stream。

- [ ] **Step 3: 暴露 typed `last_window`**

`SubtitleProxy.last_window` 只委托 `MeetingCaptureSession.last_window`，为下一阶段移除 `MeetingSession` 对 `_capture_last_window` 的私有 `getattr` 做准备。保留原内部属性兼容一轮只会形成双事实源，禁止这样做。

- [ ] **Step 4: 运行并提交 meeting capture**

```bash
uv run --extra dev pytest tests/test_subtitle_components.py tests/asr/test_proxy_contract.py \
  tests/test_meeting_session.py tests/test_runtime_mode.py -q --no-cov
uv run --extra dev ruff check src/voice_realtime/ui/subtitle_sessions.py src/voice_realtime/ui/subtitle_proxy.py \
  tests/test_subtitle_components.py
uv run --extra dev mypy src
git add src/voice_realtime/ui/subtitle_sessions.py src/voice_realtime/ui/subtitle_proxy.py \
  tests/test_subtitle_components.py tests/test_meeting_session.py tests/test_runtime_mode.py
git commit -m "refactor: extract meeting capture session"
```

## Task 5: 收敛 façade 与完整门禁

- [ ] **Step 1: 删除重复状态**

`SubtitleProxy` 最终只保留 settings/factory、共享 PCM queue、单 owner 仲裁、四个组件装配、兼容 public 方法和 diagnostics 聚合。组件已拥有的 stream/task/event/counter 不得在 façade 再存一份；`push_audio()` 必须先确认 committed owner，再通知 audio listeners 并把数据写入这一个 queue。

- [ ] **Step 2: 检查 UI WebSocket 调用模型**

`ui/server.py` 仍执行 `client_id = proxy.add_client(websocket.send_text)` 并在 `finally` 调用 `remove_client(websocket.send_text)`；不要新增 `serve(websocket)` 或把 receive loop 移入 proxy。

- [ ] **Step 3: 运行聚焦与项目门禁**

```bash
uv run --extra dev pytest tests/test_subtitle_components.py tests/asr/test_proxy_contract.py \
  tests/test_ui_server.py tests/test_meeting_session.py tests/test_runtime_mode.py -q --no-cov
uv run --extra dev ruff check src tests
uv run --extra dev mypy src
git diff --check
```

## 完成标准

- [ ] browser callback hub、standard SpeechRail session、meeting capture session、SRT archive 各有独立测试。
- [ ] `SubtitleProxy` constructor 和 public façade 保持兼容。
- [ ] 单 PCM owner、bounded queue、epoch、gap、reconnect、timeout 与 SRT 行为不变。
- [ ] `last_window` 是 typed public property，不再要求上层访问私有字段。
- [ ] UI server WebSocket protocol、meeting persistence 和 public contracts 未改变。
