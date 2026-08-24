# voice-realtime

> 全本地实时语音交互（Voice Assistant）+ 会议助手（Meeting Assistant，含说话人分离、PostgreSQL 持久化、异步 AI 纪要、崩溃恢复 journal）+ 实时语音字幕系统（Live Subtitles）（Apple Silicon / MLX / 中文优先 / 离线）。  
> **Python 3.12 严格锁定**（`misaki[zh]` 要求 `<3.13`）；使用 `uv` + `PEP 621` + `hatchling`。

---

## 📑 文档索引与规范契约

| 文档 / 契约 | 说明与范围 |
|---|---|
| `docs/全链路语音交互与会议助手-技术方案与实施方案.md` | **完整技术方案与实施路径**：架构、断句/分人/对账、前沿调研、ROI 与阶段落地 |
| `docs/实时语音交互与字幕-方案与最佳实践.md` | 语音交互与字幕完整技术方案；含 §7 实测验收回填数据 |
| `docs/系统总体架构与详细设计方案.md` | **系统总体架构与详细设计方案**：权威拓扑、分层架构、交互/字幕/会议/控制端到端时序与详细设计 |
| `docs/会议助手后端运行与前后端联调.md` | 会议助手运行手册、接口定义与前后端联调规范 |
| `docs/Voice-Studio-UI-设计方案.md` | 前端控制台、组件状态机与交互设计方案 |
| `docs/Qwen3-ASR-实时语音转文字开发对接手册.md` | **Qwen3-ASR 实时语音转文字开发对接手册**：WebSocket / REST 音频流式与文件转写对接指南 |
| `contracts/meeting-assistant/v1/` | OpenAPI / AsyncAPI / JSON Schema / Fixtures 规范契约 |

---

## 🏗️ 架构与数据流

```text
麦克风 ──► vr-ui / AudioHub (单源采集 / 有界扇出 / 真实静音)
            ├─► AudioInjector / Pipecat ──► LM Studio (/api/v1/chat) ──► TTS 桥 ──► 扬声器 [单人交互模式]
            ├─► SubtitleProxy ──PCM WS──► vr-subtitles / WhisperLiveKit (Sortformer 分离) [实时字幕]
            └─► MeetingSession (窗口对账 / EOF 冲刷 / Journal) ──► PostgreSQL ──► MeetingSummary [会议助手模式]
```

### 核心设计原则

- **统一所有权与模式协调**：`vr-ui`（端口 `8100`）作为默认主进程，由 `RuntimeModeCoordinator` 协调 `assistant` / `meeting` / `idle` 三种模式。语音助手与会议助手**互斥运行**（启动会议时主动挂起/关闭 Pipecat / LLM / TTS 链路，独占麦克风转录流）。
- **Headless 替代入口**：`vr-interact` 为命令行单人交互入口，通过 flock 文件锁与 `vr-ui` 互斥，禁止同时运行。
- **处理器链（`interaction/pipeline.py`）**：  
  `AudioInjector/transport.input` ➔ `EchoSuppressionProcessor` ➔ `FunASRSTTService` ➔ `SelfEchoFilter` ➔ `LLMUserAggregator (含 SileroVADAnalyzer)` ➔ `LmStudioNativeLLMService` ➔ `BotTextRecorder` ➔ `OpenAITTSService (桥)` ➔ `TTSStateObserver` ➔ `transport.output` ➔ `LLMAssistantAggregator`。

---

## 🗺️ 模块地图

