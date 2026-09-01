# voice-realtime

> 全本地实时语音交互（Voice Assistant）+ 会议助手（Meeting Assistant，含说话人分离、PostgreSQL 持久化、异步 AI 纪要、崩溃恢复 journal）+ 实时语音字幕系统（Live Subtitles）+ 会中内心 OS 伴侣（Inner OS 私密局势研判与发言对策）（Apple Silicon / 中文优先 / 离线）。
> **Python 3.12 严格锁定**；使用 `uv` + `PEP 621` + `hatchling`。

---

## 📑 文档索引与规范契约

| 文档 / 契约 | 说明与范围 |
|---|---|
| `docs/README.md` | **文档中心总览**：全量文档索引、生命周期状态对照与研发导航矩阵 |
| `docs/architecture/系统总体架构与详细设计方案.md` | **系统总体架构与详细设计方案**：权威拓扑、分层架构、交互/字幕/会议/控制端到端时序与详细设计 |
| `docs/architecture/全链路语音交互与会议助手-技术方案与实施方案.md` | **完整技术方案与实施路径**：架构、断句/分人/对账、前沿调研、ROI 与阶段落地 |
| `docs/architecture/实时语音交互与字幕-方案与最佳实践.md` | 语音交互与字幕完整技术方案；含 §7 实测验收回填数据 |
| `docs/solutions/会议模式多说话人精准识别与声纹聚类技术方案.md` | **历史归档**：本地 CAM++/AHC 声纹方案；当前实现以 SpeechRail diarization、应用侧平滑与会议作用域映射为准 |
| `docs/manuals/会议助手后端运行与前后端联调.md` | 会议助手运行手册、接口定义与前后端联调规范 |
| `docs/manuals/Voice-Studio-UI-设计方案.md` | 前端控制台、组件状态机与交互设计方案 |
| `docs/manuals/SpeechRail-Realtime-v2-语音转文字开发对接手册.md` | **SpeechRail Realtime v2 语音转文字开发对接手册**：WebSocket / REST 音频流式与文件转写对接指南 |
| `contracts/meeting-assistant/v1/` | OpenAPI / AsyncAPI / JSON Schema / Fixtures 规范契约 |

---

## 🏗️ 架构与数据流

```text
麦克风 ──► vr-ui / AudioHub (单源采集 / 有界扇出 / 真实静音)
            ├─► AudioInjector / Pipecat ──► LM Studio (/api/v1/chat) ──► SpeechRail Realtime v2 TTS ──► 扬声器 [单人交互模式]
            ├─► SubtitleProxy ──PCM WS──► SpeechRail Realtime v2 (ASR / diarization profile) [实时字幕]
            └─► MeetingSession (窗口对账 / EOF 冲刷 / Journal) ──► PostgreSQL ──► MeetingSummary [会议助手模式]
                    │
                    └──► Inner OS (会前底牌 / 局势研判 / 事实核查 / 回应草稿 / 会后即焚) ──► LM Studio [会中伴侣]
```

### 核心设计原则

- **统一所有权与模式协调**：`vr-ui`（端口 `8100`）作为默认主进程，由 `RuntimeModeCoordinator` 协调 `assistant` / `subtitles` / `meeting` / `idle` 四种模式。语音助手、普通字幕与会议助手**互斥消费 PCM**（启动会议时主动挂起交互链路，独占麦克风转录流）。
- **Headless 替代入口**：`vr-interact` 为命令行单人交互入口，通过 flock 文件锁与 `vr-ui` 互斥，禁止同时运行。
- **处理器链（`interaction/pipeline.py`）**：  
  `AudioInjector/transport.input` ➔ `EchoSuppressionProcessor` ➔ `SpeechRail STT` ➔ `SelfEchoFilter` ➔ `LLMUserAggregator (含 SileroVADAnalyzer)` ➔ `LmStudioNativeLLMService` ➔ `BotTextRecorder` ➔ `SpeechRailTTSService (Realtime v2)` ➔ `TTSStateObserver` ➔ `transport.output` ➔ `LLMAssistantAggregator`。

---

## 🗺️ 模块地图

