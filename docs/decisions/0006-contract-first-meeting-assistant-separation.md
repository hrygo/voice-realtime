---
title: "ADR-006：会议助手契约优先前后端分离"
description: "在单仓架构下以 contracts/ 目录为唯一事实源，分离后端领域生产与前端派生消费"
status: accepted
type: decision_record
category: meeting
date: 2026-08-26
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-core"
tags:
  - adr
  - meeting-assistant
  - contract-first
  - separation-of-concerns
  - openapi
  - asyncapi
scope:
  - "voice_realtime.meeting"
  - "ui"
  - "contracts"
related_documents:
  - "docs/operations/会议助手前后端分离式开发准备方案.md"
  - "docs/operations/会议助手前后端分离工作交接清单.md"
contracts:
  - "contracts/meeting-assistant/v1/"
---

# ADR-006：以契约优先支持会议助手前后端团队分离

## 状态

Accepted

## 日期

2026-08-26

## 背景

会议助手后续将由前端团队和后端团队分别开发。当前仓库已经包含 React/TypeScript 前端、Python 后端、PostgreSQL、WhisperLiveKit、Sortformer 和会议 WebSocket，但公共契约、实现代码和测试仍然在同一工作树中演进。

若直接拆分代码仓库，以下问题会被放大：

- HTTP canonical path 与运行手册存在差异；
- 代码和测试已经使用 `start_subtitles`，AsyncAPI 枚举可能未同步；
- 事件 envelope 的 payload 约束过宽，消费者无法独立校验；
- `meeting_snapshot`、revision、窗口替换和重同步规则依赖实现上下文；
- 前端可能逐渐依赖数据库字段、Python 类型或 `speaker_key` 内部格式。

## 决策

采用“契约优先、单仓库先行、服务端权威”的分离策略：

1. `contracts/meeting-assistant/v1/` 是前后端公共接口唯一事实源。
2. REST 以 OpenAPI 描述，WebSocket 以 AsyncAPI 描述，具体资源和事件 payload 以 JSON Schema 描述，并用 fixtures 固化可运行样例。
3. 后端拥有会议领域状态、数据库事实、ASR/分人、窗口对账、事件顺序、错误码和协议生产。
4. 前端只依赖公共契约，负责协议消费、状态派生、阅读视图、交互和可访问性。
5. 前端不访问数据库、不导入后端实现、不解析 `speaker_key`，阅读层的合并不覆盖事实层片段。
6. 当前先在单仓库按目录 ownership 和 contract CI 隔离；契约稳定后再发布独立 artifact，最后再评估拆仓。
7. 同一 major version 内只允许向后兼容的 additive 变更；破坏性变更进入新 major version。

## 备选方案

### 立即拆成两个代码仓库

隔离边界清晰，但会在契约尚未稳定时引入跨仓库版本同步、artifact 分发、联调环境和回滚协调成本。拒绝作为第一步。

### 共享后端生成的 TypeScript 内部类型

可以减少初始重复，但会让前端绑定后端模块结构、生成工具和内部字段，无法真正形成稳定公共接口。拒绝作为公共边界。

### 只维护一份手写 Markdown 接口说明

可读性好，但无法对 producer/consumer 做机器校验，容易出现“文档正确、事件实际不正确”。拒绝作为唯一契约形式；Markdown 只作为导航和语义说明。

## 后果

### 正向后果

- 前后端可以使用 fixtures 和 mock 并行开发；
- 服务端状态、revision 和恢复语义有明确所有权；
- 接口变更可判断 additive 或 breaking，降低隐式耦合；
- 测试可以分别验证 producer、consumer 和集成行为；
- 未来拆仓时只需迁移契约 artifact 和 CI，不必重新发现协议边界。

### 负向后果

- 需要维护 OpenAPI、AsyncAPI、JSON Schema、fixtures 和变更日志；
- 事件 payload schema 和兼容测试会增加短期工作量；
- 两团队需要共同 review 公共契约；
- 旧兼容路径和新 `/ws/v1/*` 路径需要明确废弃周期。

### 保留风险

- 实时 ASR 的 partial/revision 行为仍受 WLK、模型和机器负载影响；
- 说话人分离仍是匿名 diarization，不能保证真实身份；
- 外部高负载可能影响延迟，但不改变前后端契约；
- 如果双方绕过公共契约直接共享内部对象，单仓库仍会重新形成隐式耦合。

## 实施约束

- `meeting_id`、`speaker_key`、`source_epoch`、`transcript_revision`、`content_revision` 和 `runtime_revision` 的语义必须写入公共契约；
- `event_id` 只用于去重，不能代替 revision 排序；
- `meeting_snapshot` 是重连基线；`resync_required` 必须触发 HTTP 回源；
- 会议 ID 变化时前端必须清理旧派生状态；
- PostgreSQL 仍是会议唯一事实源，不保存音频；
- 合同错误必须使用稳定机器错误码，不能让前端依赖人类可读 message；
- 前后端整体发布和回滚必须记录各自 commit SHA 与 contract version。

## 关联文档

- [`docs/operations/会议助手前后端分离式开发准备方案.md`](../operations/会议助手前后端分离式开发准备方案.md)
- [`contracts/meeting-assistant/v1/openapi.json`](../../contracts/meeting-assistant/v1/openapi.json)
- [`contracts/meeting-assistant/v1/asyncapi.yaml`](../../contracts/meeting-assistant/v1/asyncapi.yaml)