| 模块 | 职责与功能 | 关键文件 |
|---|---|---|
| `voice_realtime.meeting` | 会议助手核心：会话状态机、窗口对账、PostgreSQL 持久化、说话人映射、Sortformer 接入、异步 AI 纪要生成、崩溃恢复 journal、REST API 与 WebSocket 实时网关 | `session.py`<br>`repository.py`<br>`summary.py`<br>`recovery.py`<br>`runtime_mode.py`<br>`api.py`<br>`events.py`<br>`models.py`<br>`migrations.py` |
| `voice_realtime.ui` | 默认运行时主入口：`RuntimeModeCoordinator` 模式协调、`SubtitleProxy`（带 PCM 重连快照与 ready_to_stop 优雅停机）、严格控制协议网关（`request_id` ack）、助手桥接 | `server.py`<br>`runtime.py`<br>`control.py`<br>`assistant_bridge.py`<br>`subtitle_proxy.py`<br>`protocol.py` |
| `voice_realtime.interaction` | 共享交互会话/所有权 + Pipecat 管道 + LM Studio 原生服务 + 双层回声防线 + NLTK 依赖自愈 | `session.py`<br>`ownership.py`<br>`pipeline.py`<br>`reasoning.py`<br>`runner.py`<br>`nltk_data.py` |
| `voice_realtime.subtitles` | WhisperLiveKit 启动器、Sortformer Diarization 参数注入、WS 字幕事件桥、事件去重与 SRT 持久化 | `launcher.py`<br>`consumer.py`<br>`events.py` |
| `voice_realtime.audio` | 单源麦克风采集、有界 sink 扇出、真实静音（零音频吞吐）、Pipecat 音频注入器 | `hub.py`<br>`audio_injector.py` |
| `voice_realtime.tts_bridge` | mlx-audio Qwen3-TTS → OpenAI 兼容 `POST /v1/audio/speech`，请求级音色、单并发门有界串行生成 | `server.py`<br>`engine.py`<br>`schema.py` |
| `voice_realtime.config` | 集中配置层（pydantic-settings，含 Bridge / Interaction / Subtitles / Meeting / UI） | `config.py` |
| `ui/` (前端) | React 19 + TypeScript + Vite + Zustand 前端控制台：交互助手面板、会议助手（录制/总结/历史/说话人命名）、实时字幕流、状态栏与快捷键 | `App.tsx`<br>`components/`<br>`stores/`<br>`hooks/`<br>`services/`<br>`contracts/` |

> 📌 **注**：`tools/` 下的 `WhisperLiveKit` 与 `mlx-audio` 为 vendor 子仓库（仅启动/桥接），非自研代码。

---

## ⚠️ CRITICAL 实现约束（实测，写代码前必读，防回退）

### 1. LM Studio 推理开关只能走原生端点
- OpenAI 兼容 `/v1/chat/completions` **忽略** `reasoning` 参数；唯一有效方式为**原生 `/api/v1/chat` + `reasoning: "off"`**（实测 `reasoning_output_tokens=0`）。
- 原生 `/api/v1/chat` 不接收 OpenAI `messages` 角色历史：交互首轮必须发送独立的
  `system_prompt` + 当前 user `input`，后续只发送当前 `input` + `previous_response_id`；
  LM Studio 由此保存真实 system/user/assistant 角色链。不得把历史 assistant 压成 user text item。
- 流式正文事件为 `message.delta`，只有 `chat.end.result.response_id` 才提交新会话状态；
  `clear_context` / persona 切换必须重置 response chain。payload 不发送 `role` 或 OpenAI
  `max_tokens`；后台原生摘要/预热可使用 LM Studio 支持的 `max_output_tokens`。
- 长会话由应用层根据原生 `input_tokens` / TTFT 滚动压缩：结构化摘要使用 `store:false`，新链
  预热只接受精确 `MEMORY_READY`、零 reasoning tokens 和合法 response ID，再按 generation / turn /
  旧 ID 原子换链。默认 soft/hard/target 为 16384/32768/8192 tokens、保留最近 16 组问答；
  `VR_INTERACTION_CONTEXT_COMPACTION_ENABLED=false` 可回滚。断链必须先恢复记忆再重试当前 user，
  禁止静默空链降级。详见 ADR-003。
- `LmStudioNativeLLMService`（`interaction/reasoning.py`）与 `MeetingSummaryService`（`meeting/summary.py`）均封装原生端点；**切勿改回**向 OpenAI 端点注入 `extra_body` 的方案。

### 2. 离线优先与模型下载源
- 默认 `allow_model_downloads=False` 且使用 `local_files_only=True`，只有显式授权才允许联网。
- pipecat `FunASRSTTService` 把 funasr `hub` 硬编码为 modelscope（`ms`），在受限网络下会被 **SSRF 拦截** ➔ 任何 repo ID 必须经 `snapshot_download()` 存入 `~/.cache/huggingface/hub` 后使用**本地路径**加载（`pipeline._resolve_stt_model` + `InteractionSettings.stt_model`，空值自动解析 `FunAudioLLM/SenseVoiceSmall` 快照）。
- Sortformer（`runtime/sortformer.nemo`）与 Qwen3-ASR 本地模型目录缺失时 fail-fast，不隐式联网下载。

