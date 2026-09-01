---
title: "ADR-001：交互管道采用单一所有者运行时"
description: "确定 vr-ui 为交互管道唯一所有者，vr-interact 为互斥无UI替代入口，统一 InteractionSession 生命周期"
status: accepted
type: decision_record
category: interaction
date: 2026-08-20
last_updated: 2026-09-01
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-core"
tags:
  - adr
  - architecture
  - interaction
  - single-owner
  - lock
scope:
  - "voice_realtime.interaction"
  - "voice_realtime.ui"
related_documents:
  - "docs/architecture/系统总体架构与详细设计方案.md"
  - "docs/decisions/0005-server-side-runtime-workload-arbitration.md"
---

# ADR-001：交互管道采用单一所有者运行时

## 状态

Accepted

## 日期

2026-08-20

## 背景

Voice Studio 已从三个独立进程演化为 `vr-ui` 内嵌 AudioHub、Pipecat 交互管道与状态观察器，
但 `vr-interact` 仍可独立启动同一条 STT→LLM→TTS 管道。旧文档甚至要求两者同时运行，
可能造成双麦克风采集、重复推理、重复播报和资源竞争。

同时，UI 运行时与 headless 入口分别管理会话超时、NLTK 自检、WorkerRunner 生命周期和停止语义，
形成重复编排与行为漂移。

## 决策

采用单一所有者模型：

- `vr-ui` 是带 UI 场景下交互管道的唯一所有者。
- `vr-interact` 作为无 UI 的 headless 替代入口保留，但与 `vr-ui` 互斥。
- 两个入口复用同一个 `InteractionSession` 应用服务；入口只负责装配与进程生命周期。
- `InteractionSession` 统一负责 NLTK 自检、Pipeline/Worker/Runner 创建、会话超时、优雅停止、
  队列清理、persona/duplex 状态重放和资源关闭。
- 通过本机进程级所有权锁阻止两个入口同时持有交互管道。锁失败必须明确报错，不能静默降级。
- `vr-ui` 停止交互会话时保留 AudioHub 与字幕链，但进入交互队列的音频必须暂停或丢弃；
  重新启动前清空队列，禁止回放停止期间的历史音频。

## 备选方案

### UI 仅作为外部 `vr-interact` 的控制器

优点是进程职责直观。缺点是需要跨进程控制、状态同步和音频共享协议，当前代码没有这些基础设施，
会显著扩大实现与故障面。

### 通过配置长期支持内嵌与外置两套 UI 模式

灵活但会保留两套运行拓扑、两套故障恢复和更多组合测试，违反本项目当前单机本地场景的 YAGNI 与
DRY 原则。

## 后果

- UI 和 headless 的会话行为一致，修复只需落在一个应用服务。
- 原有 `vr-interact` 命令仍可使用，但不能与 `vr-ui` 并行运行。
- 启动文档、架构图、健康状态和测试应描述当前四类运行单元：`vr-ui`、SpeechRail、LM Studio
  与 PostgreSQL；不再把 `vr-subtitles` 或 `vr-bridge` 当作独立应用进程。
- 运行时重构会改变内部模块边界，但不改变现有 CLI 命令名和公开 HTTP 路径。
