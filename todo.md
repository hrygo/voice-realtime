# 架构整治验收清单（当前基线：2026-09-01）

已批准并实施“方案 A：`sona-ui` 单一交互所有者，`sona-interact` 为互斥替代入口”；ASR/TTS 已统一迁移到
独立 SpeechRail Realtime v2，SpeechRail worker/profile 的部署验收另行记录。

## 已完成

- [x] `InteractionOwnership` 跨进程锁，阻止 UI/headless 双开。
- [x] `InteractionSession` 统一启动、超时、优雅停止、兜底取消和重启。
- [x] AudioHub 打开失败上抛、每 sink 有界队列、慢消费者丢最旧帧、真实静音。
- [x] SpeechRail Realtime v2 ASR 接线、离线 fail-fast、全量快照、去重和 source epoch 重连对账。
- [x] confirmed 字幕原子写 `current.srt` 并在停止时归档。
- [x] EchoState 单写者、文本最短长度/常用应答保护、耳机重锁阈值修复。
- [x] 助手每轮指标重置，TTS TTFB 以首个真实音频帧为准，缺失阶段为 null。
- [x] SpeechRail TTS 请求级 preset、单并发生成、有界队列、response cancel 和 PCM 流式输出。
- [x] LM Studio 客户端超时、SSE 校验和显式关闭；保留原生端点约束。
- [x] 控制协议严格 schema、`request_id` 确认、完整状态握手和稳定错误码。
- [x] 前端断线重连、服务端权威设置、真实静音、状态机和会话计时。
- [x] loopback 配置限制、WebSocket Origin 校验和 HTTP 安全响应头。
- [x] Python 3.12 元数据统一，默认 pytest 覆盖率门禁启用。
- [x] Python 与前端自动化测试覆盖上述行为。

## 验收命令

```bash
uv run pytest tests/
uv run mypy src/
uv run ruff check src/ tests/
cd ui && npm test -- --run
cd ui && npm run build
cd ui && npm audit --audit-level=high
```

运行时真机验收记录与仍受外部进程状态影响的项目，统一记录在架构整治设计文档的验收章节，
不再把已完成的 M1–M4 保留为“未完成任务”。