### 3. 会议数据边界与存储隔离
- PostgreSQL 是会议元数据、confirmed 转录、speaker 映射与 AI 纪要的**唯一事实源**；**绝对不保存音频**；会议采集不写 `runtime/subtitles/current.srt`。
- 故障恢复 journal（`runtime/meetings/recovery/*.jsonl`）目录权限 `0700`、文件权限 `0600`，仅在数据库写入短暂失败时记录 confirmed 文本操作，回放成功后删除。
- 测试数据库必须使用独立临时 schema 并于测试结束时执行 `DROP SCHEMA ... CASCADE` 清理，严禁将测试 DSN 指向生产数据。

### 4. 运行时模式互斥与单一音频源
- 麦克风输入由 `AudioHub` 独占采集，通过有界队列扇出；
- 语音助手与会议助手**不可同时录音**。进入会议模式时主动挂起语音助手；会议结束后返回空闲态。

### 5. 字幕与会议 EOF 优雅冲刷
- 会议结束时向 WhisperLiveKit 发送空 PCM 作为 EOF，等待 `ready_to_stop` 信号后再封存 confirmed 转录；超时则标记 `interrupted/finalization_timeout`。
- `SubtitleProxy` 支持重连期间重放 PCM 活跃快照，保证断线重连后转录文本不丢字。

### 6. HTTP / 控制 WebSocket 测试与边界
- `httpx.AsyncClient.stream()` 的请求体关键字参数是 **`json=`**（不是 `body=`）；测试 mock 必须同名，否则测试端报错 `KeyError: 'model'`。
- **TTS 桥 422 排查**：`SpeechRequest.model` 是必填字段；出现 422 错误先检查 payload 字段完整性（`HealthResponse` 无此约束）。
- 控制 WebSocket 严格校验 `request_id` 与 loopback Origin。

### 7. 回声死循环两道防线（勿删）
> 单机同麦同箱环境下必须保留双层防护（`pipeline.py`）。

- **L1 `EchoSuppressionProcessor`**：TTS 播报**全程**丢弃输入帧，仅当输入 RMS 超过回声基线（滑动中位数）× `echo_barge_in_gain`（默认 `2.5`）连续 `echo_barge_in_frames`（默认 `3`）帧（真人插话能量明显更高）才放行；TTS 结束后执行 `echo_tail_hangover_secs`（默认 `0.4s`）尾延抑制。删除会导致"机器人一开口就打断自己 / 长播报尾部回声自触发"。
- **L2 `BotTextRecorder` + `SelfEchoFilter`（共享 `EchoTextBuffer`）**：用户转写文本与近端（`echo_text_window_secs` 默认 `10s`）机器人播报文本相似度 $\ge$ `echo_text_similarity`（默认 `0.7`）或为其子串时 ➔ 吞帧不送入 LLM 上下文，确保机器人永不响应自己的话，阻断内容层死循环。
- **端点参数联动**：`silence_secs`（`0.45`）必须略小于 STT `ttfs_p99_latency`（`0.5`），保留转写等待窗口。

### 8. Lint & 类型检查规范
- `ruff` 刻意忽略 `RUF001/002/003`（中文全角标点是项目既定风格）；
- `tests/*` 采用 per-file-ignore `S101/ANN001/ANN201`；
- `mypy` strict 校验**仅针对 `src/`**（配置中 `exclude = ["tests/"]`）。

---

## 🛡️ 质量门禁（提交前必须全绿）

```bash
# 1. 后端单元与集成测试（启用分支覆盖率，fail_under=80，实测 580 passed，覆盖率 ~84%）
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/

# 2. Python 类型检查（strict，46 source files 全绿）
uv run mypy src/

# 3. Python 代码风格与 Lint 检查
uv run ruff check src/ tests/

# 4. 前端测试（55 passed / 11 test files）
cd ui && npm test -- --run

# 5. 前端类型检查与生产构建
cd ui && npm run build
```

---

## 💻 常用命令

