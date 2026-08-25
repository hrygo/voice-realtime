# 运行时语音工作负载仲裁设计

## 1. 文档状态

- 设计日期：2026-08-25（Asia/Shanghai）
- 状态：会话方案已批准，书面规格待复核
- 决策记录：[`ADR-005`](../../decisions/0005-server-side-runtime-workload-arbitration.md)
- 代码基线：`main`，`HEAD=407c980ec9be5d4473256931af5cb58f93a51f9c`
- 图证据：`Users-hrygo-Documents-voice-realtime`，generation
  `2026-08-25T13:29:05Z`，相关路径均无已记录覆盖缺口；该信号为 best-effort
- 运行态证据：`runtime/` 日志和本机进程快照直接读取，不属于知识图覆盖范围

## 2. 结论先行

当前标准单浏览器流程已在进入字幕 Tab 时停止助手，并只在字幕 Tab 挂载字幕 WebSocket，不能简单
归类为“设计上始终双 ASR 并发”。真正缺口是这种互斥由前端尽力维持，而服务端只认识
`assistant / meeting / idle`，无法约束控制命令失败、快速切换、多浏览器和直接 WebSocket 客户端。

本设计把普通字幕提升为服务端一等模式，由 `RuntimeModeCoordinator` 原子仲裁
`assistant / subtitles / meeting / idle`。WLK、TTS 与 LM Studio 服务进程保持常驻；状态机只切换
活跃连接和 PCM 所有权，确保任意时刻最多一个麦克风推理工作负载。

这项修改解决应用内部的结构性资源竞争。它不会承诺在《博德之门 3》等高负载程序运行时仍保持同样
实时性；外部负载被纳入诊断和运行建议，而不是由应用自动处置。

## 3. 当前事实与问题边界

### 3.1 已存在的正确行为

1. `AudioHub` 独占麦克风并通过有界队列扇出，不能让新模式直接打开第二路麦克风。
2. 前端 `App.handleTabChange()` 在进入字幕 Tab 时发送 `stop_session`，进入助手 Tab 时发送
   `start_assistant`。
3. `SubtitleStream` 只在字幕 Tab 挂载；离开 Tab 后浏览器字幕 WebSocket 正常卸载。
4. `SubtitleProxy.push_audio()` 在没有浏览器客户端时不发送普通字幕 PCM；会议租约不依赖浏览器客户端。
5. 会议模式已在服务端停止交互链路，并由 `SubtitleProxy.begin_capture()` 独占转录流。
6. ASR 契约、adapter、registry 已存在；本设计不重新设计 ASR 后端接口。

### 3.2 服务端互斥缺口

当前前端先执行 `setActiveTab()`，再异步发送控制命令；以下任一条件成立时，字幕组件仍会挂载：

- `commandSocket.ready` 为 false；
- `stop_session` 返回失败；
- 命令尚未完成，字幕 WebSocket 已连接；
- 另一个浏览器窗口随后发送 `start_assistant`。

服务端 `/ws/subtitles` 当前只验证来源并注册客户端，不检查运行模式。`RuntimeModeCoordinator` 又没有
普通字幕依赖，所以服务端无法证明“助手和普通字幕不同时消费 PCM”。

### 3.3 运行态证据

2026-08-25 现场观测包括：

- WLK 进程仍监听 `:8001` 时，HTTP `/health` 可超时；
- WLK `lag` 从约 `0.03s` 增长到几十秒并出现客户端断开、清理和重连；
- 交互 SenseVoice 出现过高于实时的 RTF；
- TTS 取样返回 HTTP 200，但音频源块出现过约 `254ms` 间隔；
- 《博德之门 3》进程观测到约 `165%–283%` CPU，占用与异常时间重叠；
- 系统没有记录热告警，因此现有证据更支持资源争抢，而非已确认热降频。

以上证据证明外部负载会放大问题，但不能据此声称游戏是唯一原因。应用内部仍必须建立可证明的资源
所有权。

## 4. 目标与非目标

### 4.1 目标

