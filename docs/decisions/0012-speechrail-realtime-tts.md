---
title: "ADR-0012：TTS 运行时统一由 SpeechRail Realtime v2 提供"
description: "移除旧 TTS bridge 与本地 TTS 运行时，统一 SpeechRail TTS session、播放取消和回声状态边界"
status: accepted
type: decision_record
category: architecture
date: 2026-09-01
last_updated: 2026-09-01
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-core"
tags:
  - adr
  - speechrail
  - tts
  - realtime-v2
  - pipecat
scope:
  - "voice_realtime.interaction"
  - "voice_realtime.speechrail"
  - "voice_realtime.config"
  - "voice_realtime.ui"
related_documents:
  - "docs/architecture/系统总体架构与详细设计方案.md"
  - "docs/manuals/SpeechRail-Realtime-v2-语音转文字开发对接手册.md"
  - "docs/decisions/0011-speechrail-only-asr.md"
---

# ADR-0012：TTS 运行时统一由 SpeechRail Realtime v2 提供

## 状态

Accepted — 2026-09-01

## 背景

旧实现由 `voice-realtime` 维护本地/bridge TTS 进程、模型生命周期和 OpenAI 兼容转换，造成
ASR/TTS 服务边界不一致、取消与播放状态分散，以及重复的模型资源占用。SpeechRail 已提供
统一的本地 TTS Realtime v2 session，应用不应继续保留第二套运行时。

## 决策

- 交互管道统一使用 `SpeechRailTTSService` → `SpeechRailTTSClient`，通过 SpeechRail Realtime v2
  `speech` session 请求公开模型 `speechrail/qwen3-tts` 和 preset `default`/`warm`/`bright`/`calm`。
- SpeechRail 返回有序的 24kHz mono PCM `response.audio.delta`；应用负责把音频映射为 Pipecat frame、
  播放、打断、取消和 `EchoState`，SpeechRail 不负责扬声器或业务状态。
- REST `/v1` 仅用于音色目录与试听/回放代理；实时交互使用 `/v2/realtime`，不回退到旧 bridge。
- `scripts/run-all.sh` 只启动 `vr-ui`；SpeechRail 独立管理模型、worker、队列和健康状态。
- `tts_bridge_url` 等兼容配置仅保留至 2026-10-31 的迁移窗口，生产管道不得使用。

## 后果

### 正向后果

- 本仓库不安装、下载或启动本地 TTS 模型，ASR/TTS 模型生命周期归属一致。
- Realtime v2 的 response ID、chunk index 和 cancel 语义集中在一个客户端适配层，便于顺序校验和资源回收。
- TTS 播放状态仍由应用掌握，可与单一 PCM owner 及双层回声防线协调。

### 保留风险

- SpeechRail 未就绪或 profile 缺失时，语音助手必须明确失败；不能静默降级到旧 bridge。
- Realtime v2 session 不可透明恢复；取消或断线后应用需丢弃未播放缓存并创建新 session。
- 本 ADR 的代码适配已完成，SpeechRail 实际 worker、模型 snapshot/profile 和真实音频闭环仍需独立 smoke/e2e 验收。