### 依赖与环境初始化
```bash
uv sync --all-extras                                # 安装全量依赖（含 tts, interaction, dev）
psql knowledge -f scripts/bootstrap-meeting-db.sql  # 初始化 PostgreSQL voice_realtime 角色与 schema
scripts/download-models.sh                          # SenseVoice 经 HF snapshot_download 落本地快照
scripts/install-nltk-data.sh                        # 幂等安装 NLTK punkt_tab（pipecat TTS 断句依赖）
```

### 服务运行
```bash
scripts/run-all.sh                                  # 一键启动全套服务（默认 127.0.0.1，含 ui + bridge + subtitles）
VR_BIND_HOST=lan scripts/run-all.sh                 # 局域网绑定模式启动全套服务（自动探测 LAN IP）
VR_BIND_HOST=0.0.0.0 scripts/run-all.sh             # 全网卡绑定模式启动全套服务

# 独立服务启动（也可直接使用对应 scripts/run-*.sh）
uv run vr-ui                                        # 默认入口：Voice Studio UI + AudioHub + 会议/交互/字幕 (8100)
uv run vr-bridge                                    # TTS 桥（默认 8765，也可运行 scripts/run-bridge.sh）
uv run vr-subtitles                                 # 字幕服务：启动 WhisperLiveKit（8001，含 Sortformer 分离）
uv run vr-interact                                  # Headless 命令行交互替代入口（必须先停止 vr-ui）
uv run vr-subtitle-events                           # 字幕事件消费者（--url ws://127.0.0.1:8001 --language Chinese）
```

> 🌐 **网络绑定配置**：
> - 默认绑定 `127.0.0.1`（`localhost`，仅限本机访问）；
> - 支持全局环境变量 `VR_BIND_HOST`（或 `VR_HOST`），可选 `localhost` / `0.0.0.0` / `lan`；
> - 支持服务级环境变量覆盖：`VR_UI_HOST`（UI 8100）、`VR_BRIDGE_HOST`（TTS 8765）、`VR_SUBTITLE_HOST`（字幕 8001）。

> 📦 **依赖组说明**：
> - `tts`：`mlx-audio[tts]` + `misaki[zh]`（重型依赖）
> - `interaction`：`pipecat-ai[funasr,silero,openai,soundfile,websocket,local]` + `modelscope` + `torch/torchaudio`（重型依赖）
> - `dev`：`pytest` 系列 + `ruff` + `mypy`
> - *新增任何依赖前请严格确认 Python 3.12 兼容性锁定。*

---

## ⚙️ 环境与运行时依赖

| 依赖组件 | 规格与配置要求 |
|---|---|
| **硬件平台** | Apple Silicon M-series (M5 Max / macOS 26 / 128GB 等) |
| **LM Studio** (`localhost:1234`) | - **交互模型**：`qwen/qwen3.6-35b-a3b`<br>- **会议纪要模型**：`qwen/qwen3.8-27b` |
| **PostgreSQL** | DSN: `postgresql:///knowledge`，Schema: `voice_realtime` |
| **TTS 桥服务** (Port: `8765`) | `mlx-audio` Qwen3-TTS (24 kHz WAV/PCM)，`VoiceDesign` 音色 profile |
| **SenseVoice 缓存快照** | `~/.cache/huggingface/hub/models--FunAudioLLM--SenseVoiceSmall/snapshots/…` |
| **Sortformer 说话人分离** | `runtime/sortformer.nemo` |
| **Qwen3-ASR 本地目录** | `runtime/qwen3-asr-1.7b`（MPS/windowed，12s 左上下文，支持领域词 context） |
| **NLTK punkt_tab** | `~/nltk_data/tokenizers/punkt_tab`（TTS 断句必需；`vr-ui`/`vr-interact` 自动检查与修复） |
| **实测性能基准 (QA 参考)** | SenseVoice RTF $\approx 0.17$；推理关闭时 TTFT $\approx 0.24 \sim 0.26\text{s}$ / $\sim 97 \sim 113\text{ tok/s}$ |

---

## 🚀 提交规范

- **Commit Message 格式**：`feat|fix|docs|chore|style: 中文描述`  
  - *示例*：`feat(meeting): 集成会议助手后端运行链路`  
  - *示例*：`docs: 更新 AGENTS.md 优化文档排版与结构`
- **提交流程**：质量门禁全绿即可提交；Bugfix 仅修复根因，严禁顺手大范围无关联重构。