1. 服务端成为语音工作负载模式的唯一事实源。
2. 任意时刻最多一个麦克风推理工作负载消费新 PCM。
3. 普通字幕、助手和会议之间的切换原子化、可回滚、可观测。
4. 保留 WLK 进程预热，模式切换不触发模型重新加载。
5. 多浏览器和直接 WebSocket 客户端不能绕过互斥。
6. 模式切换后不处理来源模式遗留 PCM。
7. 健康面区分进程可达、WebSocket 状态、工作负载 ready 与数据新鲜度。
8. 保持会议 EOF、gap、PostgreSQL、SRT 和零音频持久化约束。

### 4.2 非目标

- 不更换 Qwen3-ASR、SenseVoice、Sortformer、Qwen3-TTS 或 LM Studio 模型。
- 不实现生产 ASR 后端热切换；既有科学测试 registry 不转化为运行时切换产品功能。
- 不让 `vr-ui` 接管 WLK、TTS 或 LM Studio 服务进程生命周期。
- 不增加第二个 WLK 实例或动态切换 diarization。
- 不自动扫描、结束、暂停或限速游戏及其他用户进程。
- 不在本阶段增加 TTS 预缓冲或修改 `chunk_ms`；先完成资源隔离和节奏观测。
- 不改变麦克风选择逻辑、回声双层防线或会议数据模型。

## 5. 核心不变量

| 模式 | 交互 PCM | 普通字幕 PCM | 会议 PCM | LLM/TTS | 允许的字幕事件订阅 |
|---|---:|---:|---:|---:|---|
| `assistant` | 是 | 否 | 否 | 是 | 否 |
| `subtitles` | 否 | 是 | 否 | 否 | 是 |
| `meeting` | 否 | 否 | 是 | 否 | 是，只读观察会议转录 |
| `idle` | 否 | 否 | 否 | 否 | 否 |

额外不变量：

1. `interaction.active`、`subtitle_proxy.browser_capture_active`、
   `subtitle_proxy.capture_owner is not None` 三者最多一个为真。
2. 模式值只在目标工作负载 ready 后更新；失败时保持或恢复来源模式。
3. 每次离开来源模式都清空其应用层音频队列，并停止对应发送任务。
4. `meeting` 的 EOF 冲刷完成或超时后才释放会议捕获租约。
5. 浏览器订阅数量不决定模式；订阅断线不能隐式启动、停止或抢占工作负载。

## 6. 目标架构

```text
浏览器控制 WS
  └─ start_assistant / start_subtitles / start_meeting / stop_active_mode
       └─ RuntimeModeCoordinator（唯一模式锁与补偿回滚）
          ├─ InteractionSession
          │    └─ SenseVoice → LM Studio → Qwen3-TTS
          ├─ SubtitleWorkload
          │    └─ SubtitleProxy 普通字幕连接 → WLK
          └─ MeetingSession
               └─ SubtitleProxy 会议租约 → WLK → PostgreSQL

AudioHub（唯一麦克风）
  ├─ interaction sink ── UIRuntime 模式门控 ──► InteractionSession
  └─ subtitle sink ───── UIRuntime 模式门控 ──► SubtitleProxy

浏览器字幕 WS
  └─ 只读订阅 SubtitleProxy；不拥有模式写权限
```

### 6.1 `SubtitleWorkload` 窄接口

`RuntimeModeCoordinator` 不依赖 WLK 或 UI 细节，只消费窄接口：

```python
from typing import Protocol


class SubtitleWorkload(Protocol):
    @property
    def browser_capture_active(self) -> bool: ...

    async def activate_browser_capture(self, *, timeout_secs: float) -> None: ...

    async def deactivate_browser_capture(self) -> None: ...
```

生产实现由 `SubtitleProxy` 提供；测试使用 fake workload，验证调用顺序和回滚。

### 6.2 WLK 进程与连接边界

`run-all.sh` 继续并行启动 `vr-subtitles`。由于模型加载约需几十秒，UI 启动和助手可先完成；
`/api/services` 在此期间显示 `starting/unreachable`，但不阻塞助手模式。

`SubtitleProxy.start()` 只完成目录、任务状态和诊断初始化，不再自动调用普通字幕 supervisor。
`activate_browser_capture()` 才建立 `/asr` WebSocket，并等待下列条件：

