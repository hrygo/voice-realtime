# Voice Realtime 架构缺陷修复设计

## 状态

已批准。2026-08-20 采用方案 A：`vr-ui` 拥有内嵌交互管道，`vr-interact` 是互斥的
headless 入口。实现遵循 Clean Architecture、DRY、SOLID、TDD 与最小外部依赖原则。

## 目标

一次性修复架构审计确认的运行拓扑、协议接线、算法、资源生命周期、前端控制、安全边界、配置漂移、
测试门禁和文档问题，使默认本地运行链具备可证明的字幕、交互、控制、重启和降级恢复能力。

## 非目标

- 不把系统扩展为远程多用户产品。
- 不引入数据库、消息队列、云服务或新的音频模型。
- 不替换 Pipecat、WhisperLiveKit、LM Studio 或 mlx-audio。
- 不在本轮实现真正的声学回声消除模型；保留半双工与耳机能量门控，但修正其状态和阈值错误。

## 设计原则

### 单一职责

- 入口负责进程启动；`InteractionSession` 负责交互会话；AudioHub 只负责采集与扇出；
  SubtitleProxy 只负责 WLK 连接、音频上行和事件广播。
- 文本回声记录器只记录文本，不再驱动声学闭麦状态。
- 前端控制 hook 只管理命令连接、确认和重连；组件只处理展示与用户意图。

### 依赖倒置

- `InteractionSession` 依赖可注入的 pipeline、runner、worker 和音频队列工厂，便于真实状态测试。
- UI 控制桥依赖明确的运行时协议，而不是 `Any`。
- 外部服务连接均有显式开始、停止、超时和状态接口。

### DRY 与接口收敛

- UI 与 headless 入口共用会话构建、NLTK 自检、超时和关闭逻辑。
- 只有一个音频格式事实源：交互输入固定 16kHz、16-bit、mono；TTS 输出固定 24kHz。
- 删除无法兑现的配置；可兑现的配置必须有运行时消费者和测试。

## 目标运行拓扑

```text
系统麦克风
    │
    ▼
AudioHub (16kHz/s16le/mono)
    ├──► InteractionSession ─► SenseVoice ─► LM Studio ─► TTS Bridge ─► 扬声器
    └──► SubtitleProxy ─► WhisperLiveKit --pcm-input ─► 浏览器字幕

vr-ui：拥有 AudioHub、InteractionSession、SubtitleProxy、状态桥与控制桥
vr-interact：headless 替代入口，复用 InteractionSession，不与 vr-ui 同时运行
```

两个入口通过 `InteractionOwnership` 本机锁互斥。UI 启动失败应区分：

- 所有权冲突：启动失败并报告另一个交互入口正在运行。
- 麦克风失败：UI 可启动，但交互与字幕标记为 degraded。
- WLK 失败：SubtitleProxy 后台重连，交互链继续运行。
- LM Studio/TTS 失败：会话保持可重启，状态面报告具体依赖失败。

## 交互会话与生命周期

新增应用服务 `InteractionSession`，提供：

```python
async def start(*, persona: str | None = None, duplex_mode: DuplexMode | None = None) -> None
async def stop(reason: str) -> None
async def restart() -> None
async def clear_context() -> None
def set_persona(persona: str) -> None
def set_duplex_mode(mode: DuplexMode) -> None
@property
def state(self) -> InteractionSessionState
```

模块边界固定为：

- `interaction/session.py`：`InteractionSession`、状态枚举与会话生命周期；
- `interaction/ownership.py`：基于 `fcntl.flock` 的 macOS 本机所有权锁，锁文件为
  `~/Library/Caches/voice-realtime/interaction.lock`，不受当前工作目录影响；
- `ui/runtime.py`：组合 AudioHub、InteractionSession 与 SubtitleProxy，不复制会话逻辑；
- `ui/protocol.py`：控制命令、响应与状态快照的严格 Pydantic 模型。

行为约束：

