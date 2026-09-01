---
title: "Voice Studio UI 设计方案"
description: "Voice Studio 前端控制台架构设计、单源麦克风控制面、组件状态机、WebSocket 协议桥接与交互设计规范"
status: active
type: guide
category: frontend
version: "v1.1.0"
date: 2026-08-21
last_updated: 2026-09-01
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-ui"
tags:
  - voice-studio
  - react
  - zustand
  - ui-design
  - state-machine
  - websocket-bridge
scope:
  - "voice_realtime.ui"
  - "ui"
related_documents:
  - "docs/architecture/系统总体架构与详细设计方案.md"
  - "docs/architecture/实时语音交互与字幕-方案与最佳实践.md"
  - "docs/manuals/会议助手后端运行与前后端联调.md"
---

# Voice Studio UI 设计方案（架构整治版，2026-08-21）

## 1. 目标与边界

Voice Studio 是本机语音系统的默认运行入口，不只是展示页。`vr-ui` 同时拥有：

- 唯一麦克风采集器 `AudioHub`；
- 注入式 Pipecat 交互会话；
- SpeechRail Realtime v2 字幕/会议 ASR 代理；
- 浏览器事件面、控制面和静态资源。

浏览器不采集音频，只展示服务端事件并发送严格控制命令。关闭浏览器不会停止服务。
`vr-interact` 仅用于无 UI 模式，与 `vr-ui` 通过文件锁互斥。

## 2. 组件关系

```mermaid
flowchart LR
    MIC[麦克风 16kHz mono int16] --> HUB[AudioHub<br/>有界扇出 / 真静音]
    HUB --> AQ[AudioInjector 队列]
    HUB --> SP[SubtitleProxy]
    AQ --> SESSION[InteractionSession<br/>Pipecat]
    SESSION --> OBS[StatusBridgeObserver]
    SP --> SR[SpeechRail Realtime v2 :8201<br/>ASR / diarization profile]
    SESSION --> LM[LM Studio :1234]
    SESSION --> TTS[SpeechRail TTS :8201]
    OBS --> ALOG[/ws/assistant]
    SP --> SLOG[/ws/subtitles]
    CTRL[/ws/assistant/cmd] --> SESSION
    ALOG --> WEB[React UI :8100]
    SLOG --> WEB
    WEB --> CTRL
```

`InteractionSession` 是 UI 与 headless 入口的共享边界，持有 `WorkerRunner`、worker、任务、
超时任务、persona、duplex 和会话时间。停止时先调用 `runner.end()`，仅在超时后取消任务。

## 3. 音频与字幕

- AudioHub 一次读取 512 个采样帧，即约 32ms、1024 bytes；每个 sink 只有一个工作协程和
  固定容量队列。队满丢最旧帧并计数，不创建无限任务。
- 静音在服务端阻断所有 sink 投递，并清空交互队列；前端不做乐观伪静音。
- SubtitleProxy 在 `subtitles`/`meeting` workload 激活期间消费 SpeechRail transcription events，并在应用边界组装全量快照。快照由所有 confirmed 行与当前 partial 共同构成，
  因而已有 confirmed 不会冻结后续 partial。
- SpeechRail 断线后清空旧音频并可取消地指数退避；没有浏览器订阅时仍维持上游消费，但不积压
  浏览器消息。
- confirmed 字幕原子替换 `runtime/subtitles/current.srt`，停止时生成时间戳归档。

## 4. 控制协议

连接成功后，服务端首先发送：

```json
{"event":"state","state":{"pipeline":"running","subtitle":"connected","mic_muted":false,"persona":null,"voice":"default","duplex_mode":"speaker_focus","session_started_at":"2026-08-21T00:00:00+00:00"}}
```

请求和响应：

```json
{"request_id":"uuid","cmd":"set_mic_muted","muted":true}
{"request_id":"uuid","cmd":"set_mic_muted","ok":true,"state":{"pipeline":"running","subtitle":"connected","mic_muted":true,"persona":null,"voice":"default","duplex_mode":"speaker_focus","session_started_at":"2026-08-21T00:00:00+00:00"},"error_code":null,"message":null}
```

支持命令：`clear_context`、`stop_session`、`restart`、`set_persona`、`set_voice`、
`set_duplex_mode`、`set_mic_muted`。额外字段、缺失字段、超长 persona 和非法枚举均被拒绝。
失败只返回稳定错误码与用户可读消息，不暴露内部异常。

前端只有收到成功确认才更新并持久化 persona、voice、duplex 和 mute；页面重载后以服务端
握手状态为准，localStorage 只是在尚未连通时的显示缓存。

## 5. 事件与状态

### 助手事件 `/ws/assistant`

- `vad`: `user_speaking` / `user_silence`
- `stt`: `interim` / `final`
- `llm`: `streaming` / `final`
- `tts`: `started` / `synthesizing` / `stopped`
- `interruption`: `detected`
- `system`: `pipeline_started` / `pipeline_stopped`
- `metrics`: `stt_ms`、`llm_ttft_ms`、`tts_ttfb_ms`、`e2e_ms`

每轮在 `UserStartedSpeakingFrame` 重置时间戳；TTS TTFB 在首个 `TTSAudioRawFrame` 闭合，
不可计算的阶段为 `null`。每个浏览器客户端使用有界发送队列，慢客户端不会阻塞管道。

### 字幕事件 `/ws/subtitles`

转发由 SpeechRail transcription events 组装的 full-state：`lines` 为 confirmed，`buffer_transcription` 为 partial。前端 reducer 用
快照替换而非追加猜测，并生成标准 `HH:MM:SS,mmm` SRT 时间。

## 6. 浏览器安全边界

- 所有服务配置只接受 loopback 地址。
- WebSocket 允许 UI 当前端口、`localhost`/`127.0.0.1` 及 Vite 5173；其他 Origin 以
  1008 拒绝。无 Origin 的本地非浏览器客户端保留可用性。
- HTTP 响应带 CSP、`nosniff`、`no-referrer` 和 `DENY` frame 头。
- `/api/runtime` 返回权威组件状态；`/api/services` 返回外部探活与目标 LLM 是否加载。

## 7. 前端状态管理

- `useEventSocket`：事件面和控制面复用的可取消指数退避实现。
- `useCommandSocket`：状态握手、`request_id` 关联、超时和断线拒绝。
- `assistantStore`：默认基准态为 `listening`（👂 聆听麦克风）、`user_silence` / STT `final` → `thinking`（🧠 LM Studio 推理）、`TTS started` → `speaking`（🗣️ SpeechRail TTS 播报）、`TTS stopped` → 闭环返回 `listening`，打断与超时亦安全恢复 `listening`，异常进入 `degraded`/`stopped`。
- `subtitleStore`：完整快照 reducer、confirmed/partial 分离和 SRT 导出。
- 会话计时使用服务端 `session_started_at`，不再使用页面加载时间。

## 8. 验收

Python 聚焦测试覆盖控制 schema、状态握手、Origin、真实静音、运行时生命周期和安全响应头；
Vitest 覆盖控制确认、断线重连、助手状态、字幕快照和 StatusBar。全量门禁见项目 README。

历史上“同时运行 vr-ui 与 vr-interact 的多服务冒烟”结论已废止；当前拓扑由 `vr-ui`、SpeechRail、LM Studio 与 PostgreSQL 构成，
headless 入口只能替代 UI 入口。