| 模块 | 职责与功能 | 关键文件 |
|---|---|---|
| `voice_realtime.asr` | SpeechRail ASR 契约与适配层：Realtime v2 流式适配器、Pipecat 轮次适配与结果呈现 | `contracts.py`<br>`profiles.py`<br>`adapters/`<br>`presenters.py` |
| `voice_realtime.meeting` | 会议助手核心：会话状态机、窗口对账、PostgreSQL 持久化、SpeechRail diarization 接入与应用侧平滑/remap、异步 AI 纪要生成、崩溃恢复 journal、REST API 与 WebSocket 实时网关 | `session.py`<br>`repository.py`<br>`diarization_smoother.py`<br>`summary.py`<br>`recovery.py`<br>`runtime_mode.py`<br>`api.py`<br>`events.py`<br>`models.py`<br>`migrations.py` |
| `voice_realtime.ui` | 默认运行时主入口：`RuntimeModeCoordinator` 模式协调、`SubtitleProxy`（带 PCM 重连快照与 `session.completed` 优雅停机）、严格控制协议网关（`request_id` ack）、助手桥接 | `server.py`<br>`runtime.py`<br>`control.py`<br>`assistant_bridge.py`<br>`subtitle_proxy.py`<br>`protocol.py` |
| `voice_realtime.interaction` | 共享交互会话/所有权 + Pipecat 管道 + LM Studio 原生服务 + 双层回声防线 + 滚动记忆压缩与 NLTK 依赖自愈 | `session.py`<br>`ownership.py`<br>`pipeline.py`<br>`reasoning.py`<br>`context_memory.py`<br>`runner.py`<br>`nltk_data.py` |
| `voice_realtime.subtitles` | 保留的包命名空间；当前字幕 workload 由 `voice_realtime.ui.subtitle_proxy` 通过 SpeechRail 适配 | `__init__.py` |
| `voice_realtime.audio` | 单源麦克风采集、有界 sink 扇出、真实静音（零音频吞吐）、Pipecat 音频注入器 | `hub.py`<br>`audio_injector.py` |
| `voice_realtime.speechrail` | SpeechRail Realtime v2 的 ASR/TTS 客户端、事件顺序校验、PCM 解码与取消回收 | `transport.py`<br>`tts.py`<br>`__init__.py` |
| `voice_realtime.config` | 集中配置层（pydantic-settings，含 Bridge / Interaction / Subtitles / Meeting / UI / ASR） | `config.py` |
| `ui/` (前端控制台) | React 19 + TypeScript + Vite 7 + Zustand 前端控制台：交互助手面板、会议助手（录制/总结/历史/说话人命名）、实时字幕流、声学波形、状态栏与快捷键 | `App.tsx`<br>`components/`<br>`stores/`<br>`hooks/`<br>`services/`<br>`contracts/` |
| `ui/src/features/innerOS` | 会中内心 OS 伴侣前端：会前底牌抽屉、Prompt 快捷矩阵、流式多意图研判卡片、会后即焚瞬态管理与历史归档 | `InnerOSPanel.tsx`<br>`InnerOSEphemeralContext.tsx`<br>`InnerOSAnswerCard.tsx`<br>`InnerOSArchive.tsx`<br>`innerOSStore.ts` |

> 📌 **注**：`tools/` 下的 `WhisperLiveKit` 为历史 vendor 子仓库，不属于当前启动链；当前 ASR/TTS 模型与运行时统一由独立 SpeechRail 服务负责。

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
- 会议纪要默认 map/reduce 输出上限为 `2048/10240` tokens，客户端字符熔断为 `65536`；模型侧契约必须保持紧凑，token 触顶且 JSON 未闭合时归类为 `output_limit`，不得循环 repair。

### 2. 离线优先与模型下载源
- 默认 `allow_model_downloads=False` 且使用 `local_files_only=True`，只有显式授权才允许联网。
- SpeechRail 独占 ASR/TTS 模型生命周期；`voice-realtime` 只通过 Realtime v2 客户端消费 ASR/TTS，
  不安装、下载或启动 ASR/TTS 模型。
- SpeechRail 的 Qwen3-ASR 与 diarization profile（如 Sortformer）必须由 SpeechRail 使用项目外的绝对 snapshot 路径加载；
  缺失时由 SpeechRail fail-fast，不隐式联网下载，也不得重新放回 `voice-realtime/runtime/`。

### 3. 会议数据边界与存储隔离
- PostgreSQL 是会议元数据、confirmed 转录、speaker 映射与 AI 纪要的**唯一事实源**；**绝对不保存音频**；会议采集不写 `runtime/subtitles/current.srt`。
- 故障恢复 journal（`runtime/meetings/recovery/*.jsonl`）目录权限 `0700`、文件权限 `0600`，仅在数据库写入短暂失败时记录 confirmed 文本操作，回放成功后删除。
- 测试数据库必须使用独立临时 schema 并于测试结束时执行 `DROP SCHEMA ... CASCADE` 清理，严禁将测试 DSN 指向生产数据。