1. TCP/WebSocket 建立成功；
2. 收到 WLK ready/config 事件；
3. 当前没有会议捕获租约；
4. 激活没有被 shutdown 或后续模式切换取消。

只有四项全部满足才将 `browser_capture_active=True`。`deactivate_browser_capture()` 必须取消发送与接收
任务、关闭 stream、清空 `_audio_buffer`、清除 ready，并进入 `paused`。

## 7. 服务端状态机

### 7.1 模式枚举

```python
class RuntimeMode(StrEnum):
    ASSISTANT = "assistant"
    SUBTITLES = "subtitles"
    MEETING = "meeting"
    IDLE = "idle"
```

### 7.2 转换表

| 来源 | 命令 | 目标 | 执行顺序 | 失败语义 |
|---|---|---|---|---|
| `assistant` | `start_subtitles` | `subtitles` | stop interaction → drain → activate subtitles → commit | activate 失败则 restart interaction；恢复失败进入 `idle` |
| `idle` | `start_subtitles` | `subtitles` | activate subtitles → commit | 保持 `idle` |
| `subtitles` | `start_assistant` | `assistant` | deactivate subtitles → drain → start interaction → commit | start 失败则 reactivate subtitles；恢复失败进入 `idle` |
| `idle` | `start_assistant` | `assistant` | start interaction → commit | 保持 `idle` |
| `assistant` | `start_meeting` | `meeting` | stop interaction → meeting start → commit | meeting 失败则 restart interaction |
| `subtitles` | `start_meeting` | `meeting` | deactivate subtitles → meeting start → commit | meeting 失败则 reactivate subtitles |
| `meeting` | `end_meeting` | `idle` | EOF/finalize → release capture → commit | 保持既有 interrupted/finalization timeout 语义，最终进入 `idle` |
| 任意活动模式 | `stop_active_mode` | `idle` | 停止对应工作负载 → drain → commit | 尽力释放；失败返回错误且不得谎报活动工作负载已停止 |

幂等规则：

- 已在 `assistant` 且 interaction active 时，`start_assistant` 返回成功且不重建管道。
- 已在 `subtitles` 且 browser capture active 时，`start_subtitles` 返回成功且不重连。
- `meeting` 中重复开始任一模式均返回 `mode_conflict`。
- 转换执行期间的并发命令由同一把锁串行处理；后到命令根据最新已提交模式重新判断。

### 7.3 模式提交与回滚

协调器在转换开始时保存来源工作负载身份，不提前修改 `_mode`。执行目标启动成功后再一次性更新：

```text
_mode = target
_runtime_revision += 1
```

目标失败后执行来源补偿。补偿成功则 `_mode` 保持来源值；补偿失败时：

- 尽力停止所有工作负载；
- `_mode = idle`；
- `_runtime_revision += 1`；
- 返回稳定错误码 `service_unavailable`；
- 结构化日志记录目标错误和补偿错误，不记录音频、凭据或完整外部响应。

## 8. 控制协议与前端行为

### 8.1 新控制命令

在现有严格 `request_id` 协议中增加：

```json
{"request_id":"...","cmd":"start_subtitles"}
```

成功 ack 继续返回统一 runtime state，其中 `mode="subtitles"`。失败使用现有错误包络和
`mode_conflict`、`service_unavailable`，不新增第二套错误格式。

### 8.2 前端切换顺序

`App` 不再先提交 `activeTab`。非会议状态下：

1. 设置 `pendingTab` 并禁用重复切换；
2. 发送目标模式命令；
3. 等待匹配 `request_id` 的 ack；
4. 验证 ack 中 `state.mode` 等于目标模式；
5. 提交 `activeTab`，此时目标面板才挂载；
6. 失败时保留原 Tab，并显示本地错误提示。

会议状态事件仍可强制切换到会议 Tab，但后端会议模式已经先提交，前端只反映事实。

### 8.3 字幕 WebSocket

`/ws/subtitles` 的语义固定为只读事件订阅：

- `mode=subtitles`：接受并接收普通字幕；
- `mode=meeting`：接受并接收会议捕获广播，不创建第二条 WLK 流；
- `mode=assistant/idle`：关闭码 `4409`，reason 使用固定非敏感文本“字幕模式未激活”；
- 客户端断开只移除广播队列，不改变全局模式；
- 多客户端共享同一 `SubtitleProxy` 连接，每个客户端继续使用独立有界发送队列。

