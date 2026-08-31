# ADR-0011：仅使用 SpeechRail 作为 ASR 运行时

## Status

Accepted — 2026-08-31

## Context

`voice-realtime` 过去同时包含 WhisperLiveKit、SenseVoice、Qwen3 本地 worker 和 Fun-ASR
实验 adapter。这些实现有不同的事件语义、模型生命周期和配置字段，导致字幕、会议与语音助手
可能在同一主机上加载多个 ASR 模型，也使故障处理隐含地退回到另一条路径。

SpeechRail 已提供本机 loopback 的 OpenAI-compatible REST 与 Realtime v2 ASR 服务，并由其
独占 Qwen3-ASR 模型 lifecycle。应用需要保持 AudioHub、会议、TTS、UI 和 PostgreSQL 的既有所有权。

## Decision

`voice-realtime` 的语音助手、实时字幕和会议转录只使用
`ws://127.0.0.1:8201/v2/realtime`：

- 删除旧 ASR adapter、profile、registry、启动器、模型下载步骤和 benchmark CLI；
- 删除 `VR_SUBTITLE_BACKEND`、`VR_INTERACTION_STT_BACKEND` 及旧模型/sidecar 配置；
- `SubtitleProxy` 和 Pipecat pipeline 都直接构造 SpeechRail adapter；
- `/api/services` 将 ASR 健康项命名为 `speechrail`，探测 `/health`；
- 连接失败、最终结果缺失或协议错误直接向调用方暴露，不做模型或服务自动回退。

历史设计、评测和 ADR 仍保留为不可变证据；它们不描述当前部署方式。

## Consequences

- ASR 进程与模型仅由 SpeechRail 管理；启动 `voice-realtime` 前必须确认 SpeechRail ready。
- 应用无需安装、下载或启动 ASR 模型，减少重复占用内存与网络行为。
- 当前 SpeechRail adapter 标识单一 speaker；多说话人标签只能在 SpeechRail 提供相应契约后以
  新的显式能力版本加入，不能回退到旧 adapter。
- 这是破坏性配置变更：旧 ASR 环境变量与 `vr-subtitles` / `vr-asr-benchmark` 命令不再存在。
