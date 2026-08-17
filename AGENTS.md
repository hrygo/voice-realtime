# voice-realtime

全本地实时语音交互 + 实时语音字幕系统（Apple Silicon / MLX / 中文优先 / 离线）。
Python **3.12 严格锁定**（`misaki[zh]` 要求 <3.13）；uv + PEP 621 + hatchling。

方案文档：`docs/实时语音交互与字幕-方案与最佳实践.md`（2026-08-17 定稿，含 §7 实测验收回填）。

## 架构与数据流

```
WhisperLiveKit ──► 实时字幕 (Web UI + SRT + 说话人分离)        [subtitles]
   │ Qwen3-ASR streaming (MPS 原生)
   ▼
Pipecat ── FunASR(SenseVoice) ──► LM Studio 3.6-35B-A3B ──► [自研 TTS 桥] ──► mlx-audio Qwen3-TTS ──► 播放
   │                                                                           [interaction + tts_bridge]
   └─ WebSocket / localhost 单机
```

处理器链（`interaction/pipeline.py`）：`transport.input → VADProcessor(Silero) → FunASRSTTService → LLMUserAggregator → LmStudioNativeLLMService → LLMAssistantAggregator → OpenAITTSService(桥) → transport.output`。

## 模块地图

| 模块 | 职责 | 关键文件 |
|---|---|---|
| `voice_realtime.tts_bridge` | **唯一自研核心**：mlx-audio Qwen3-TTS → OpenAI 兼容 `POST /v1/audio/speech` 流式（wav/pcm） | `server.py`(FastAPI) `engine.py`(TTSEngine) `schema.py`(SpeechRequest) |
| `voice_realtime.interaction` | Pipecat 交互管道组装 + LM 服务 | `pipeline.py`(build_pipeline) `reasoning.py`(LmStudioNativeLLMService) `runner.py` |
| `voice_realtime.subtitles` | WhisperLiveKit 启动、WS 字幕事件桥、事件去重 | `launcher.py` `consumer.py` `events.py` |
| `voice_realtime.config` | 集中配置（pydantic-settings） | `config.py`（`InteractionSettings.stt_model` 等） |

`tools/` 下 WhisperLiveKit、mlx-audio 是 vendor 子仓库（仅启动/桥接），非自研。

## CRITICAL 实现约束（实测，写代码前必读，防回退）

1. **LM Studio 推理开关只能走原生端点**：OpenAI 兼容 `/v1/chat/completions` **忽略** `reasoning` 参数；唯一有效是**原生 `/api/v1/chat` + `reasoning:"off"`**（实测 `reasoning_output_tokens=0`）。
   - 原生 payload **无 `role`、无 `max_tokens`**（否则 "Unrecognized key(s)"）；流式 SSE 事件为 `message.delta`；非流式响应为 `output[].content`（**非** OpenAI `choices`）。
   - `LmStudioNativeLLMService`（`interaction/reasoning.py`）已封装；勿改回 `extra_body` 注入 OpenAI 端点的方案。
2. **SenseVoice 下载源**：pipecat `FunASRSTTService` 把 funasr `hub` 硬编码为 modelscope（`ms`），本环境被 **SSRF 拦截** → 任何 repo ID 必须经 `snapshot_download()` 落 `~/.cache/huggingface/hub` 后用**本地路径**加载（`pipeline._resolve_stt_model` + `InteractionSettings.stt_model`，空值自动解析 `FunAudioLLM/SenseVoiceSmall` 快照）。
3. **HTTP 测试**：`httpx.AsyncClient.stream()` 的请求体关键字参数是 **`json=`**（不是 `body=`）；测试 mock 必须同名，否则测试端 `KeyError: 'model'`。
4. **TTS 桥 422 排查**：`SpeechRequest.model` 是必填字段；422 先查 payload 字段完整性（`HealthResponse` 无此约束）。
5. **ruff 刻意忽略** `RUF001/002/003`（中文全角标点是项目风格）；`tests/*` per-file-ignore `S101/ANN001/ANN201`；mypy strict **仅 src/**（`exclude = ["tests/"]`）。

## 质量门禁（提交前必须全绿）

```bash
uv run pytest tests/            # 61 passed（asyncio_mode=auto，timeout=120，coverage fail_under=80）
uv run mypy src/                # strict；当前 14 files clean（tests 排除）
uv run ruff check src/ tests/   # clean（line-length=100，select 含 PTH/RET/PERF/SIM/ASYNC）
```

## 常用命令

```bash
uv sync --all-extras            # 或 --extra tts / --extra interaction / --extra dev
uv run vr-bridge                # TTS 桥（默认 8765，也可 scripts/run-bridge.sh）
uv run vr-interact              # 交互管道（scripts/run-interact.sh）
uv run vr-subtitles             # 字幕 lmk（scripts/run-subtitles.sh；wlk 8001）
uv run vr-subtitle-events       # 字幕事件消费者
scripts/download-models.sh      # SenseVoice 经 HF snapshot_download 落本地（modelscope 被拦截）
```

依赖组：`tts` = mlx-audio[tts]+misaki[zh]（重型，单独组）；`interaction` = pipecat-ai[funasr,silero,openai,soundfile,websocket,local]+torch/torchaudio（重型）；`dev` = pytest 系列 + ruff + mypy。新增依赖前确认锁 3.12。

## 环境与运行时依赖

- Apple Silicon M5 Max / macOS 26 / 128GB（本机实测）；别处按默认资源处理
- **LM Studio** `localhost:1234`，模型 `qwen/qwen3.6-35b-a3b`
- TTS 桥 8765：mlx-audio Qwen3-TTS（24 kHz WAV/PCM），VoiceDesign 音色 profile
- SenseVoice 缓存快照：`~/.cache/huggingface/hub/models--FunAudioLLM--SenseVoiceSmall/snapshots/…`
- 实测要点（文档 §7.1，QA 参考）：SenseVoice RTF≈0.17；推理关闭时 TTFT≈0.24–0.26s / ~97–113 tok/s

## 提交规范

- 中文描述式消息，`feat|fix|docs|chore|style` 前缀 + 冒号，如：`feat(interaction): local SenseVoice via HF snapshot`、`docs: 回填 §7.1/§7.2 实测验收数据`。
- 小步提交：门禁绿即可提交；bugfix 只修根因不顺手重构。