## 9. PCM 门控与队列语义

### 9.1 UIRuntime 双重门控

`AudioHub` sink 保持常驻，但回调必须在进入下游前校验模式：

```text
_enqueue_audio:
  仅 mode=assistant 且 interaction active 时写 interaction queue

_push_subtitle_audio:
  mode=subtitles 时写普通字幕流
  mode=meeting 时写会议捕获流
  其他模式直接丢弃
```

`SubtitleProxy.push_audio()` 保留现有自身状态校验作为第二道防线，不能只信任调用方。

### 9.2 切换屏障

离开模式时按以下顺序建立屏障：

1. 停止来源工作负载接受新 PCM；
2. 清空来源应用队列；
3. 取消来源发送任务并关闭 stream；
4. 启动目标工作负载；
5. 目标 ready 后提交模式。

旧音频不得被发送到目标模式。普通字幕不要求持久化切换期间丢弃的音频；会议模式仍按现有 gap 和
EOF 规则处理，不能静默丢弃已接受的会议音频。

## 10. 观测与健康模型

### 10.1 进程健康与工作负载健康分离

`/api/services` 的 WLK 项保留 HTTP 探活，并增加不破坏旧字段的诊断字段：

```json
{
  "name": "wlk",
  "status": "ok",
  "url": "http://127.0.0.1:8001/health",
  "workload": "paused",
  "ws_state": "paused",
  "reconnect_count": 0,
  "last_event_age_ms": null
}
```

字段语义：

- `status`：仅表示 HTTP 进程可达性，沿用 `ok/unreachable/timeout/error`；
- `workload`：`paused/starting/ready/degraded/error`；
- `ws_state`：`paused/connecting/connected/backoff/error/meeting`；
- `last_event_age_ms`：只在活跃工作负载存在且收到过事件后给出；
- `reconnect_count`：当前应用生命周期内普通字幕和会议连接重连累计值。

禁止通过抓取 WLK 日志推导应用健康。若 vendor 事件没有原生 lag，则使用“发送 PCM 后长期无事件”作为
`degraded` 信号，并明确它是应用层新鲜度，不伪装成 WLK 内部 lag。

### 10.2 音频与 TTS 指标

增加下列本机诊断，不改变音频数据边界：

- `AudioHub` 每个 sink 的 `dropped_chunks`；
- interaction queue 的 `dropped_chunks`；
- SubtitleProxy 普通字幕和会议 buffer 的丢弃/gap 计数；
- TTS 首块耗时、源音频块数量、最大/中位块间隔、超过 `200ms` 的间隔次数；
- 最近一次模式切换耗时、目标、结果和回滚结果。

TTS 指标名称必须使用 `source_chunk_gap`，不能在没有音频设备回调证据时命名为“扬声器 underrun”。

### 10.3 外部负载提示

应用不枚举或终止具体用户进程。只有在活跃工作负载出现持续新鲜度下降、ASR RTF 高于实时或 TTS
源块间隔异常时，UI 显示通用提示：

> 本机推理资源紧张；请关闭高负载应用后重试。

提示不声称具体进程是原因，也不自动调整模型。

## 11. Sortformer 与低负载运行边界

本阶段保持现有 WLK Qwen3-ASR + Sortformer 默认身份，避免同时改变模式调度和模型变量。

现有 `VR_SUBTITLE_DIARIZATION=false` 可作为明确的单说话人低负载启动配置，但它有以下边界：

- 需要重启 `vr-subtitles`；
- 会议将失去匿名 speaker labels；
- 不能在会议开始时动态恢复；
- 不作为产品默认值，也不进入本阶段代码改动。

若完成工作负载仲裁后，关闭外部高负载程序仍无法满足单说话人字幕实时性，再以独立证据评估双 WLK
实例或动态 diarization；不得在本设计中预先建设。

## 12. 错误处理

