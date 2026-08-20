# voice-realtime

全本地实时语音交互 + 实时语音字幕系统（Apple Silicon / MLX / 中文优先 / 离线）。
Python **3.12 严格锁定**（`misaki[zh]` 要求 <3.13）；uv + PEP 621 + hatchling。

方案文档：`docs/实时语音交互与字幕-方案与最佳实践.md`（2026-08-17 定稿，含 §7 实测验收回填）。
架构图与端到端流程图：`docs/架构图与流程图.md`（2026-08-18；图 1 高阶架构 / 图 2 模块架构 / 图 3 交互链路时序，①–⑧ / 图 4 字幕链路时序，①–⑤；均附编号图注，Mermaid 渲染）。

## 架构与数据流

```
麦克风 ──► vr-ui / AudioHub ──► AudioInjector / Pipecat ──► LM Studio ──► TTS 桥 ──► 播放
                   └──────────► SubtitleProxy ──PCM WS──► vr-subtitles / WhisperLiveKit
```

默认由 `vr-ui` 独占交互会话；`vr-interact` 是 headless 互斥替代入口，禁止同时运行。
处理器链（`interaction/pipeline.py`）：`AudioInjector/transport.input → EchoSuppressionProcessor → FunASRSTTService → SelfEchoFilter → LLMUserAggregator(内含 SileroVADAnalyzer) → LmStudioNativeLLMService → BotTextRecorder → OpenAITTSService(桥) → TTSStateObserver → transport.output → LLMAssistantAggregator`。

## 模块地图

| 模块 | 职责 | 关键文件 |
|---|---|---|
| `voice_realtime.tts_bridge` | mlx-audio Qwen3-TTS → OpenAI 兼容 `POST /v1/audio/speech`，请求级音色、有界串行生成 | `server.py` `engine.py` `schema.py` |
| `voice_realtime.interaction` | 共享会话/所有权 + Pipecat 管道 + LM 服务 + 双层回声防线 | `session.py` `ownership.py` `pipeline.py` `reasoning.py` `runner.py` |
| `voice_realtime.subtitles` | WhisperLiveKit 启动、WS 字幕事件桥、事件去重 | `launcher.py` `consumer.py` `events.py` |
| `voice_realtime.audio` | 单源麦克风、有界 sink 扇出、真实静音、Pipecat 音频注入 | `hub.py` `audio_injector.py` |
| `voice_realtime.ui` | 默认运行时、字幕代理、状态观测、严格控制协议与浏览器安全边界 | `runtime.py` `subtitle_proxy.py` `assistant_bridge.py` `control.py` `protocol.py` |
| `voice_realtime.config` | 集中配置（pydantic-settings） | `config.py`（`InteractionSettings.stt_model` 等） |

`tools/` 下 WhisperLiveKit、mlx-audio 是 vendor 子仓库（仅启动/桥接），非自研。

## CRITICAL 实现约束（实测，写代码前必读，防回退）

1. **LM Studio 推理开关只能走原生端点**：OpenAI 兼容 `/v1/chat/completions` **忽略** `reasoning` 参数；唯一有效是**原生 `/api/v1/chat` + `reasoning:"off"`**（实测 `reasoning_output_tokens=0`）。
   - 原生 payload **无 `role`、无 `max_tokens`**（否则 "Unrecognized key(s)"）；流式 SSE 事件为 `message.delta`；非流式响应为 `output[].content`（**非** OpenAI `choices`）。
   - `LmStudioNativeLLMService`（`interaction/reasoning.py`）已封装；勿改回 `extra_body` 注入 OpenAI 端点的方案。
