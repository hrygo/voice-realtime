# voice-realtime

全本地实时语音交互、实时字幕与会议助手系统，面向 Apple Silicon、中文优先、离线运行。
Python 严格锁定 `>=3.12,<3.13`。

## 当前架构

`vr-ui` 是默认交互所有者：它独占麦克风，通过 `AudioHub` 把 16kHz mono int16 PCM
有界扇出到 Pipecat 交互管道和 WhisperLiveKit 字幕代理。

```text
麦克风 → vr-ui :8100 / AudioHub
              ├─ AudioInjector → SenseVoice → LM Studio :1234
              │                              → vr-bridge :8765 → 扬声器
              └─ PCM WebSocket → vr-subtitles :8001
                                  ├─ 普通字幕 → 快照 + SRT
                                  └─ 会议模式 → 实时文字 → PostgreSQL → AI 纪要
```

运行单元只有四个：

- `vr-ui`：Web 控制台、麦克风采集、交互管道、状态与控制协议。
- `vr-subtitles`：WhisperLiveKit/Qwen3-ASR 字幕服务，必须启用 `--pcm-input`。
- `vr-bridge`：Qwen3-TTS 的 OpenAI 兼容桥，固定输出 24kHz。
- LM Studio：加载 `qwen/qwen3.6-35b-a3b`，原生 `/api/v1/chat` 推理。

`vr-interact` 是无 UI 的替代入口，与 `vr-ui` 共用 `InteractionSession` 和跨进程所有权锁；
二者不能同时运行，不能把它当成第五个并行服务。

## 启动

先确认本地模型已缓存，再分别启动四个运行单元：

```bash
uv sync --all-extras
uv run vr-bridge
uv run vr-subtitles
uv run vr-ui
```

LM Studio 在 `localhost:1234` 单独启动并加载目标模型。打开
`http://127.0.0.1:8100` 使用 Voice Studio。

如只需要 headless 交互，停止 `vr-ui` 后运行：

```bash
uv run vr-interact
```

## 运行语义

- 服务监听地址强制为 loopback；浏览器 WebSocket 还会校验本机 Origin。
- 控制连接先接收完整状态握手；命令带 `request_id`，只有成功确认后前端才持久化状态。
- 麦克风静音是真实的服务端静音，会阻断扇出并清空交互队列。
- 字幕代理消费 WhisperLiveKit 全量快照，断线后指数退避重连；confirmed 字幕原子写入
  `runtime/subtitles/current.srt`，停止时归档。
- 默认禁止交互 STT 与字幕 ASR 隐式下载模型；缓存/模型目录缺失时启动会立即报错。
- TTS 请求可按请求指定 voice；MLX 生成串行、有界，并在取消后停止继续积压。

## 会议助手

会议模式与语音助手互斥：开始会议会停止完整 Pipecat/LLM/TTS 交互链路，只保留麦克风到
WhisperLiveKit 的实时转录；结束会议执行 EOF 冲刷，随后保持 `idle` 并异步生成 AI 纪要。
会议模式不保存音频，也不写旧字幕 SRT；PostgreSQL 是 confirmed 转录、说话人映射和纪要的事实源。

首次使用由本机 PostgreSQL 管理员创建最小权限角色和独立 schema：

```bash
psql knowledge -f scripts/bootstrap-meeting-db.sql
```

等价 SQL 为：

```sql
CREATE ROLE voice_realtime_app LOGIN;
CREATE SCHEMA voice_realtime AUTHORIZATION voice_realtime_app;
REVOKE ALL ON SCHEMA voice_realtime FROM PUBLIC;
GRANT CONNECT ON DATABASE knowledge TO voice_realtime_app;
GRANT USAGE, CREATE ON SCHEMA voice_realtime TO voice_realtime_app;
```

然后配置并启动；`vr-ui` 会在 advisory lock 保护下自动执行前向 migration：

```bash
export VR_MEETING_DATABASE_URL='postgresql://voice_realtime_app@/knowledge'
export VR_MEETING_SCHEMA='voice_realtime'
export VR_SUBTITLE_DIARIZATION_MODEL_PATH='runtime/sortformer.nemo'
uv run vr-ui
```

独立前端使用 `/api/v1/*`、`/ws/v1/control` 和 `/ws/v1/meetings`；唯一契约位于
[`contracts/meeting-assistant/v1`](contracts/meeting-assistant/v1)。完整部署、恢复和联调说明见
[`docs/会议助手后端运行与前后端联调.md`](docs/会议助手后端运行与前后端联调.md)。

## 状态与诊断

- `GET /health`：UI 进程存活。
- `GET /api/runtime`：交互、字幕、静音、persona、voice、duplex 和会话开始时间。
- `GET /api/services`：WhisperLiveKit、TTS 桥、LM Studio 探活及目标 LLM 是否已加载。
- `GET /v1/voices`：代理 TTS 桥音色列表。
- `GET /api/v1/runtime`：版本化运行模式与活动会议状态。
- `GET /api/v1/meetings`：会议历史游标分页；详情、转录、纪要、导出和删除均为其子资源。

## 质量门禁

```bash
uv run pytest tests/
uv run mypy src/
uv run ruff check src/ tests/
cd ui && npm test -- --run
cd ui && npm run build
cd ui && npm audit --audit-level=high
```

默认 pytest 已启用分支覆盖率，最低门槛为 80%。

更多设计依据见：

- [`docs/实时语音交互与字幕-方案与最佳实践.md`](docs/实时语音交互与字幕-方案与最佳实践.md)
- [`docs/Voice-Studio-UI-设计方案.md`](docs/Voice-Studio-UI-设计方案.md)
- [`docs/架构图与流程图.md`](docs/架构图与流程图.md)
- [`docs/decisions/0001-single-owner-interaction-runtime.md`](docs/decisions/0001-single-owner-interaction-runtime.md)
