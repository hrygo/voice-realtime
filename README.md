# voice-realtime (Voice Studio)

> 🎙️ **全本地、端到端、低延迟的实时语音交互 + 智能会议助手 + 实时语音字幕系统**  
> 专为 **Apple Silicon / macOS** 打造，**中文优先**，**100% 离线隐私安全**。

---

## 🌟 核心特性

- 🤖 **全双工实时语音助手 (Voice Assistant)**
  - **实时语音输入**：基于 SpeechRail Realtime v2 + 本地大语言模型 (LM Studio / Qwen3.6) + MLX Qwen3-TTS。
  - **双重声学防回声**：L1 声学 RMS 自适应打断抑制 + L2 文本相似度过滤，彻底杜绝单机外放播报时的“自我回声死循环”。
  - **双工模式切换**：支持“🔊 外放保护”（播报期间暂停收音，防误触）与“🎧 耳机双工”（佩戴耳机时随时自然插话打断）。
  - **个性化人设与音色**：内置通用助手、程序员、英语教练等丰富人设模板，支持请求级动态切换音色。
  - **长会话上下文智能滚动压缩**：基于 LM Studio 原生 Token 计数平滑压缩上下文，支持无感长聊。

- 🎙️ **智能会议助手 (Meeting Assistant)**
  - **实时转录与持久化**：SpeechRail 输出的已确认片段进入会议记录；说话人标注能力取决于 SpeechRail 当前 profile。
  - **可靠数据持久化**：采用 PostgreSQL ACID 存储全量确认转录记录与发言人映射，拒绝在内存中积压数据；内置 `0700/0600` 崩溃恢复 Journal。
  - **异步 AI 会议纪要**：会议结束自动冲刷（EOF Flush），调用后台模型异步生成包含「议题大纲、核心讨论、关键决策、待办事项 (Action Items)」的高质量结构化纪要。
  - **灵活导出与管理**：一键导出 Markdown 纪要与带说话人标签的 SRT 字幕文件，支持历史会议回溯与检索。

- 📝 **实时语音字幕 (Live Subtitles)**
  - **窗口式流式识别**：通过 SpeechRail Realtime v2 提交与获取最终转录，支持实时低延迟上屏。
  - **独立运行与无缝联动**：切换到字幕页面时自动挂起语音助手，保证纯净转录；支持实时同步会议转录流。

- 🔒 **100% 本地与隐私安全**
  - 无需任何云端 API Key，无需联网传输音频，所有音频处理、转录、LLM 推理与语音合成均在本地 Apple Silicon NPU/GPU/CPU 上完成。

---

## 🏗️ 系统架构与数据流

`voice-realtime` 采用 **单音频源独占采集 + 有界扇出 + 互斥运行模式协调** 的设计哲学：

```text
               ┌────────── 麦克风输入 (Microphone) ──────────┐
               │                                             │
               ▼                                             │
      ┌─────────────────┐                                    │
      │    AudioHub     │ (16kHz Mono int16 单源采集 / 真实静音)
      └────────┬────────┘                                    │
               │                                             │
    ┌──────────┴──────────────────────────────┐              │
    │ [单人交互模式 - 语音助手]               │ [会议模式 / 字幕模式]
    ▼                                         ▼              │
┌───────────────────────────────┐   ┌────────────────────────┴──────────────┐
│  Pipecat 管道 (AudioInjector) │   │ SubtitleProxy / WebSocket             │
├───────────────────────────────┤   ├───────────────────────────────────────┤
│ • L1 声学回声抑制 (RMS)       │   │ • SpeechRail Realtime v2               │
│ • SpeechRail Realtime STT     │   │ • SpeechRail Realtime ASR              │
│ • L2 文本相似度自激过滤       │   │ • 窗口对账与快照管理                  │
│ • LM Studio 原生 /api/v1/chat │   └───────────────────┬───────────────────┘
│ • MLX Qwen3-TTS 桥 (:8765)    │                       │
└──────────────┬────────────────┘                       │
               │                                        ▼
               ▼ (扬声器/耳机)             ┌────────────────────────────────┐
        [语音实时播报]                     │  PostgreSQL (ACID 会议存储)    │
                                           ├────────────────────────────────┤
                                           │ • 会议元数据 / 说话人映射      │
                                           │ • 异步生成 AI 会议纪要 (Qwen)  │
                                           │ • 故障恢复 Journal (0700/0600) │
                                           └────────────────────────────────┘
```