- `start()` 先执行 `ensure_punkt_tab()`，失败时给出可操作错误，不静默继续到运行期失败。
- 保留 `WorkerRunner` 实例并调用 `runner.end()`；任务取消只作为带超时的最后兜底。
- 停止交互时暂停向交互队列写入并清空既有音频；重启从新采集帧开始。
- `max_session_seconds` 在 UI 和 headless 两种入口都生效。
- persona 与 duplex 是会话状态；任何重建都必须重放当前状态。
- runner 自然退出或异常时立即清理 worker/task，并向 observer 发布 stopped/degraded 状态。
- LLM/TTS/HTTP 客户端通过处理器停止钩子关闭，重启不得遗留连接或生成任务。

## 音频采集与背压

- 明确 `chunk_frames=512`，对应 1024 字节、约 32ms；日志和注释统一使用帧而非字节。
- AudioHub 以一个有界分发队列和固定 dispatcher 任务扇出，禁止每个音频块无界创建任务。
- sink 仍彼此隔离；慢 sink 采用各自有界队列与明确 drop-oldest 计数，不阻塞采集线程。
- 音频线程只有在 PyAudio stream 成功打开后才报告启动成功；打开失败必须传播给调用方。
- `mic_muted` 是 AudioHub 运行状态：静音时不向任何 sink 分发真实音频，并清空交互队列。
- 交互采样率固定为 16000；不支持的配置在启动前拒绝，禁止给 16k 音频贴上其他采样率标签。

## 回声与打断算法

### 声学状态

- `EchoState` 只由 `TTSStarted/TTSStopped/BotStartedSpeaking/BotStoppedSpeaking/Interruption`
  等真实播放控制帧更新。
- `BotTextRecorder` 不得调用 `on_tts_started()`；LLM 生成期间麦克风保持可用。
- 共享状态只保留一个观察处理器写入，其他策略只读取，避免同一帧多次改变状态。

### 耳机模式

- 建峰与插话判定使用明确的窗口统计；触发阈值必须高于重锁阈值。
- 重锁要求连续低于基线附近阈值的帧，不能把刚达到插话阈值的正常人声算作安静。
- 模式切换重置所有包络和 streak，防止旧模式状态污染。

### 文本回声兜底

- 恢复 `min_chars` 语义；单字与常见短应答不直接丢弃。
- 只有在机器人实际播报窗口或尾部窗口内，且文本相似度满足严格条件时才拦截。
- 命中时发送可观测事件和计数；测试覆盖“好的/是/不”等自然回答不被误杀。

## 字幕链

### PCM 契约

- `wlk serve` 必须带 `--pcm-input`；这是服务端模式，不能依赖未消费的 WS query 参数。
- SubtitleStream URI 不再声称 query 可以切换 PCM；客户端依据启动契约发送 s16le。
- 本地 `model_dir` 存在时使用它；缺失时在离线默认模式下 fail-fast，不隐式下载。
  新增 `SubtitleSettings.allow_model_downloads=False`，只有显式改为 `True` 才允许以
  `model_size` 作为联网 fallback。

### 快照与去重

- Proxy 广播 full snapshot，去重键为 confirmed lines 指纹与 `buffer_transcription` 的组合。
- 历史 confirmed 不得阻止新 partial 广播。
- CLI 事件消费者能提取一次快照中新出现的全部 confirmed 行，而非只取最后一行。
- confirmed 后重置 partial 跟踪状态。

### 重连

- SubtitleProxy 使用一个显式状态机：`stopped → connecting → connected → backoff → connecting`。
- 指数退避有上限并可取消；重连后复用浏览器订阅，不要求刷新页面。
- 浏览器无订阅时丢弃而非积累音频；恢复订阅只发送未来音频。
- 慢或断开的浏览器客户端不阻塞 WLK 接收循环，并从客户端集合中清理。
- 每次 confirmed 快照以临时文件加原子替换写入 `output_dir/current.srt`；会话停止时按时间戳
  归档，保证 `output_dir` 具有真实的服务端落盘语义。

## TTS 桥与 LLM 资源

### TTS

