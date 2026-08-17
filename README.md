# voice-realtime

全本地实时语音交互 + 实时语音字幕系统（Apple Silicon / MLX / 中文优先 / 离线）。

> 方案与最佳实践见 [`docs/实时语音交互与字幕-方案与最佳实践.md`](docs/实时语音交互与字幕-方案与最佳实践.md)（2026-08-17 定稿）。

## 架构

```
WhisperLiveKit ──► 实时字幕 (Web UI + SRT + 说话人分离)
   │ Qwen3-ASR streaming (MPS 原生)
   ▼
Pipecat ── FunASR(SenseVoice) ──► LM Studio 35B-A3B ──► [qwen3-tts-openai 桥] ──► Qwen3-TTS --stream ──► 播放
   │
   └─ WebSocket / localhost (单机、无外网依赖)
```

## 模块

| 模块 | 说明 |
|---|---|
| `voice_realtime.tts_bridge` | **qwen3-tts-openai 桥**：mlx-audio Qwen3-TTS → OpenAI 兼容 `POST /v1/audio/speech` 流式端点（唯一自研核心） |
| `voice_realtime.interaction` | Pipecat 交互管道组装：FunASR STT → LM Studio → TTS 桥 → 播放 |
| `voice_realtime.subtitles` | WhisperLiveKit 字幕服务启动与事件桥接 |
| `voice_realtime.config` | 集中配置（pydantic-settings） |

## 快速开始

```bash
uv sync --all-extras
# 1. 启动 TTS 桥
uv run vr-bridge
# 2. 启动字幕服务（WhisperLiveKit，需先 tools/ 安装）
# 3. 启动交互管道
```

## 质量门禁

```bash
uv run ruff check . && uv run mypy src && uv run pytest
```

## 环境要求

- Apple Silicon (M5 Max) / macOS 26 / 128GB
- LM Studio 运行于 `localhost:1234`（`qwen/qwen3.6-35b-a3b`）
- 语音模型经 HuggingFace 下载（mlx-community Qwen3-TTS / SenseVoice）