2. **SenseVoice 下载源**：pipecat `FunASRSTTService` 把 funasr `hub` 硬编码为 modelscope（`ms`），本环境被 **SSRF 拦截** → 任何 repo ID 必须经 `snapshot_download()` 落 `~/.cache/huggingface/hub` 后用**本地路径**加载（`pipeline._resolve_stt_model` + `InteractionSettings.stt_model`，空值自动解析 `FunAudioLLM/SenseVoiceSmall` 快照）。默认 `allow_model_downloads=False` 使用 `local_files_only=True`，只有显式授权才联网。
3. **HTTP 测试**：`httpx.AsyncClient.stream()` 的请求体关键字参数是 **`json=`**（不是 `body=`）；测试 mock 必须同名，否则测试端 `KeyError: 'model'`。
4. **TTS 桥 422 排查**：`SpeechRequest.model` 是必填字段；422 先查 payload 字段完整性（`HealthResponse` 无此约束）。
5. **ruff 刻意忽略** `RUF001/002/003`（中文全角标点是项目风格）；`tests/*` per-file-ignore `S101/ANN001/ANN201`；mypy strict **仅 src/**（`exclude = ["tests/"]`）。
6. **回声死循环两道防线勿删**：单机同麦同箱必须保留（`pipeline.py`）。
   - L1 `EchoSuppressionProcessor`：TTS 播报**全程**丢弃输入帧，仅当输入 RMS 超过回声基线（滑动中位数）× `echo_barge_in_gain`(默认 2.5) 连续 `echo_barge_in_frames`(默认 3) 帧（真人插话能量明显更高）才放行；删除会回归"机器人一开口就打断自己 / 长播报尾部回声自触发"。
   - L2 `BotTextRecorder` + `SelfEchoFilter`（共享 `EchoTextBuffer`）：用户转写与近端（`echo_text_window_secs` 默认 10s）机器人播报文本相似度 ≥ `echo_text_similarity`(0.7) 或为其子串 → 吞帧不进 LLM 上下文，机器人永不响应自己的话，内容层死循环必断。
   - 端点参数联动：`silence_secs`(0.45) 须略小于 STT `ttfs_p99_latency`(0.5)，保留转写等待窗口。

## 质量门禁（提交前必须全绿）

```bash
uv run pytest tests/            # 271 passed（默认启用分支覆盖率，fail_under=80）
uv run mypy src/                # strict；tests 排除
uv run ruff check src/ tests/   # clean（line-length=100，select 含 PTH/RET/PERF/SIM/ASYNC）
cd ui && npm test -- --run      # 13 passed
cd ui && npm run build
```

## 常用命令

```bash
uv sync --all-extras            # 或 --extra tts / --extra interaction / --extra dev
uv run vr-bridge                # TTS 桥（默认 8765，也可 scripts/run-bridge.sh）
uv run vr-subtitles             # 字幕服务：启动 WhisperLiveKit（scripts/run-subtitles.sh；wlk 8001）
uv run vr-ui                    # 默认入口：UI + AudioHub + 交互管道 + 字幕代理（8100）
uv run vr-interact              # headless 替代入口；必须先停止 vr-ui
uv run vr-subtitle-events       # 字幕事件消费者（--url ws://127.0.0.1:8001，--language Chinese）
scripts/download-models.sh      # SenseVoice 经 HF snapshot_download 落本地（modelscope 被拦截）
scripts/install-nltk-data.sh    # 幂等安装 NLTK punkt_tab（pipecat TTS 断句依赖；ensure_punkt_tab 自动安装失败时手动执行）
```

依赖组：`tts` = mlx-audio[tts]+misaki[zh]（重型，单独组）；`interaction` = pipecat-ai[funasr,silero,openai,soundfile,websocket,local]+torch/torchaudio（重型）；`dev` = pytest 系列 + ruff + mypy。新增依赖前确认锁 3.12。

## 环境与运行时依赖

- Apple Silicon M5 Max / macOS 26 / 128GB（本机实测）；别处按默认资源处理
- **LM Studio** `localhost:1234`，模型 `qwen/qwen3.6-35b-a3b`
- TTS 桥 8765：mlx-audio Qwen3-TTS（24 kHz WAV/PCM），VoiceDesign 音色 profile
- SenseVoice 缓存快照：`~/.cache/huggingface/hub/models--FunAudioLLM--SenseVoiceSmall/snapshots/…`
- NLTK punkt_tab：`~/nltk_data/tokenizers/punkt_tab`（pipecat 1.7 TTS 断句必需；`vr-ui` 与 `vr-interact` 启动交互前均检查，失败则手动 `scripts/install-nltk-data.sh`）
- 实测要点（文档 §7.1，QA 参考）：SenseVoice RTF≈0.17；推理关闭时 TTFT≈0.24–0.26s / ~97–113 tok/s

## 提交规范

- 中文描述式消息，`feat|fix|docs|chore|style` 前缀 + 冒号，如：`feat(interaction): local SenseVoice via HF snapshot`、`docs: 回填 §7.1/§7.2 实测验收数据`。
- 小步提交：门禁绿即可提交；bugfix 只修根因不顺手重构。