- TTS 原生输出固定为 24000Hz；移除伪可配置采样率选项或严格拒绝其他值，不做假重采样。
- `SpeechRequest.voice` 按请求生效；未指定时使用 engine 当前默认音色。
- 模型生成通过单一并发门串行化，避免多个线程同时访问 MLX 模型。
- 线程到异步端使用有界队列；客户端取消后设置停止信号，生产者不再积压 PCM。
- PCM 保持真流式。WAV 端点允许为写入正确长度而缓冲一次，但删除双重聚合并明确其非低延迟用途。
- PCM 生成异常不能伪装成成功的静默截断；在首块前失败返回 5xx，首块后失败记录结构化错误并终止。

### LLM

- `LmStudioNativeLLMService` 提供关闭钩子，关闭自建 `httpx.AsyncClient`。
- 配置显式 connect/read/write/pool timeout；流式读取允许正常本地生成时长。
- SSE 错误事件和非法 content 形成 ErrorFrame/异常，不再静默返回空回复。
- 继续严格使用 `/api/v1/chat` 和 `reasoning:"off"`，不改变项目关键约束。

## 控制协议与前端

### 命令协议

请求：

```json
{"request_id":"uuid","cmd":"set_voice","voice":"warm"}
```

响应：

```json
{"request_id":"uuid","cmd":"set_voice","ok":true,"state":{"voice":"warm"}}
```

失败响应使用稳定错误码和用户可读消息，不返回内部异常文本。控制连接建立后先下发完整运行状态：
pipeline、subtitle、micMuted、persona、voice、duplexMode。

### 前端行为

- 事件 WS 与控制 WS 复用同一可取消的指数退避实现；组件卸载后不得再次重连。
- persona、voice、duplex、mute 只有收到成功确认后才持久化；失败则保持服务端真值。
- 页面重载时以服务端状态为准，localStorage 仅作为尚未连接时的展示缓存。
- 麦克风静音发送真实控制命令，并反映 AudioHub 状态。
- 助手相位状态机：`user_speaking→listening`、`user_silence/STT final→thinking`、
  `TTS started→speaking`、`TTS stopped→idle`；异常/停止进入 degraded/stopped。
- 状态灯区分“浏览器 WS 已连接”与“后端组件健康”，不再用一个绿色状态代表全部链路。
- 会话计时器使用服务端 `session_started_at`，停止、重启和页面刷新后都与真实会话一致。
- SRT 时间解析同时接受 `HH:MM:SS.mmm` 与 `HH:MM:SS,mmm`，导出统一为逗号毫秒格式。

## 安全边界

本项目保持 loopback-only，不新增远程认证系统：

- UI、TTS 与 WLK 的 host 配置统一拒绝非 loopback 地址；远程部署不属于本项目支持范围。
- WebSocket 校验 `Origin`，仅允许当前 UI origin 和显式本地开发 origin。
- 增加 CSP、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、
  `X-Frame-Options: DENY`。
- 控制 payload 使用 Pydantic 判别联合或等价严格 schema，限制字符串长度并拒绝多余字段。
- 对浏览器返回稳定错误，不泄漏内部路径、异常或外部服务响应正文。

这解决恶意网页跨站连接 localhost 的威胁；非浏览器本机进程仍属于本项目的可信本机边界。

## 配置收敛

- Python classifier 与 mypy 均改为 3.12。
- `InteractionSettings.sample_rate` 固定验证为 16000。
- `BridgeSettings.sample_rate` 固定验证为 24000。
- 删除重复且无法生效的 `InteractionSettings.tts_voice`，音色唯一事实源为 Bridge/控制状态。
- 删除无消费者的 `interrupt_echo_suppression_ms`。
- 删除 WLK CLI 不支持的 `SubtitleSettings.device`。
- `model_size` 只在 `allow_model_downloads=True` 且无 `model_dir` 时作为明确 fallback；
  默认离线模式要求目录存在。
- `output_dir` 用于字幕会话导出和启动日志，统一由 SubtitleProxy/launcher 消费。
- 删除 AudioHub、SubtitleProxy 中无消费者的 `_queue_size/_audio_sinks/_paused` 等遗留状态，
  或把它们落实为上述有界队列/状态机；不保留假扩展点。

