---
title: "ADR-009：LM Studio 原生协议与本地推理准入采用跨业务公共层"
description: "统一助手、会议纪要和内心 OS 的原生 SSE 语义、配置所有权与单槽优先级调度"
status: accepted
type: decision_record
category: architecture
date: 2026-08-27
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "sona-core"
tags:
  - adr
  - lm-studio
  - local-inference
  - workload-scheduler
scope:
  - "sona.lm_studio"
  - "sona.inference"
  - "sona.interaction"
  - "sona.meeting"
related_documents:
  - "docs/decisions/0002-lm-studio-stateful-chat-context.md"
  - "docs/decisions/0005-server-side-runtime-workload-arbitration.md"
  - "docs/decisions/0007-bounded-meeting-summary-generation.md"
  - "docs/superpowers/plans/2026-08-27-meeting-inner-os-p0-p1.md"
---

# ADR-009：LM Studio 原生协议与本地推理准入采用跨业务公共层

## 状态

Accepted

## 日期

2026-08-27

## 背景

语音助手、会议纪要、Inner OS 和本地评测都调用同一个 LM Studio 实例，但此前只有 HTTP 传输和
鉴权 helper 共用。各业务分别解析 `message.delta`、`chat.end`、error、stats 和输出字符上限，导致
终态语义漂移；Inner OS 曾因错误等待不存在的终态而一直显示“等待模型算力”。同时，进程内单槽 gate
只被 Inner OS 使用，后台纪要仍可绕过仲裁，与本机 LM Studio `parallel=1` 的事实不一致。

## 决策

1. `sona.lm_studio` 是 LM Studio 原生协议唯一实现：`NativeChatRequest` 控制可选参数，
   `stream_chat()` 唯一负责 SSE 解码，`complete_chat()` 统一正文、终态、stats、error 和字符熔断。
2. 业务层不再读取原始 SSE 行。助手直接消费规范化事件以保持低延迟 TTS 和 response chain；纪要、
   Inner OS 和评测使用完成结果 API。
3. prompt、领域 schema、证据映射、repair 和业务错误码仍属于各业务适配器。公共层不解释会议事实，
   不引入通用 prompt 或通用 JSON 业务模型。
4. `LocalInferenceScheduler` 是共享 LM Studio 的进程级单槽准入器。优先级固定为实时助手、Inner OS、
   维护任务、后台纪要；优先级只影响尚未入场的任务，已入场模型调用不抢占。
5. 录音期间暂停新的后台纪要 claim 和模型入场。已进入 LM Studio 的纪要允许完成；等待中的纪要可
   取消并重新排队，防止把会议时长计入纪要总 deadline。
6. `LMStudioSettings` 持有公共 endpoint 与 API key。旧的
   `SONA_INTERACTION_LLM_BASE_URL/SONA_INTERACTION_LLM_API_KEY` 保留兼容别名，配置诊断继续脱敏。
7. Inner OS 拆为查询编排、模型策略和私有连接会话。WebSocket 命令使用严格判别联合校验，模型输出
   必须先验证 schema 与证据别名，不能直接进入数据库、HTML、shell 或日志。

## 备选方案

### 各业务继续使用原始 `stream_request()`

改动最小，但会继续复制终态、错误和输出上限语义，无法从结构上阻止同类回归，因此拒绝。

### 把所有业务统一成一个通用 LLM Service

会混淆助手会话链、纪要 map/reduce 和 Inner OS 证据问答的不同契约，形成新的 God Service。公共层只
统一协议和资源准入，因此拒绝。

### 运行中按优先级强制抢占

会破坏原生响应链、浪费已生成 token，并增加纪要恢复复杂度。当前采用非抢占式准入；只有尚未入场的
后台任务在录音切换时重排队。

## 后果

### 正向后果

- 原生 SSE 语义只有一个实现，各业务不再独立猜测终态；
- 本机单并发模型有明确优先级和暂停语义；
- Inner OS 查询状态机不再持有 prompt、HTTP 和 JSON 修复职责；
- 旧环境变量和现有 REST/WS 事件保持兼容。

### 负向后果

- 调度器是单进程状态；多进程部署需要外部租约或继续限制为单 worker；
- 非抢占意味着已入场的长纪要调用仍可能让交互任务短暂等待；
- 原生事件发生破坏性变化时所有消费者会同时受影响，因此协议夹具和实机冒烟是发布门禁。

## 实施约束

- 不得把业务 prompt、原始模型输出、API key 或会议原文写入公共日志；
- 助手 response chain 只在合法 `chat.end.result.response_id` 后提交；
- 纪要保留 map/reduce token 上限、字符熔断和一次 repair；
- Inner OS 只允许当前会议 confirmed 快照和证据别名，未知引用必须拒绝；
- 新增模型工作负载必须显式选择 `WorkloadKind`，不得绕过共享调度器。
