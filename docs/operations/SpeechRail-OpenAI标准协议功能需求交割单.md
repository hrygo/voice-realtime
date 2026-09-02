---
title: "SpeechRail 采用 OpenAI 标准协议功能需求交割单"
description: "sona 向 SpeechRail 交割的功能需求：让 SpeechRail 的 OpenAI 兼容实时协议完整覆盖流式 ASR 分人、流式 TTS、取消与 EOF，从而支持弃用 /v2/realtime"
status: completed
type: technical_spec
category: asr
version: "v1.0.0"
date: 2026-09-01
last_updated: 2026-09-02
author: "Voice Realtime Core Team"
owners:
  - "sona-core"
tags:
  - speechrail
  - openai
  - realtime
  - diarization
  - handover
  - asr
  - tts
scope:
  - "sona.asr"
  - "sona.meeting"
  - "sona.interaction"
  - "sona.ui.subtitle_proxy"
related_documents:
  - "docs/manuals/SpeechRail-Realtime-v2-语音转文字开发对接手册.md"
  - "docs/decisions/0011-speechrail-only-asr.md"
  - "docs/decisions/0012-speechrail-realtime-tts.md"
  - "docs/architecture/系统总体架构与详细设计方案.md"
  - "docs/operations/SpeechRail-OpenAI标准协议功能需求交割单.md"
---

# SpeechRail 采用 OpenAI 标准协议功能需求交割单

## 交割对象

- **需求方**：sona（Sona）
- **执行方**：SpeechRail 服务（`src/speechrail`）
- **目标端点**：`WS /v1/realtime`（OpenAI 兼容实时协议）；补充对齐批量 `POST /v1/audio/transcriptions`

---

## 1. 背景与目标

sona 当前将「语音助手、实时字幕、会议助手」三路实时链路全部承载在 SpeechRail **`/v2/realtime`** 私有协议上。经调研确认：**OpenAI 标准实时协议已能覆盖几乎所有能力**（流式 ASR partials、流式 TTS、取消/打断、commit/EOF），唯一缺口是**实时说话人分离**。

本交割单请求 SpeechRail **把 `/v1/realtime` 从「OpenAI 实时协议子集」补齐为完整的 OpenAI 标准实现**，特别是加入实时说话人分离事件，使 sona 可以：

1. 三路实时链路**统一使用 OpenAI 标准 `/v1/realtime`**；
2. **彻底弃用 `/v2/realtime` 私有协议**；
3. 保留全部既有功能：流式逐字字幕、流式 TTS 边生成边播、打断取消、会议实时分人、EOF 冲刷。

> 关键前提（sona 侧不变量）：**不持久化音频**。故会议说话人分离必须由**实时转录事件**直接携带（`segment.speaker`），不能依赖会议结束后用音频二次批量转写。

---

## 2. 现状与差距（证据）

交割前逐行核对了 SpeechRail Realtime 路由、协议适配器与 OpenAI 官方文档；下表保留交割前的差距快照：

| 能力 | SpeechRail `/v2/realtime` | SpeechRail `/v1/realtime`（现状） | OpenAI 标准（真 API） | 判定 |
|---|---|---|---|---|
| 流式 ASR partials | `transcription.delta` | `transcription.delta`（自定义命名） | `conversation.item.input_audio_transcription.delta` | 已有，但命名非标准 |
| 流式 ASR final | `transcription.completed` | `transcription.completed` | `conversation.item.input_audio_transcription.completed` | 已有，但命名非标准 |
| **实时说话人分离** | `transcription.diarization.completed` | ❌ **无任何分人事件** | `conversation.item.input_audio_transcription.segment`（带 `speaker`） | **❌ 缺口** |
| 流式 TTS（`response.output_audio.delta`） | ✅ | ✅ | ✅ | 已有 |
| 取消/打断（`response.cancel`） | ✅ | ✅ | ✅ | 已有 |
| `input_audio_buffer.commit` / `clear` / EOF | ✅ | ✅ | ✅ | 已有 |
| 批量转写分人（`diarized_json`/`segments.speaker`） | — | ✅（`speaker`/`speakers`） | ✅（`gpt-4o-transcribe-diarize`） | 已有 |

**结论**：`/v1/realtime` 目前只实现了 OpenAI 实时协议的**子集**，事件命名用自定义 `transcription.*`，且**缺少实时分人事件**。这是 sona 保留 v2 的唯一原因。

---

## 3. 功能需求清单（需求可追溯矩阵）

> 编号 NFR 列：Voice → 语音助手；Sub → 实时字幕；Meet → 会议助手。