### 4. 运行时模式互斥与单一音频源
- 麦克风输入由 `AudioHub` 独占采集，通过有界队列扇出；
- 语音助手与会议助手**不可同时录音**。进入会议模式时主动挂起语音助手；会议结束后返回空闲态。

### 5. 字幕与会议 EOF 优雅冲刷
- 会议结束时通过 SpeechRail Realtime v2 发送 `input_audio_buffer.commit` 作为 EOF，等待 `session.completed`（及已启用分人的最终事件）后再封存 confirmed 转录；超时则标记 `interrupted/finalization_timeout`。
- `SubtitleProxy` 支持重连期间重放 PCM 活跃快照，保证断线重连后转录文本不丢字。

### 6. HTTP / 控制 WebSocket 测试与边界
- `httpx.AsyncClient.stream()` 的请求体关键字参数是 **`json=`**（不是 `body=`）；测试 mock 必须同名，否则测试端报错 `KeyError: 'model'`。
- **SpeechRail TTS 422 排查**：`SpeechRequest.model` 是必填字段；出现 422 错误先检查 payload 字段完整性（`HealthResponse` 无此约束）。
- 控制 WebSocket 严格校验 `request_id` 与 loopback Origin。

### 7. 回声死循环两道防线（勿删）
> 单机同麦同箱环境下必须保留双层防护（`pipeline.py`）。

- **L1 `EchoSuppressionProcessor`**：`speaker_focus` 默认在 TTS 播报**全程**丢弃输入帧；显式允许 barge-in 时，使用自适应峰值包络/快慢 EMA，只有输入 RMS 超过动态基线 × `echo_barge_in_gain`（默认 `2.5`）并连续 `echo_barge_in_frames`（默认 `3`）帧（真人插话能量明显更高）才放行；TTS 结束后执行 `echo_tail_hangover_secs`（默认 `0.4s`）尾延抑制。删除会导致"机器人一开口就打断自己 / 长播报尾部回声自触发"。
- **L2 `BotTextRecorder` + `SelfEchoFilter`（共享 `EchoTextBuffer`）**：用户转写文本与近端（`echo_text_window_secs` 默认 `10s`）机器人播报文本相似度 $\ge$ `echo_text_similarity`（默认 `0.7`）或为其子串时 ➔ 吞帧不送入 LLM 上下文，确保机器人永不响应自己的话，阻断内容层死循环。
- **端点参数联动**：`silence_secs`（`0.45`）必须略小于 STT `ttfs_p99_latency`（`0.5`），保留转写等待窗口。

### 8. 内心 OS 伴侣与会后即焚原则
- **隐私与生命周期隔离**：会前底牌与会中即时问答默认仅驻留在浏览器端内存与会话生命周期内，标注"会后即焚"，会议结束自动销毁；只有用户主动点击"保存"才持久化至会话存储。
- **布局防挤压与黄金分割规范**：内心 OS 侧边面板宽度设为黄金分割小端比例（`width: clamp(520px, 38.2vw, 860px)`）；支持面板外任意点击（click outside）自动收起；主滚动视口与子抽屉严格设置 `flex-shrink: 0` 与定制细滚动条（`scrollbar-width: thin`），防止卡片增多时组件被挤压坍缩。

### 9. 全局无障碍对比度硬性约束（WCAG 2.1 AA/AAA）
- **全局适用范围**：覆盖全站所有模块（语音助手、会议助手、实时字幕、内心 OS、侧边栏、状态栏、模态弹窗及深浅双色主题）。
- **对比度指标底线**：普通正文文本对比度严格满足 $\ge 4.5:1$（AA级）或 $\ge 7.0:1$（AAA级）；大字标题与关键 UI 交互组件（按钮底色与文字、激活胶囊、边框、状态小圆点）对比度严格满足 $\ge 3.0:1$。
- **色彩设计禁忌**：严禁在浅色背景下直接渲染浅紫/淡黄色文本（如 `#c4b5fd`），严禁在浅紫（如 `#c084fc`）或浅红（如 `#f87171`）按钮底色上使用纯白文字（`#ffffff`），深色模式与浅色模式必须双向复核对比度。