| 故障 | 行为 | 对外状态 |
|---|---|---|
| WLK 尚未 ready | `start_subtitles` 在超时内失败，恢复来源模式 | `service_unavailable` |
| 普通字幕 WS 中断 | supervisor 退避重连，保持 `subtitles`，工作负载为 `degraded` | 控制面仍可停止或切换 |
| 切到助手时 interaction 启动失败 | 尝试恢复普通字幕 | 恢复成功保持 `subtitles`，否则 `idle` |
| 切到会议时 meeting start 失败 | 恢复来源 assistant/subtitles | 返回原异常映射 |
| 会议 EOF 超时 | 沿用 interrupted/finalization timeout | 最终进入 `idle` |
| 浏览器字幕客户端慢 | 丢弃该客户端旧快照，保留最新快照 | 不影响 WLK 和其他客户端 |
| AudioHub sink 满 | 丢弃最旧块并累计计数 | 诊断标记 degraded，不阻塞采集线程 |
| 应用 shutdown | 先停止 coordinator，再停止 hub/proxy | 不恢复任何来源模式 |

## 13. 数据、安全与隐私约束

- 麦克风仍由 `AudioHub` 单源采集。
- 不新增音频文件、音频数据库字段或网络上传。
- PostgreSQL 仍是会议 confirmed 文本、speaker 映射和纪要唯一事实源。
- 普通字幕只维护现有 SRT；会议模式不写 `current.srt`。
- 模式与诊断日志不得记录 PCM、完整转写正文、模型上下文、凭据或私有环境变量。
- 默认禁止模型下载；模式切换不得触发下载或修改模型配置。
- 控制 WebSocket 继续要求严格 `request_id` 和 loopback/LAN Origin 校验。

## 14. 代码边界与预计修改面

| 文件 | 单一责任变化 |
|---|---|
| `src/voice_realtime/meeting/runtime_mode.py` | 增加 `subtitles` 模式、字幕 workload 依赖、原子转换与补偿回滚 |
| `src/voice_realtime/ui/subtitle_proxy.py` | 增加普通字幕显式 activate/deactivate、ready 和诊断快照 |
| `src/voice_realtime/ui/runtime.py` | 实现模式级 PCM 门控、队列屏障并向 coordinator 注入字幕 workload |
| `src/voice_realtime/ui/protocol.py` | 增加 `StartSubtitlesCommand` 和 additive runtime/diagnostic 字段 |
| `src/voice_realtime/ui/control.py` | 路由 `start_subtitles` 并保持统一 ack/error 包络 |
| `src/voice_realtime/ui/server.py` | 约束字幕 WS 模式、聚合进程与工作负载健康 |
| `src/voice_realtime/audio/hub.py` | 暴露只读 sink drop 诊断，不改变背压策略 |
| `src/voice_realtime/ui/assistant_bridge.py` | 记录 TTS 源块节奏，不改变播放路径 |
| `ui/src/App.tsx` | ack 后提交 Tab，维护 pending/失败状态 |
| `ui/src/protocol.ts` | 接受 `subtitles` 模式和 additive 诊断字段 |
| 对应 `tests/` 与 `ui/src/*.test.tsx` | 冻结转换、回滚、多客户端、门控和 UI 时序 |

不移动 `RuntimeModeCoordinator` 文件，不拆分 `SubtitleProxy`，避免在行为修复中夹带无关重构。

## 15. 测试设计

### 15.1 状态机单元测试

覆盖：

1. 四种模式合法转换与幂等调用；
2. assistant → subtitles 调用顺序；
3. subtitles → assistant 调用顺序；
4. subtitles → meeting 及失败恢复；
5. 目标启动失败、来源恢复成功；
6. 目标和来源恢复都失败后进入 idle；
7. 并发命令被锁串行化且只提交最新合法结果；
8. `runtime_revision` 只在成功提交或强制降级 idle 时增长。

### 15.2 SubtitleProxy 测试

覆盖：

- `start()` 不自动连接；
- activate 等待 ready，重复 activate 幂等；
- deactivate 关闭任务并清空 PCM；
- 会议租约与普通 activate 互斥；
- 重连只在字幕模式持续；
- shutdown 不恢复 supervisor；
- 多客户端共享同一 stream，慢客户端隔离；
- 诊断时间与计数使用单调时钟，可注入 fake clock。

### 15.3 UIRuntime 与服务器测试