## 可观测性与健康

`/health` 只表示 UI HTTP 进程存活；新增运行状态响应覆盖：

- AudioHub：stopped/starting/running/muted/error；
- InteractionSession：stopped/starting/running/stopping/error/ownership_conflict；
- SubtitleProxy：stopped/connecting/connected/backoff/error；
- 外部依赖：WLK/TTS/LM 目标服务状态。

状态观察器按 turn 重置时间戳。指标名称改为真实语义：endpoint→STT final、STT final→LLM first token、
LLM first token→first TTS audio、endpoint→first audio。没有足够事件时字段为 null，不使用旧轮次时间戳或伪零值。
字幕与助手广播共用有界的 per-client broadcaster；慢客户端只能丢失自己的旧状态快照，不能阻塞
管道 observer 或 WLK 接收任务。

## 测试策略

### 单元测试

- 所有缺陷先建立失败测试，再实现修复。
- 覆盖 InteractionSession 状态机、队列清理、所有权冲突、persona/duplex 重放和超时。
- 覆盖回声状态单写者、耳机触发/重锁、短回答和真实回声文本。
- 覆盖字幕 full snapshot partial 更新、多个 confirmed、退避取消与慢客户端。
- 覆盖 TTS per-request voice、生成串行、取消、有界队列与单次 WAV 聚合。
- 覆盖 WS Origin、命令 schema、稳定错误和完整状态握手。
- 修复现有测试中的未等待协程；门禁运行不得产生项目代码导致的 RuntimeWarning。

### 集成测试

- 使用真实 FastAPI lifespan 与内存 fake 外部服务验证 UI 启停和命令确认。
- 使用本地 fake WebSocket server 验证 SubtitleProxy 断线重连和 PCM 字节透传。
- 使用真实 Pipecat frame 类验证 stop/restart 不回放旧帧，避免只验证 mock 调用次数。

### 前端测试

- 引入轻量 Vitest，仅测试纯 reducer、控制命令状态和 WebSocket 重连；不引入大型 E2E 框架。
- TypeScript build 继续作为门禁。

### 完成门禁

```bash
uv run pytest tests/ --cov=src --cov-report=term-missing
uv run mypy src/
uv run ruff check src/ tests/
npm test -- --run
npm run build
```

`pyproject.toml` 的 pytest `addopts` 同步启用 coverage 与 `fail_under=80`，确保文档中的默认
`uv run pytest tests/` 也实际执行覆盖率门禁；关键入口 `runner.py`、字幕重连和控制协议不得继续为
零覆盖或仅 mock 调用覆盖。

另外执行本机运行级验收：

1. `vr-ui` 与 `vr-interact` 互斥；
2. WLK 进程参数包含 `--pcm-input` 且日志无 FFmpeg PCM 解析错误；
3. 首个 confirmed 后 partial 继续刷新；
4. stop 后等待十秒再 restart，不产生旧音频转写；
5. 控制 WS 断开重连后 persona、voice、duplex、mute 与服务端一致；
6. 外放模式无自回声循环，耳机模式可在 LLM 生成期和 TTS 播放期插话；
7. 恶意 Origin 的 WS 握手被拒绝。

## 并行实施边界

获批进入实现后采用三个子代理与主 Agent，文件所有权互斥：

- 字幕子代理：`subtitles/`、`ui/subtitle_proxy.py` 及对应测试。
- TTS/LLM 子代理：`tts_bridge/`、`interaction/reasoning.py` 及对应测试。
- 前端子代理：`ui/src/`、前端测试与 `ui/package*.json`。
- 主 Agent：`audio/`、`interaction/pipeline.py`、共享 InteractionSession、`ui/runtime.py`、
  `ui/control.py`、`ui/server.py`、配置、拓扑互斥、整合测试和文档。

任何跨边界接口变更由主 Agent 先定义；子代理不得修改他人所有文件，也不得回退现有未提交改动。
