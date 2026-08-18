# Voice Studio UI — 未完成任务交接（2026-08-18）

> 下班交接。方案定稿见 `docs/Voice-Studio-UI-设计方案.md`。当前处于 M3 起点。

## 已完成的里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M1 骨架** | `config.py` + `UISettings`；`ui/server.py` FastAPI（/health、/api/services 服务灯聚合、/ws 骨架、静态托管）；`pyproject.toml` `vr-ui` 入口；`tests/test_ui_server.py`（5 测试）；`ui/` React+Vite+TS 工程（StatusBar 组件） | ✅ 门禁绿 |
| **M2 字幕** | `audio/hub.py` **AudioHub**（PyAudio 16k/16bit/mono 采集 + asyncio 扇出 + throttle 注入防忙轮询）；`ui/subtitle_proxy.py` **SubtitleProxy**（单 wlk 连接：`send_audio`+收事件+去重+multi-cast）；React 侧 `useEventSocket.ts`（指数退避重连）+ `subtitleStore.ts`（含 SRT 导出）+ `SubtitleStream.tsx` 组件；`tests/test_audio_hub.py`（9 测试）、`tests/test_subtitle_proxy.py`（6 测试） | ✅ 门禁绿 |

## 未完成任务（按顺序）

### M3：助理状态桥 + 管道音频注入（①③已启动过研究）
- [ ] **M3-1** `audio/audio_injector.py`：`AudioInjector(FrameProcessor)` 管道首节点，从 AudioHub 队列取块推 `InputAudioRawFrame`
  - `pipeline.py` 改造：`LocalAudioTransportParams(audio_in_enabled=False)`（输入停用）+ 在 `transport.input()` 前/替换首节点挂 AudioInjector
  - **既有调优必须保留**：VAD 参数、`silence_secs`(0.45)、`EchoSuppressionProcessor`、ttfs——AGENTS.md CRITICAL 约束
  - 注意：不要加载 `pipecat.transports.base_input` 的实验特性，遵循 06a 官方组装模式
- [ ] **M3-2** `ui/assistant_bridge.py`：`StatusBridgeObserver(BaseObserver)` 捕获 `TranscriptionFrame/LLMTextFrame/TTSAudioFrame/UserStartedSpeakingFrame/BotStartedSpeakingFrame/InterruptionFrame`，序列化为 WS 事件（见方案 §5 协议），节流（TTSAudioFrame 仅计数）；`PipelineWorker(pipeline, observers=[...])`
  - 参考 pipecat 官方 `examples/observability/observability-observer.py` 注册方式
- [ ] **M3-3** server.py 接入：`/ws/subtitles` 骨架换成真实 SubtitleProxy 事件流；`/ws/assistant` 换成 observer 广播；生命周期管理（AsyncExitStack）
- [ ] **M3-4** React：`AssistantPanel.tsx`（StateLights 三态 👂/🧠/🗣 + 波形 canvas rAF + TranscriptView 气泡 + Controls 基础三键）
- [ ] **M3 验证点（关键）**：`LLMMessagesUpdateFrame` 对自定义 `LmStudioNativeLLMService` 的兼容性（M4 清空上下文依赖）；失败则退回"清空=重启会话"

### M4：控制扩展集
- [ ] `ui/control.py` ControlBridge：`clear_context`(queue_frame LLMMessagesUpdateFrame)、`stop_session`(WorkerRunner.end)、`restart`(spawn vr-interact)、`set_persona`、`set_voice`
- [ ] TTS 桥新端点 `POST /v1/voice`（`tts_bridge/server.py` + `engine.py` 热切换 VoiceDesign profile）；测试 `tests/test_server.py` 补用例
- [ ] React：人格编辑器 dialog + 音色下拉 + 主题（亮/暗/系统）+ 联动（助手说话时字幕高亮）
- [ ] 音色列表来源：引擎当前支持的 profile（读 `engine.py` 确认，勿发明）

### M5：端到端实测
- [ ] 双模块同时运行（AudioHub 单源扇出 → Pipecat + wlk，macOS 多路采集相容性首验）
- [ ] 断连重连恢复（wlk 重启后 SubtitleProxy 重连）、打断（barge-in）、长会话
- [ ] 全量门禁绿

## 关键代码位置

| 文件 | 说明 |
|---|---|
| `src/voice_realtime/audio/hub.py` | AudioHub（已完成） |
| `src/voice_realtime/ui/subtitle_proxy.py` | SubtitleProxy（已完成） |
| `src/voice_realtime/ui/server.py` | FastAPI 服务（/ws 骨架待 M3 填充） |
| `src/voice_realtime/interaction/pipeline.py` | build_pipeline（M3 改 transport input 停用 + AudioInjector） |
| `ui/src/components/SubtitleStream.tsx` | 字幕组件（已完成） |
| `ui/src/stores/subtitleStore.ts` | zustand store + SRT（已完成） |

## 质量门禁（提交前必须全绿）

```bash
uv run pytest tests/            # 当前 90+new 全过（新增 15 个 UI/audio 测试）
uv run mypy src/                # strict；已加 pyaudio 走 `# type: ignore[import-untyped]`
uv run ruff check src/ tests/   # clean（E501 注意 100 列；中文文本行谨慎断行）
```

## 注意（防回退）

1. **pyaudio 已加入 `interaction` 依赖组**（`pyproject.toml`），本机 portaudio 已装（brew）
2. AudioHub `throttle_secs` 默认 0.0（生产由 PyAudio 阻塞节流）；**勿删空转分支**（防忙轮询高 CPU，有测试锁定）
3. LM Studio 推理开关仅原生端点（AGENTS.md CRITICAL 1）；TTS 桥 422 先查 `model` 字段（CRITICAL 4）
4. 未提交 git：工作区有一批未 commit 改动（M1+M2 全部）——下次开工先 `git status` 核对，按「小步提交」规范分组 commit
5. 测试禁用 warnings 显示用 `-p no:warnings`（pipecat/fastapi 有第三方 DeprecationWarning 噪音）