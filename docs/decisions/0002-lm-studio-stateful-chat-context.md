---
title: "ADR-002：LM Studio 交互上下文采用原生有状态会话链"
description: "废弃 OpenAI 兼容端点，改用 LM Studio 原生 /api/v1/chat + reasoning: 'off' 及 previous_response_id 保持真实角色链"
status: accepted
type: decision_record
category: interaction
date: 2026-08-21
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "sona-interaction"
tags:
  - adr
  - lm-studio
  - stateful-context
  - native-api
  - reasoning-off
scope:
  - "sona.interaction"
related_documents:
  - "docs/architecture/系统总体架构与详细设计方案.md"
  - "docs/decisions/0003-lm-studio-context-compaction.md"
---

# ADR-002：LM Studio 交互上下文采用原生有状态会话链

## 状态

Accepted

## 日期

2026-08-21

## 背景

交互管道在 Pipecat `LLMContext` 中正确保存 `system`、`user`、`assistant` 消息，但现有
`LmStudioNativeLLMService` 在调用 `/api/v1/chat` 前会丢弃角色，把全部历史转换为无角色的
text items。该形状无法可靠表达系统指令、历史发言者和本轮用户指令。

LM Studio 0.4 原生 `/api/v1/chat` 的官方契约是：`system_prompt` 表示系统指令，`input` 表示
本轮用户消息，服务端通过 `response_id` / `previous_response_id` 保存和延续真实对话角色；该端点
不支持在请求中直接附带 assistant 历史。项目又必须使用该原生端点的 `reasoning: "off"`，因为
本机实测 OpenAI 兼容 `/v1/chat/completions` 会忽略这一开关。

## 决策

- 继续使用 LM Studio 原生 `POST /api/v1/chat`。
- 首轮请求发送 `system_prompt`、当前 user `input`、`store: true` 和 `reasoning: "off"`。
- 成功流必须以 `chat.end` 收尾；从 `chat.end.result.response_id` 提交会话状态。
- 后续请求只发送当前 user `input`，并用 `previous_response_id` 连接服务端角色历史。
- Pipecat 继续保存完整、带角色的本地历史，但不得再次把它压平成原生 `input`。
- `clear_context`、persona 变化、系统提示变化或上下文回退必须开启新的原生会话链。
- 中断、取消、显式错误、缺少 `chat.end` 或缺少有效 `response_id` 时不得推进会话链。
- 通过请求代次防止迟到的旧响应覆盖较新的会话状态。

## 备选方案

### OpenAI 兼容 `/v1/chat/completions`

能够直接发送完整 `messages` 角色历史，但本机验证该端点忽略 Qwen 的 `reasoning` 开关，会恢复
长思考、增加首字延迟，并可能在输出预算耗尽时返回空正文。

### OpenAI 兼容 `/v1/responses`

支持 assistant 输入和有状态续接，但当前官方文档只明确为特定模型提供 reasoning effort 控制，
没有证明本项目 Qwen 模型可稳定关闭 thinking，不能替代已经实测的原生开关通道。

### 在文本中手工添加角色标签

可以把历史序列化为单条 user input，但角色只是普通文本，不会进入模型 chat template 的受信角色
边界；同时扩大提示注入和角色混淆风险，因此拒绝。

## 后果

- 模型侧能准确区分系统指令、历史 user/assistant 轮次和当前用户指令。
- 每轮请求不再重复传输完整历史，降低长会话 prefill 成本。
- 交互会话依赖 LM Studio 本机保存的 response chain；上下文重置必须同步重置链 ID。
- 若服务端链 ID 失效，不能伪称历史仍完整；实现必须明确报告断链，并按定义的恢复策略处理当前轮。
- 流式适配器必须同时处理增量正文和最终 `chat.end`，不能只消费 `message.delta`。

## 参考

- https://lmstudio.ai/docs/developer/rest/chat
- https://lmstudio.ai/docs/developer/rest/stateful-chats
- https://lmstudio.ai/docs/developer/rest/streaming-events
- https://lmstudio.ai/docs/developer/rest
