# ADR-003：LM Studio 长会话采用结构化记忆预热与原子换链

## 状态

Accepted

## 日期

2026-08-22

## 背景

ADR-002 通过 LM Studio 原生 `response_id` / `previous_response_id` 保住了真实的
system/user/assistant 角色，但服务端链会随对话持续增长。LM Studio 的上下文窗口足够大，不代表
实时语音可以接受不断增加的 prefill 与首字延迟；原生接口也没有可直接启用的语义压缩或滚动摘要。

Pipecat 1.7 的自动摘要只会重写本地 `LLMContext`，不会替换 LM Studio 已保存的 response chain；
其继承推理通道还会走本项目不能可靠关闭 Qwen reasoning 的 OpenAI 兼容端点。因此，单独启用
Pipecat 摘要会形成“本地历史已变短、模型侧历史仍增长”的双重事实源。

## 决策

- Pipecat 继续保存完整带角色历史，作为恢复与审计边界；不就地删改历史消息。
- 应用根据原生 `chat.end.result.stats.input_tokens`、TTFT、未压缩消息数和模型容量决定压缩。
- 后台以 `store: false`、`reasoning: "off"`、`temperature: 0` 和
  `max_output_tokens: 2048` 生成严格的 `ConversationMemorySnapshot`。
- 摘要请求携带完整 JSON Schema；模型输出必须通过禁止额外字段、来源轮次范围和角色映射校验，
  最多进行一次只含错误类别、不回显内容的格式纠正。
- 新链预热把结构化快照与最近十六组完整问答作为不受信历史数据发送，要求精确返回
  `MEMORY_READY`、零 reasoning tokens 和合法 `resp_` ID。
- 候选链只有在请求 generation、已完成用户轮数、旧 response ID 和服务生命周期全部未变化时
  原子替换；否则丢弃，当前链继续服务。
- 下一条真实用户指令始终作为预热链之后独立的原生 user turn，不进入记忆包。
- 旧 response ID 失效时，必须先用已验证的完整历史恢复种子链，再原样重试当前用户一次；恢复失败
  明确报错，禁止静默降级为空链。
- 默认 soft/hard/target 水位分别为 16384/32768/8192 tokens，保留十六组近期问答；短消息
  兜底提高到 128 条，连续 TTFT 触发提高到 3 秒。可用
  `VR_INTERACTION_CONTEXT_COMPACTION_ENABLED=false` 整体回滚。

## 备选方案

### 只依赖 LM Studio 原生长链

实现最简单，但模型侧输入和 TTFT 会持续增长，无法满足长时间实时语音的稳定延迟目标。

### 只启用 Pipecat 自动摘要

本地消息会缩短，但 LM Studio response chain 不变；同时摘要请求无法沿用已实测的原生
`reasoning: "off"` 通道，因此拒绝。

### 每轮重放摘要与完整近期 messages

OpenAI 兼容接口可以表达 roles，但当前模型在该端点无法可靠关闭 reasoning；原生端点又不接受
任意 assistant 历史输入。把角色标签压成普通文本还会扩大提示注入与角色混淆风险。

### 持久化长期记忆到数据库或向量库

这会引入数据生命周期、隐私、检索质量和迁移问题，超出当前单机会话内压缩范围。本决策不持久化
记忆，重启或清空上下文即丢弃。

## 后果

- 模型侧实际工作集能够收敛，同时保留角色、对象、事实更新和当次指令边界。
- 摘要与预热增加后台推理请求，但不阻塞当前轮回复；失败只保留旧链。
- LM Studio 会留下不再引用的本地 orphan response chains，其清理由 LM Studio 自身策略负责。
- 结构化 schema 与恢复路径成为兼容契约，变更时必须同步更新测试和本 ADR 的后继决策。
- 2026-08-22 本机 100-turn 验收中，输入从 4218 降至 1143 tokens；后台预热 TTFT 1.314 秒，
  后续八次用户探针最大 TTFT 0.507 秒；十次原生响应 reasoning tokens 全为零，20/20 检查通过。

## 参考

- `docs/decisions/0002-lm-studio-stateful-chat-context.md`
- `docs/superpowers/specs/2026-08-21-lm-studio-context-compaction-design.md`
- https://lmstudio.ai/docs/developer/rest/stateful-chats
- https://docs.pipecat.ai/pipecat/fundamentals/context-summarization