### 10. Lint & 类型检查规范
- `ruff` 刻意忽略 `RUF001/002/003`（中文全角标点是项目既定风格）；
- `tests/*` 采用 per-file-ignore `S101/ANN001/ANN201`；
- `mypy` strict 校验**仅针对 `src/`**（配置中 `exclude = ["tests/"]`）。

---

## 🛡️ 质量门禁（提交前必须全绿）

```bash
# 1. 后端单元与集成测试（需设置 `VR_TEST_DATABASE_URL` 才会运行 PostgreSQL 临时 schema 测试；分支覆盖率门禁 `fail_under=80`）
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/

# 2. Python 类型检查（strict，仅校验 `src/`）
uv run mypy src/

# 3. Python 代码风格与 Lint 检查
uv run ruff check src/ tests/

# 4. 前端测试（以当前测试输出为准）
cd ui && npm test -- --run

# 5. 前端类型检查与生产构建
cd ui && npm run build
```

---

## 💻 常用命令

### 依赖与环境初始化
```bash
uv sync --all-extras                                # 安装全量依赖（含 interaction, dev）
psql knowledge -f scripts/bootstrap-meeting-db.sql  # 初始化 PostgreSQL voice_realtime 角色与 schema
scripts/install-nltk-data.sh                        # 幂等安装 NLTK punkt_tab（pipecat TTS 断句依赖）
```

### 服务运行
```bash
scripts/run-all.sh                                  # 一键启动应用服务（默认 127.0.0.1，含 ui；SpeechRail 独立管理）
VR_BIND_HOST=lan scripts/run-all.sh                 # 局域网绑定模式启动全套服务（自动探测 LAN IP）
VR_BIND_HOST=0.0.0.0 scripts/run-all.sh             # 全网卡绑定模式启动全套服务

# 独立服务启动（也可直接使用对应 scripts/run-*.sh）
uv run vr-ui                                        # 默认入口：Voice Studio UI + AudioHub + 会议/交互/字幕 (8100)
uv run vr-interact                                  # Headless 命令行交互替代入口（必须先停止 vr-ui）
```

> 🌐 **网络绑定配置**：
> - 默认绑定 `127.0.0.1`（`localhost`，仅限本机访问）；
> - 支持全局环境变量 `VR_BIND_HOST`（或 `VR_HOST`），可选 `localhost` / `0.0.0.0` / `lan`；
> - 支持服务级环境变量覆盖：`VR_UI_HOST`（UI 8100）。SpeechRail 的绑定、模型与 profile 配置由 SpeechRail 自身管理。

> 📦 **依赖组说明**：
> - `interaction`：`pipecat-ai[silero,openai,soundfile,websocket,local]` + `torch/torchaudio` + `pyaudio`（重型依赖）
> - `dev`：`pytest` 系列 + `ruff` + `mypy`
> - *新增任何依赖前请严格确认 Python 3.12 兼容性锁定。*

---

## ⚙️ 环境与运行时依赖

| 依赖组件 | 规格与配置要求 |
|---|---|
| **硬件平台** | Apple Silicon / macOS 14+；具体设备与资源以当前实测为准 |
| **LM Studio** (`localhost:1234`) | - **统一模型（交互 / 纪要 / 标题 / 内心 OS）**：`qwen/qwen3.6-35b-a3b`（或 `qwen2.5-7b/14b`） |
| **PostgreSQL** | DSN: `postgresql:///knowledge`，Schema: `voice_realtime` |
| **SpeechRail ASR/TTS** (Port: `8201`) | ASR Realtime v2、diarization profile 与公共 TTS 模型均由 SpeechRail 管理；TTS 公共模型 ID `speechrail/qwen3-tts`，preset `default/warm/bright/calm` |
| **NLTK punkt_tab** | `~/nltk_data/tokenizers/punkt_tab`（TTS 断句必需；`vr-ui`/`vr-interact` 自动检查与修复） |
| **实测性能基准 (QA 参考)** | SpeechRail ASR/TTS 与 LM Studio 的实测指标以各自服务运行记录为准；voice-realtime 不重复持有模型基准 |

---

## 🚀 提交规范

- **Commit Message 格式**：`feat|fix|docs|chore|style: 中文描述`  
  - *示例*：`feat(meeting): 集成会议助手后端运行链路`  
  - *示例*：`docs: 更新 AGENTS.md 优化文档排版与结构`
- **提交流程**：质量门禁全绿即可提交；Bugfix 仅修复根因，严禁顺手大范围无关联重构。