### 四大核心运行单元

1. **`vr-ui` (端口 `8100`)**：系统默认主进程，集成 Voice Studio Web 控制台、AudioHub 麦克风采集、运行时模式协调（`RuntimeModeCoordinator`）与交互/会议控制网关。
2. **`vr-bridge` (端口 `8765`)**：基于 `mlx-audio` 的 Qwen3-TTS 语音合成服务，提供 OpenAI 兼容的 `POST /v1/audio/speech` 流式接口。
3. **SpeechRail (端口 `8201`)**：独立 ASR 服务，提供 REST 与 Realtime v2；`voice-realtime` 不管理其进程或模型。
4. **LM Studio (端口 `1234`)**：本地大模型服务，加载 Qwen3.6 / Qwen3.8 等模型，通过原生 `/api/v1/chat` 提供超低延迟推理与高质量会议纪要生成。

> 💡 **提示**：`vr-interact` 为 CLI Headless 交互入口，通过文件锁与 `vr-ui` 互斥，适用于无界面的终端交互场景。

### 物理输出采集 Helper（P1 开发组件）

仓库已提供 macOS 14.2+ 的设备绑定 Core Audio Tap Helper，用于后续把本机所选物理输出设备
（内建扬声器、耳机、USB/HDMI 等）的播放音频作为会议助手远端声源。当前 P1 仅交付原生采集、
私有 UDS 和 Python 来源适配能力，尚未接入 Voice Studio 页面、会议转录或字幕链路；产品运行态仍为
麦克风-only，不会自动触发系统录音授权。

```bash
# 构建 release 配置的 .app；默认 ad-hoc + Hardened Runtime，仅供本机开发
scripts/build-audio-capture-helper.sh

# 静态签名/Bundle/架构检查，不采集音频
scripts/test-audio-capture-helper.sh --static

# 额外枚举物理输出设备，不创建 Tap、不触发授权
scripts/test-audio-capture-helper.sh --list-devices
```

首次显式执行真实 capture 时，macOS 会请求“系统音频录制”权限。默认 ad-hoc 签名的 `.app` 不是
发布制品；发布构建需显式设置 `VR_AUDIO_CAPTURE_SIGNING_IDENTITY`，并通过
`VR_AUDIO_CAPTURE_CODESIGN_TIMESTAMP=auto`（或 HTTPS 时间戳服务地址）启用时间戳，之后另行完成
Developer ID 发布校验与公证。

当前 P1 本机验收环境仅安装 Apple Command Line Tools，尚未执行 Developer ID 签名与公证；脚本提供
发布签名参数不代表发布链路已经验收完成。

---

## 💻 硬件与软件要求