| 编号 | sona 需求 | 使用方 | 目标 OpenAI 标准事件 | 现状 |
|---|---|---|---|---|
| **R1** | 流式 ASR **partials**（逐字上屏，可替换不落库） | Sub/Meet | `conversation.item.input_audio_transcription.delta` | 🟡 有但命名非标准 |
| **R2** | 流式 ASR **final**（不可变确认片段） | Voice/Sub/Meet | `conversation.item.input_audio_transcription.completed` | 🟡 有但命名非标准 |
| **R3** | **实时说话人分离**（匿名 `spk_1/2/3`，逐段携带） | Meet | `conversation.item.input_audio_transcription.segment`（`text`,`speaker`,`start`,`end`,`content_index`,`id`）；会话内以模型 `gpt-4o-transcribe-diarize` 或 `diarization` 配置启用 | 🔴 **缺失** |
| **R4** | 流式 TTS（24k mono PCM `s16le` base64，边生成边播） | Voice | `response.output_audio.delta` + `response.output_audio.done` | ✅ |
| **R5** | 取消/打断（断播、丢弃未播放缓存） | Voice | `response.cancel`、`input_audio_buffer.clear` | ✅ |
| **R6** | EOF 冲刷与最终事件（保证末句不丢） | Voice/Meet | `input_audio_buffer.commit` → 随后的 `segment`/`completed`（及会话结束信号） | 🟡 commit 有，需明确最终事件 |
| **R7** | 已知说话人参考（可选增强分人准确度） | Meet | `known_speaker_names[]`、`known_speaker_references[]` | 🟡 批量有，实时待定 |
| **R8** | 多语言 / 关键词 / 时间戳粒度（`word`/`segment`） | Voice/Meet | `language`、`languages`、`keywords`、`timestamp_granularities` | 🟡 需对齐 |
| **R9** | 会话语义：`session.created`、事件序、gap/对账边界 | 全部 | `session.created`；每个事件带 `event_id`/`session_id`/`sequence` | 部分有 |
| **R10** | 能力发现/健康：`health`、`models`、`voices` | 全部 | `GET /health`、`/v1/models`、`/v1/voices` | ✅ |

---

## 4. 目标事件契约（OpenAI 标准，全量）

### 4.1 Client → Server
| 事件 | 用途 |
|---|---|
| `session.update` | 配置：`input_audio_transcription.model`（含 diarize）、`input_audio_transcription`、`turn_detection`、TTS `voice`/`language` |
| `input_audio_buffer.append` | 流式追加 16kHz PCM（`s16le` base64） |
| `input_audio_buffer.commit` | 结束本轮、触发最终转写/分人（EOF） |
| `input_audio_buffer.clear` | 丢弃缓存，取消 pending 转写 |
| `conversation.item.create` | 提交文本（用于 TTS 合成） |
| `response.create` | 触发 TTS 合成并流式下发 |
| `response.cancel` | 取消在途 TTS |

### 4.2 Server → Client（需全量实现）
| 事件 | 说明 |
|---|---|
| `session.created` / `conversation.created` | 会话握手 |
| `conversation.item.input_audio_transcription.delta` | **partials**（逐字） |
| `conversation.item.input_audio_transcription.completed` | **final**（确认片段） |
| `conversation.item.input_audio_transcription.segment` | **实时分人段**：`text`、`speaker`、`start`、`end`、`content_index`、`id` |
| `response.created` → `response.output_item.added` → `response.content_part.added` → `response.output_audio.delta` → `response.output_audio.transcript.delta/.done` → `response.output_audio.done` → `response.content_part.done` → `response.output_item.done` → `response.done` | TTS 流式全生命周期 |
| `error` | 稳定错误码（`code`、`message`、`event_id`） |

> **命名要求**：请使用 OpenAI 标准事件名，**不要**再用自定义 `transcription.delta`/`transcription.completed`（保留可读性对应关系即可）。这样 sona 可直接复用 OpenAI 实时客户端语义。

---

## 5. 验收标准（可度量）

