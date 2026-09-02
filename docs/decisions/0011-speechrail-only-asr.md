---
title: "ADR-0011：仅使用 SpeechRail 作为 ASR 运行时"
description: "统一字幕、会议与语音助手的 ASR 运行时边界，移除本地 ASR worker 与隐式回退"
status: accepted
type: decision_record
category: architecture
date: 2026-08-31
last_updated: 2026-09-01
author: "Voice Realtime Core Team"
owners:
  - "sona-core"
tags:
  - adr
  - speechrail
  - asr
  - realtime-v2
scope:
  - "sona.asr"
  - "sona.meeting"
  - "sona.interaction"
  - "sona.ui"
related_documents:
  - "docs/architecture/系统总体架构与详细设计方案.md"
  - "docs/manuals/SpeechRail-Realtime-v2-语音转文字开发对接手册.md"
  - "docs/decisions/0012-speechrail-realtime-tts.md"
---

# ADR-0011：仅使用 SpeechRail 作为 ASR 运行时

## Status

Accepted — 2026-08-31

## Context

`sona` 过去同时包含 WhisperLiveKit、SenseVoice、Qwen3 本地 worker 和 Fun-ASR
实验 adapter。这些实现有不同的事件语义、模型生命周期和配置字段，导致字幕、会议与语音助手
可能在同一主机上加载多个 ASR 模型，也使故障处理隐含地退回到另一条路径。

SpeechRail 已提供本机 loopback 的 OpenAI-compatible REST 与 `/v1/realtime` ASR 服务，并由其
独占 Qwen3-ASR 模型 lifecycle。应用需要保持 AudioHub、会议、TTS、UI 和 PostgreSQL 的既有所有权。

## Decision

`sona` 的语音助手、实时字幕和会议转录只使用
`ws://127.0.0.1:8201/v1/realtime`：

- 删除旧 ASR adapter、profile、registry、启动器、模型下载步骤和 benchmark CLI；
- 删除 `SONA_SUBTITLE_BACKEND`、`SONA_INTERACTION_STT_BACKEND` 及旧模型/sidecar 配置；
- `SubtitleProxy` 和 Pipecat pipeline 都直接构造 SpeechRail adapter；
- `/api/services` 将 ASR 健康项命名为 `speechrail`，探测 `/health`；
- 连接失败、最终结果缺失或协议错误直接向调用方暴露，不做模型或服务自动回退。

历史设计、评测和 ADR 仍保留为不可变证据；它们不描述当前部署方式。

## 当前实现补充（2026-09-01）

- `SpeechRailStreamingTranscriber` 已支持会议 diarization：请求 `diarization`、`speaker_count_hint`
  与会议作用域 `group_id`，接收匿名 speaker segments 和 commit 后的 `transcription.diarization.completed` mapping。
- `MeetingSession` 将 mapping 作为原子 remap 应用，并由 `DiarizationSmoother` 处理短片段/时序平滑；
  本仓库不再运行本地 CAM++、AHC 或 voiceprint worker。
- 语音助手使用 `SpeechRailConversationSTTProcessor`；字幕和会议使用 `SpeechRailStreamingTranscriber`。
- SpeechRail `/v1/realtime` 会话不可透明恢复，应用重连时创建新 session/source epoch，并在窗口层记录 gap/对账边界。

## Consequences

- ASR 进程与模型仅由 SpeechRail 管理；启动 `sona` 前必须确认 SpeechRail ready。
- 应用无需安装、下载或启动 ASR 模型，减少重复占用内存与网络行为。
- 当前 SpeechRail adapter 已支持会议所需的匿名多说话人标签与最终 mapping；身份仍是会议内匿名
  group，不等同于跨会议真实声纹识别。
- 这是破坏性配置变更：旧 ASR 环境变量与 `vr-subtitles` / `vr-asr-benchmark` 命令不再存在。