覆盖每种模式下两个 AudioHub sink 的放行/拒绝矩阵；验证 `/ws/subtitles` 在 assistant/idle 返回
`4409`，在 subtitles/meeting 接受；验证 `/api/services` 兼容旧字段并增加诊断字段。

### 15.4 前端测试

覆盖：

- 命令 ack 前不切换 Tab、不挂载 `SubtitleStream`；
- 命令失败保留原 Tab 并显示提示；
- 快速重复点击只存在一个 pending 转换；
- meeting 事件优先于 pending 普通模式切换；
- runtime state 的 `subtitles` 模式正确渲染；
- 多窗口不依赖本地 Tab 推断后端模式。

### 15.5 全量门禁

```bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/
uv run mypy src/
uv run ruff check src/ tests/
cd ui && npm test -- --run
cd ui && npm run build
```

## 16. 真实验收场景

在无游戏等额外高负载的基线环境完成：

1. 启动后处于 assistant；WLK 进程可预热，但没有普通字幕 PCM。
2. assistant → subtitles：助手完整停止，字幕 ready 后 UI 才切换，字幕正常产生。
3. subtitles → assistant：普通字幕 stream 关闭且旧 PCM 不进入助手，TTS 正常播报。
4. assistant → meeting → idle：会议转录、speaker、EOF、PostgreSQL 和纪要行为不回退。
5. subtitles → meeting 失败：普通字幕自动恢复且模式不谎报。
6. 两个浏览器窗口分别尝试助手与字幕：服务端始终只有一个已提交模式和一个 PCM 所有者。
7. WLK 启动未完成时请求字幕：明确失败并保持原模式；助手不被留在停止状态。
8. WLK 活跃时模拟断线：显示 degraded、自动重连，控制面仍能切到 idle/assistant。
9. 检查磁盘和 PostgreSQL：没有新增音频持久化。
10. 在外部高负载下重复短冒烟，只验证系统给出资源紧张提示且不产生内部双工作负载；该场景不作为
    实时延迟通过门禁。

## 17. 验收标准

1. 自动化测试能够证明三个 active 标志最多一个为真。
2. assistant 模式连续注入测试 PCM 时，WLK 发送计数保持为零。
3. subtitles/meeting 模式连续注入测试 PCM 时，interaction queue 写入计数保持为零。
4. 任一模式转换失败后，最终 mode 与真实 active workload 一致。
5. 多浏览器不能绕过服务端模式冲突。
6. 普通字幕首次模式激活只承担连接/ready 等待，不重新加载 WLK 模型。
7. 现有会议 EOF、gap、SRT、PostgreSQL 和回声双防线测试全部通过。
8. 诊断字段不包含音频、完整正文、凭据或私有环境变量。
9. 全量质量门禁通过，pytest 分支覆盖率不低于 80%。
10. 默认模型、下载策略、LAN/localhost 监听选择和公开端口保持不变。

## 18. 发布与回退

### 18.1 发布顺序

1. 后端状态机、代理显式启停、控制协议、前端 ack 后切换及对应测试可以分提交开发，但必须作为
   一个原子产品变更发布；不得把新 `/ws/subtitles` 模式约束单独部署给旧 UI。
2. 发布前执行全量门禁，并完成 assistant ↔ subtitles、assistant/subtitles → meeting 的真实闭环。
3. 验收通过后更新总体架构文档和运行手册，再进入产品版本发布流程。

### 18.2 回退方式

该变更不修改数据库 schema、会议记录、模型文件或外部服务配置。若发布后出现阻断问题，回退整个
原子产品变更并重启 `vr-ui`，恢复发布前前后端组合；不要只回退前端或后端，也不引入临时双拓扑
开关。回退不得删除数据、清理会议记录或改变模型。

## 19. 后续独立议题

以下事项只有在本设计验收完成并获得新证据后再立项：

- 单说话人字幕与会议分人使用两个 WLK profile/实例；
- 动态启停 Sortformer；
- TTS 自适应预缓冲；
- 基于主机压力的模型降级；
- 将 WLK/TTS 服务进程纳入 UI supervisor。

这些事项不属于本设计实施计划，不能作为延迟当前资源所有权修复的前置条件。