1. **流式 ASR**：一次发音内服务端连续下发 ≥3 个 `input_audio_transcription.delta`；结束后下发 1 次 `completed`，文本与最终一致。
2. **实时分人（R3）**：多说话人语音下，`segment` 事件能区分 ≥2 个 `speaker`（`spk_1`/`spk_2`），同说话人同一 `speaker`，且 `start`/`end` 单调不重叠；分人开关关闭时不下发 `speaker`（或全 `null`）且**不得伪造**标签。
3. **流式 TTS（R4）**：`response.create` 后首个 `response.output_audio.delta` 在 **TTFA ≤ 300ms** 内到达；音频 24kHz mono `s16le`，base64 可解码为合法 PCM。
4. **取消/打断（R5）**：`response.cancel` 后不再下发该 response 的音频 delta，并释放占用的合成资源。
5. **EOF（R6）**：`input_audio_buffer.commit` 后最终 `completed`/`segment` 完整；断线重连语义清晰（新建 session / source epoch）。
6. **事件有序性（R9）**：每事件带单调 `sequence`，客户端校验无乱序；缺失事件不静默降级到本地模型。
7. **健康/能力（R10）**：`/readyz` 在 ASR/TTS 后端就绪时返回 200；`/v1/models` 暴露当前 diarize 模型 id。

---

## 6. 非功能性需求

- **延迟预算**：ASR partial 首包 TTFT ≤ 260ms；TTS 首段 TTFA ≤ 300ms（对照 v2 实测基线）。
- **顺序与幂等**：`event_id` 全局唯一，`sequence` 单调；同 `item_id` 仅最新 revision 有效。
- **取消语义**：取消后丢弃未播放缓存并创建新 session；不允许半上下文残留。
- **鉴权**：`Authorization: Bearer <key>`，key 不落 URL；401/403 稳定错误码。
- **错误语义**：`backend_not_ready`、`diarization_not_available`、`invalid_state`、`invalid_event` 等稳定 `code`，**不得**回退到本地模型。
- **会话恢复**：Realtime 会话不可透明恢复 → 断线后新建 session 并声明 source epoch，客户端负责窗口对账。

---

## 7. 约束与不变量（sona 侧）

1. sona **不持久化音频** → 分人必须实时随转录事件下发，不能用会后批量兜底。
2. 不在语音链路中落入**本地模型回退**（ADR-0011/0012 既定）。
3. `diarization` 为可选增强；未配置相应 SpeechRail profile 时返 `diarization_not_available`，不得伪造 speaker label。

---

## 8. 交付里程碑建议

| 阶段 | 内容 | 验收 |
|---|---|---|
| M1 | `/v1/realtime` 事件命名对齐 OpenAI 标准（`conversation.item.input_audio_transcription.*`）| R1/R2/R6/R9 |
| M2 | 加入实时分人段事件（`segment` + `speaker`）| R3 |
| M3 | TTS/取消/EOF 全生命周期完善 | R4/R5 |
| M4 | 批量 `/v1/audio/transcriptions` 对齐 `diarized_json`/`gpt-4o-transcribe-diarize` | R7 |
| M5 | 端到端联调：sona 三路切到 `/v1/realtime`，确认并**弃用 v2** | 全部 |

---

## 9. 决策依赖

本交割单完成后，sona 将依据实施范围做如下决策（立项时已初步倾向）：
- **若 SpeechRail `/v1/realtime` 补齐到本单全量（含实时分人）** → sona **全量迁移到 OpenAI 标准 `/v1/realtime`，彻底弃用 `/v2/realtime`**；三路链路统一、移除 v2 适配器与 diarization 私有事件处理。
- **若 SpeechRail 无法在 `/v1/realtime` 提供实时分人，且 sona 必须保留会议实时分人** → 会议保留 `/v2/realtime`，其余走 `/v1/realtime`（混合，最小化 v2 暴露面）。

## 10. 交割结论（2026-09-02）

本单已完成。SpeechRail 已将 `/v1/realtime` 统一为唯一公共 Realtime 入口，并完成以下交付：

- 使用 OpenAI 标准 `conversation.item.input_audio_transcription.*` 事件承载 partial、segment 和 completed；
- 通过 `gpt-4o-transcribe-diarize` alias 或显式 `diarization` 配置启用匿名实时分人；无 profile 时 fail closed；
- 保留 24 kHz PCM16 流式 TTS、`response.cancel`、`input_audio_buffer.clear/commit` 和连接级终态；
- 在应用层集中管理 ASR/TTS/diarization 生命周期，HTTP route 只负责 WebSocket 传输边界；
- 直接移除 `/v2/realtime` 及其重复的 session、outbound、domain、route 和测试代码；
- `sona` 三路链路统一使用 `speechrail-openai-realtime` 与 `/v1/realtime`，不再提供 v2 fallback。

原“若无法提供实时分人则保留 v2”的备选路径不再适用；当前实现与 [SpeechRail OpenAI Realtime 契约](../../../SpeechRail/contracts/realtime-openai.md) 及 SpeechRail ADR-0009 对齐。