| 维度 | 要求与推荐配置 |
|---|---|
| **硬件平台** | Apple Silicon Mac（M1 / M2 / M3 / M4 / M5 系列芯片） |
| **统一内存 (RAM)** | 具体取决于你选择的模型；交互助手和会议纪要可以共用同一个模型，不必同时运行两套大模型。推荐 **32GB 及以上**；16GB/24GB 设备也可以选择更小或量化模型使用 |
| **操作系统** | 主应用 macOS 14.0+；物理输出采集 Helper 要求 macOS 14.2+ |
| **Python 版本** | **Python 3.12 严格锁定** (`>=3.12,<3.13`，由于 `misaki[zh]` 兼容性要求) |
| **包管理工具** | [`uv`](https://docs.astral.sh/uv/)（强力推荐） |
| **数据库** | PostgreSQL 14+（用于会议助手数据持久化） |
| **Node.js** | Node.js 18+ & npm（用于编译前端界面） |
| **LLM 后端** | [LM Studio](https://lmstudio.ai/) 0.3+（开启本地服务器 `localhost:1234`） |

---

## 🚀 5 分钟快速上手

### 步骤 1：克隆仓库与安装依赖

```bash
# 1. 克隆代码仓库
git clone https://github.com/your-username/voice-realtime.git
cd voice-realtime

# 2. 一键安装 Python 全量依赖 (含 tts, interaction, dev)
uv sync --all-extras

# 3. 安装前端依赖并构建
cd ui && npm install && npm run build && cd ..
```

### 步骤 2：下载本地模型与初始化数据

本项目坚持离线优先原则，模型统一保存在 ModelScope、Hugging Face 或 LM Studio 的项目外缓存中，
Git 工作树只保存代码、配置和运行产物：

```bash
# 下载 Qwen3-TTS 与会议声纹模型；ASR 模型由 SpeechRail 管理
bash scripts/download-models.sh

# 下载 NLTK punkt_tab 分词数据 (TTS 断句必需)
bash scripts/install-nltk-data.sh
```

> ⚠️ **SpeechRail 前置条件**：先启动本机 SpeechRail，并确认 `http://127.0.0.1:8201/health` 可用。

### 步骤 3：初始化 PostgreSQL 数据库 (会议助手必需)

首次使用时，通过 PostgreSQL 初始化 `voice_realtime` 专用角色与独立 schema：

```bash
psql knowledge -f scripts/bootstrap-meeting-db.sql
```

*(默认连接 DSN 为 `postgresql://voice_realtime_app@localhost/knowledge`，Schema 为 `voice_realtime`)*

### 步骤 4：配置并启动 LM Studio

1. 打开 **LM Studio**，下载并加载推荐模型：
   - **推荐统一模型（交互 / 纪要 / 标题）**：`qwen/qwen3.6-35b-a3b`（或 `qwen2.5-7b-instruct` / `qwen2.5-14b-instruct`）
2. 在 LM Studio 中启动 Local Server，监听 `localhost:1234`。
3. 如果 Local Server 开启了 API authentication，将 key 写入项目根目录 `.env` 的
   `VR_INTERACTION_LLM_API_KEY`；程序会以 `Bearer` header 发送，启动配置表会自动脱敏。

### 步骤 5：启动系统服务

推荐通过统一脚本启动全部服务，并在启动时指定监听类型：

```bash
# 仅本机访问（默认）
scripts/run-all.sh

# 同时支持 localhost 与局域网访问（监听全部 IPv4 接口）
VR_BIND_HOST=lan scripts/run-all.sh

# 显式绑定全部网络接口（与 lan 的监听效果相同）
VR_BIND_HOST=0.0.0.0 scripts/run-all.sh
```

统一脚本会根据 TTS 桥的实际监听地址自动设置交互管道使用的
`VR_INTERACTION_TTS_BRIDGE_URL`；如显式设置该变量，则保留显式配置。

也可以依次在不同终端窗口中独立启动 2 个应用服务单元（SpeechRail 由其自身服务管理）：

```bash
# 终端 1: 启动 TTS 语音合成桥 (8765)
uv run vr-bridge

# 终端 2: 启动 Web 控制台与主运行协调服务 (8100)
export VR_MEETING_DATABASE_URL='postgresql://voice_realtime_app@/knowledge'
export VR_MEETING_SCHEMA='voice_realtime'
uv run vr-ui
```

### 步骤 6：打开 Voice Studio 控制台

在浏览器中访问：  
👉 **`http://127.0.0.1:8100`**

---

## 🖥️ Voice Studio 使用指南

Voice Studio 提供了精致、现代化、低延迟的多工作区操作界面：

### 1. 🤖 语音助手面板 (`Cmd + 1`)
- **自然交谈**：直接对着麦克风说话，系统自动检测停顿并由 LM Studio 生成流式回答，Qwen3-TTS 实时跟读。
- **双工模式切换**：
  - **🔊 外放保护 (默认)**：使用外置扬声器时，Agent 播报期间自动抑制麦克风输入，防止自激回声。
  - **🎧 耳机双工**：佩戴耳机时开启，可在 Agent 说话过程中随时开口插话打断（Barge-in）。
- **人设模版与音色**：可在控制栏切换不同助手人设（如通用助手、程序员、英语教练）以及切换 TTS 发音音色。
- **清除上下文**：支持一键清空多轮对话历史，开启全新对话。

### 2. 🎙️ 会议助手面板 (`Cmd + 2`)
- **开始会议录制**：点击「开始会议」，系统自动挂起语音交互链路，独占麦克风进行转录。
- **实时说话人分离**：Sortformer 自动识别说话人变更（`Speaker 1`、`Speaker 2` 等），可随时点击发言人头像进行自定义重命名。
- **结束会议与冲刷**：点击「结束会议」，系统执行 EOF 优雅冲刷确保最后一句话不遗漏，并写入 PostgreSQL。
- **异步 AI 会议纪要**：会议结束后后台自动调度 LLM 生成结构化会议纪要（包含议题、结论、待办事项），并在前端实时渲染 Markdown。
- **历史记录与导出**：侧边栏快速翻阅历史会议，支持一键导出 Markdown 纪要与 SRT 字幕，或删除历史记录。

### 3. 📝 实时字幕面板 (`Cmd + 3`)
- **实时跟读流**：纯净全屏字幕展示，支持实时自动滚动、暂停滚动与历史回溯。
- **自动挂起保护**：切换到字幕标签页时，系统自动挂起语音助手以防杂音干扰。

### 4. ⌨️ 全局快捷键

| 快捷键 | 功能描述 |
|---|---|
| `⌘ + 1` / `Ctrl + 1` | 切换至 **语音助手** 工作区 |
| `⌘ + 2` / `Ctrl + 2` | 切换至 **会议助手** 工作区 |
| `⌘ + 3` / `Ctrl + 3` | 切换至 **实时字幕** 工作区 |
| `?` | 打开全局快捷键帮助弹窗 |
| `Esc` | 在会议助手查看历史记录时快速返回当前录制/主视图 |

---

## ⚙️ 核心配置项与环境变量

系统基于 `pydantic-settings` 统一管理配置，支持在环境或 `.env` 文件中覆写：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `VR_BIND_HOST` | `127.0.0.1` | 全局服务绑定模式（默认 `localhost`；`lan` 同时支持 localhost 与局域网，实际监听 `0.0.0.0`；也支持显式 `0.0.0.0` / 自定义 IP） |
| `VR_UI_HOST` | `127.0.0.1` | Voice Studio Web 服务绑定地址（优先于全局变量） |
| `VR_UI_PORT` | `8100` | Voice Studio Web 服务端口 |
| `VR_BRIDGE_HOST` | `127.0.0.1` | Qwen3-TTS 桥服务绑定地址（优先于全局变量） |
| `VR_BRIDGE_PORT` | `8765` | Qwen3-TTS 桥服务端口 |
| `VR_SUBTITLE_SPEECHRAIL_URL` | `ws://127.0.0.1:8201/v2/realtime` | 字幕与会议使用的 SpeechRail Realtime v2 地址 |
| `VR_INTERACTION_SPEECHRAIL_REALTIME_URL` | `ws://127.0.0.1:8201/v2/realtime` | 语音助手使用的 SpeechRail Realtime v2 地址 |
| `VR_INTERACTION_LLM_BASE_URL` | `http://localhost:1234/v1` | LM Studio API 服务地址 |
| `VR_INTERACTION_LLM_MODEL` | `qwen/qwen3.6-35b-a3b` | 语音交互 LLM 模型名称 |
| `VR_INTERACTION_LLM_API_KEY` | `lm-studio` | LM Studio API key；仅保存在本机 `.env`，通过 `Authorization: Bearer` 发送 |
| `VR_INTERACTION_TTS_BRIDGE_URL` | `http://127.0.0.1:8765/v1` | 交互管道使用的 TTS 端点；`scripts/run-all.sh` 未显式配置时按桥监听地址自动推导 |
| `VR_INTERACTION_INPUT_DEVICE_NAME` | 空（系统默认输入） | 麦克风完整名称或唯一名称片段；找不到或匹配多个设备时停止语音采集，不回退到系统默认设备 |
| `VR_MEETING_SUMMARY_MODEL` | `qwen/qwen3.6-35b-a3b` | 会议纪要 LLM 模型名称 |
| `VR_MEETING_DATABASE_URL` | `postgresql://voice_realtime_app@/knowledge` | PostgreSQL 数据库连接串 |
| `VR_MEETING_SCHEMA` | `voice_realtime` | 会议数据存放 Schema |
| `VR_INTERACTION_DUPLEX_MODE` | `speaker_focus` | 默认双工模式 (`speaker_focus` / `headphone_duplex`) |

---

## 🛡️ 质量门禁与工程规范

本项目遵循高标准的工程质量与严苛的测试规范，全链路质量门禁命令：

前后端分离开发可先启动不依赖模型和数据库的契约 mock：

```bash
scripts/run-meeting-mock.sh
uv run python3 scripts/validate-meeting-contract.py
```

默认 mock 地址为 `http://127.0.0.1:8200`，事件 WS 为
`ws://127.0.0.1:8200/ws/v1/meetings`；场景和故障参数见
[`docs/operations/联调记录模板.md`](docs/operations/联调记录模板.md)。

```bash
# 1. 后端单元与集成测试 (带分支覆盖率，fail_under=80，实测 1161 passed, 10 skipped，覆盖率 ~82%)
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/

# 2. Python 严格静态类型检查 (Strict mode，87 source files 全绿)
uv run mypy src/

# 3. Python 代码风格与 Lint 检查 (全通过)
uv run ruff check src/ tests/

# 4. 前端单元与组件测试 (144 passed / 17 test files 全绿)
cd ui && npm test -- --run

# 5. 前端类型检查与生产构建 (React 19 + TypeScript + Vite 7)
cd ui && npm run build
```

---

## ❓ 常见问题排查 (FAQ)

<details>
<summary><b>Q1: 为什么要求 LM Studio 必须关闭推理 (reasoning: "off")？</b></summary>

在实时语音交互场景中，大模型的思考标记（`<think>...</think>`）会导致首字时间（TTFT）延长数秒，严重破坏对话流畅度。本项目封装了 LM Studio 原生 `/api/v1/chat` 端点，强制设置 `reasoning: "off"`，实现毫秒级首字吐词与极速交互。
</details>

<details>
<summary><b>Q2: 麦克风无法收音或报错权限不足怎么办？</b></summary>

请确保在 macOS 的 **「系统设置」➔「隐私与安全性」➔「麦克风」** 中，为当前运行的终端（Terminal、iTerm2、VS Code 等）授予了麦克风访问权限。
</details>

<details>
<summary><b>Q3: 如何在无图形界面 (Headless) 环境下使用语音助手？</b></summary>

停止 `vr-ui` 主进程后，运行命令行专属交互入口：
```bash
uv run vr-interact
```
`vr-interact` 与 `vr-ui` 通过跨进程文件锁互斥，保证麦克风采集所有权安全。
</details>

<details>
<summary><b>Q4: 为什么模型在没有外网时无法启动？</b></summary>

本项目默认开启 `allow_model_downloads=False`（离线优先）。首次部署时，请在有网络的环境下执行 `bash scripts/download-models.sh` 将模型完整缓存到本地，之后即可在完全离线/断网环境下秒级冷启。
</details>

---

## 📑 深入技术文档

深入了解系统内部架构、契约定义与设计决策：

- 🧭 [**Voice Realtime 文档中心总览**](docs/README.md)
- 📖 [系统总体架构与详细设计方案](docs/architecture/系统总体架构与详细设计方案.md)
- 📖 [全链路语音交互与会议助手-技术方案与实施方案](docs/architecture/全链路语音交互与会议助手-技术方案与实施方案.md)
- 📖 [实时语音交互与字幕-方案与最佳实践](docs/architecture/实时语音交互与字幕-方案与最佳实践.md)
- 📖 [声学防回声与全双工交互设计方案](docs/architecture/声学防回声与全双工交互设计方案.md)
- 📖 [会议模式多说话人精准识别与声纹聚类技术方案](docs/solutions/会议模式多说话人精准识别与声纹聚类技术方案.md)
- 📖 [会议助手后端运行与前后端联调手册](docs/manuals/会议助手后端运行与前后端联调.md)
- 📖 [Voice Studio UI 设计方案](docs/manuals/Voice-Studio-UI-设计方案.md)
- 📖 [Qwen3-ASR 实时语音转文字开发对接手册](docs/manuals/Qwen3-ASR-实时语音转文字开发对接手册.md)
- 📖 [物理输出音频采集验收手册](docs/manuals/物理输出音频采集验收手册.md)
- 📐 [会议助手 OpenAPI / AsyncAPI / JSON Schema 契约规范](contracts/meeting-assistant/v1)
- 📝 [架构决策记录 (ADR-001 ~ ADR-008)](docs/decisions)

---

## 📄 开源许可证

本项目遵循 MIT 开源许可证。
