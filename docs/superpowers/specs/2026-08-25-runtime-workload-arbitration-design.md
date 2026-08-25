# 运行时语音工作负载仲裁设计

## 1. 文档状态

- 设计日期：2026-08-25（Asia/Shanghai）
- 审查修订：2026-08-25（补齐全局状态对账、两阶段切换、订阅撤销、SRT epoch、事件顺序与取消语义）
- 状态：方案已批准，审查修订稿待复核
- 决策记录：[`ADR-005`](../../decisions/0005-server-side-runtime-workload-arbitration.md)
- 代码基线：`main`，`HEAD=407c980ec9be5d4473256931af5cb58f93a51f9c`
- 图证据：`Users-hrygo-Documents-voice-realtime`，generation
  `2026-08-25T14:02:46Z`，相关源码与文档路径均无已记录覆盖缺口；`src/voice_realtime/ui/__pycache__`
  为按设计排除范围，该信号为 best-effort
- 运行态证据：`runtime/` 日志和本机进程快照直接读取，不属于知识图覆盖范围

## 2. 结论先行

当前标准单浏览器流程已在进入字幕 Tab 时停止助手，并只在字幕 Tab 挂载字幕 WebSocket，不能简单
归类为“设计上始终双 ASR 并发”。真正缺口是这种互斥由前端尽力维持，而服务端只认识
`assistant / meeting / idle`，无法约束控制命令失败、快速切换、多浏览器和直接 WebSocket 客户端。

本设计把普通字幕提升为服务端一等模式，由 `RuntimeModeCoordinator` 原子仲裁
`assistant / subtitles / meeting / idle`。模式转换使用“目标无 PCM 准备 → 来源静默 → 原子提交”的
两阶段事务；每次提交向所有控制客户端广播带 revision 的完整状态。WLK、TTS 与 LM Studio 服务进程
保持常驻，任意时刻最多只有一个 PCM 推理所有者。

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
9. 页面启动、重连、命令超时和多浏览器并发最终收敛到最高 `runtime_revision`。
10. 目标预检失败时不停止来源工作负载，也不重置来源会话。

### 4.2 非目标

- 不更换 Qwen3-ASR、SenseVoice、Sortformer、Qwen3-TTS 或 LM Studio 模型。
- 不实现生产 ASR 后端热切换；既有科学测试 registry 不转化为运行时切换产品功能。
- 不让 `vr-ui` 接管 WLK、TTS 或 LM Studio 服务进程生命周期。
- 不增加第二个 WLK 实例或动态切换 diarization。
- 不自动扫描、结束、暂停或限速游戏及其他用户进程。
- 不在本阶段增加 TTS 预缓冲或修改 `chunk_ms`；先完成资源隔离和节奏观测。
- 不改变麦克风选择逻辑、回声双层防线或会议数据模型。
- 不承诺有意离开助手模式后保留 InteractionSession 或 LM Studio response chain；这与当前行为一致。
- 不在本阶段根据尚未校准的主机压力指标自动弹出资源处置提示。

## 5. 核心不变量

下表描述无在途转换的稳定态；转换期由后续 `pcm_owner` 规则约束。

| 模式 | 交互 PCM | 普通字幕 PCM | 会议 PCM | LLM/TTS | 允许的字幕事件订阅 |
|---|---:|---:|---:|---:|---|
| `assistant` | 是 | 否 | 否 | 是 | 否 |
| `subtitles` | 否 | 是 | 否 | 否 | 是 |
| `meeting` | 否 | 否 | 是 | 否 | 是，只读观察会议转录 |
| `idle` | 否 | 否 | 否 | 否 | 否 |

额外不变量：

1. `pcm_owner` 只能为 `assistant / subtitles / meeting / none`，任意时刻只有一个值；prepared 连接和
   被模式门控的已启动管道不拥有 PCM。
2. 稳定态下 `pcm_owner` 与 `mode` 一致；转换期只允许 `source → none → target`，不得直接双写。
3. 模式值只在目标工作负载 ready 且来源已经静默后更新；失败时保持来源模式或强制降级 `idle`。
4. 每次离开来源模式都清空其应用层音频队列，并停止对应发送任务。
5. `meeting` 的 EOF 冲刷完成或超时后才释放会议捕获租约；释放租约不得自动恢复普通字幕。
6. 浏览器订阅数量不决定模式；订阅断线不能隐式启动、停止或抢占工作负载。
7. 模式提交、补偿恢复、强制降级和 shutdown 清理都会递增 `runtime_revision` 并发布完整状态。
8. `runtime_revision` 只排序 workload ownership 字段；客户端只用它更新 `mode`、`pcm_owner` 和模式
   派生导航。相同 revision 的这些字段必须相同，其他状态继续遵循各自现有事件/命令语义。

## 6. 目标架构

```text
浏览器控制 WS
  └─ start_assistant / start_subtitles / start_meeting / stop_active_mode
       └─ RuntimeModeCoordinator（唯一模式锁、两阶段切换与补偿回滚）
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
  └─ 只读订阅 SubtitleProxy；不拥有模式写权限；随运行时模式撤销

RuntimeStateBroadcaster
  └─ revisioned runtime_state ──► 所有控制 WS 客户端
```

### 6.1 两阶段工作负载接口

`RuntimeModeCoordinator` 不依赖 WLK 或 UI 细节。普通字幕通过不透明 preparation token 区分“连接已
ready”和“已拥有 PCM”：

```python
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SubtitlePreparation:
    generation: int


class SubtitleWorkload(Protocol):
    @property
    def browser_capture_active(self) -> bool: ...

    async def prepare_browser_capture(
        self, *, timeout_secs: float
    ) -> SubtitlePreparation: ...

    def commit_browser_capture(self, preparation: SubtitlePreparation) -> None: ...

    async def abort_browser_capture(self, preparation: SubtitlePreparation) -> None: ...

    async def deactivate_browser_capture(self) -> None: ...
```

`prepare` 可以建立 WebSocket、等待 ready，但不得接受 PCM；`commit` 只做已验证 token 的内存态提升，
不得执行网络 I/O，因而在来源停止后不存在第二个可预见失败点；`abort` 关闭 prepared 连接。生产实现
由 `SubtitleProxy` 提供，测试使用 fake workload 验证顺序和回滚。

会议启动采用同样语义：`MeetingSession.prepare_start()` 完成存储可写检查、创建 meeting record、注册
监听器和建立无 PCM 的会议 stream，但不发布 `recording` 事件；`commit_start()` 只启用会议 PCM。
协调器提交 mode/owner/revision 后再调用 best-effort `publish_started()`；事件发布失败只记录错误并由 runtime
state/meeting snapshot 补偿，不能反向回滚已经开始的会议。`abort_start()` 释放租约并将已创建记录标记
为 `interrupted/mode_switch_aborted`，不得删除记录。

助手目标准备可启动 InteractionSession，但 `UIRuntime` 在提交前继续把 PCM 交给来源模式；因此新管道
只处于 ready，不执行 ASR。若准备失败，来源字幕或会议状态不变。

### 6.2 WLK 进程与连接边界

`run-all.sh` 继续并行启动 `vr-subtitles`。由于模型加载约需几十秒，UI 启动和助手可先完成；
`/api/services` 在此期间显示 `starting/unreachable`，但不阻塞助手模式。

`SubtitleProxy.start()` 只完成目录、任务状态和诊断初始化，不再自动调用普通字幕 supervisor。
`prepare_browser_capture()` 才建立 `/asr` WebSocket，并等待下列条件：

1. TCP/WebSocket 建立成功；
2. 收到 WLK ready/config 事件；
3. 当前没有会议捕获租约；
4. preparation 没有被 shutdown 取消。

四项全部满足后只返回 preparation token，仍保持 `browser_capture_active=False`。协调器停止来源并把
`pcm_owner` 置为 `none` 后调用同步 `commit_browser_capture()`，再提交 mode/owner。普通字幕连接在活跃
模式中断线时，所有权保持 active，连接状态进入 `backoff/degraded` 并持续重连，直到显式 deactivate。

`deactivate_browser_capture()` 必须取消发送与接收任务、关闭 stream、清空 `_audio_buffer`、清除 ready，
归档当前 SRT epoch 并进入 `paused`。会议 `_close_capture()` 只释放会议资源并进入 `paused`，不得调用
普通字幕恢复逻辑；只有协调器可以重新 prepare/commit 普通字幕。

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
| `assistant` | `start_subtitles` | `subtitles` | prepare subtitles(no PCM) → owner none → stop interaction/drain → promote prepared → commit | prepare 失败保持原助手会话；静默来源失败则 abort target 并按实际状态收敛 |
| `idle` | `start_subtitles` | `subtitles` | prepare subtitles(no PCM) → promote → commit | abort preparation，保持 `idle` |
| `subtitles` | `start_assistant` | `assistant` | prepare interaction(no PCM) → owner none → deactivate subtitles/archive → commit | prepare 失败保持原字幕；来源静默失败则 abort interaction |
| `idle` | `start_assistant` | `assistant` | prepare interaction(no PCM) → commit | 停止 prepared interaction，保持 `idle` |
| `assistant` | `start_meeting` | `meeting` | prepare meeting(no PCM/no event) → owner none → stop interaction/drain → commit meeting → publish | prepare 失败保持原助手会话；后续失败 abort meeting record |
| `subtitles` | `start_meeting` | `meeting` | prepare meeting(no PCM/no event) → owner none → deactivate subtitles/archive → commit meeting → publish | prepare 失败保持原字幕；后续失败 abort meeting 并按实际状态恢复 |
| `meeting` | `end_meeting` | `idle` | owner none → EOF/finalize → release capture(no auto-resume) → commit/publish | 保持既有 interrupted/finalization timeout 语义，最终进入 `idle` |
| 任意活动模式 | `stop_active_mode` | `idle` | owner none → 停止对应工作负载 → drain → commit | 尽力释放；失败返回错误且不得谎报活动工作负载已停止 |

幂等规则：

- 已在 `assistant` 且 interaction active 时，`start_assistant` 返回成功且不重建管道。
- 已在 `subtitles` 且 browser capture active 时，`start_subtitles` 返回成功且不重连。
- `meeting` 中重复开始任一模式均返回 `mode_conflict`。
- 转换执行期间的并发用户命令由同一把锁串行处理，不抢占在途转换；后到命令根据最新已提交模式重新判断。

### 7.3 模式提交与回滚

协调器在转换开始时保存来源工作负载身份和 revision，不提前修改 `_mode`。目标 preparation ready 后，
先在临界区将 `pcm_owner=none`，从 AudioHub 边界阻止任何新 PCM，再停止来源并清空来源队列；目标同步
提升成功后一次性更新：

```text
_mode = target
_pcm_owner = target
_runtime_revision += 1
```

随后发布完整 `runtime_state`。目标 preparation 失败只执行 target abort，来源 mode、PCM owner、会话与
revision 均不变化。来源静默之后若出现不可预见失败，则执行来源补偿；补偿成功保持来源 mode 并重新
取得来源 PCM owner，同时递增 revision、发布恢复快照。补偿失败时：

- 尽力停止所有工作负载；
- `_mode = idle`；
- `_runtime_revision += 1`；
- 返回稳定错误码 `service_unavailable`；
- 结构化日志记录目标错误和补偿错误，不记录音频、凭据或完整外部响应。

这里的“恢复”只保证工作负载可用性和权威状态一致。用户有意离开助手模式会结束当前
InteractionSession；切回助手会建立新的 LM Studio response chain。目标 preparation 失败发生在停止
助手之前，因此不会丢失原助手上下文。

### 7.4 取消与 shutdown

- 用户命令不互相取消；后到命令在锁后排队。
- 控制 WebSocket 断开或浏览器命令超时不取消已经由服务端接受的转换；最终结果通过 runtime state
  广播收敛。
- 所有 prepare 和外部等待必须有明确超时。
- 应用 shutdown 设置 closing flag，拒绝新命令，并取消当前转换任务；协调器捕获
  `asyncio.CancelledError`，abort prepared target、停止所有可见工作负载、设置 `mode=idle` 和
  `pcm_owner=none`、递增 revision。shutdown 不恢复来源模式。
- shutdown 最终快照尽力广播，但服务退出不以客户端接收成功为前提。

## 8. 控制协议与前端行为

### 8.1 新控制命令

在现有严格 `request_id` 协议中增加：

```json
{"request_id":"...","cmd":"start_subtitles"}
```

成功 ack 继续返回统一 runtime state，其中 `mode="subtitles"`。失败使用现有错误包络和
`mode_conflict`、`service_unavailable`，不新增第二套错误格式。

控制面新增应用级 `RuntimeStateBroadcaster`。`/ws/v1/control` 和兼容入口
`/ws/assistant/cmd` 建立连接时注册独立有界队列，先发送当前完整快照；每次 revision 变化再向所有
连接发送：

```json
{
  "contract_version": "1",
  "event": "runtime_state",
  "state": {"mode": "subtitles", "runtime_revision": 12}
}
```

命令 ack 仍只发给请求连接并保留 `request_id`。广播与 ack 允许任意先后到达，客户端按 revision
幂等应用；广播队列满时丢弃旧快照、保留最新快照。命令超时只表示请求方没有及时收到 ack，不等价于
服务端事务失败。

`runtime_revision` 是 workload ownership revision，不是所有 UI 字段的全局版本。客户端用它排序
`mode`、`pcm_owner`、`active_meeting_id` 及由模式驱动的 Tab；`mic_muted`、persona、pipeline 细节和会议
转录仍按既有命令 ack、助手事件和会议 revision 更新。相同 `runtime_revision` 的 ownership 字段必须
完全一致，避免完整快照中其他字段变化造成错误去重。

### 8.2 前端切换顺序

`App` 把“工作区导航”和“运行时模式”分开：会议历史 Tab 可以在助手运行时浏览，但字幕面板只允许在
服务端 `mode=subtitles` 时挂载。页面启动时先等待控制 WS 的首个合法 runtime state；在此之前不挂载
`SubtitleStream`，也不根据 `localStorage` 自动发模式命令。

首个快照和后续广播按以下规则对账：

- `mode=meeting`：强制会议 Tab；
- `mode=subtitles`：强制字幕 Tab 并挂载字幕流；
- `mode=assistant/idle` 且当前或持久化 Tab 为字幕：回退助手 Tab；
- `mode=assistant/idle` 且当前 Tab 为会议历史或助手：保留导航选择；
- revision 小于已应用 revision 的快照直接忽略。

用户显式切换到助手或字幕时，`App` 不先提交 `activeTab`：

1. 设置 `pendingTab` 并禁用重复切换；
2. 发送目标模式命令；
3. 等待匹配 `request_id` 的 ack；
4. 验证 ack revision 不小于当前 revision，且最高已知快照的 mode 等于目标模式；
5. 提交 `activeTab`，此时目标面板才挂载；
6. 明确失败时保留原 Tab 并显示错误；超时时显示“正在对账”，等待广播或主动读取 `/api/runtime`，不得
   把本地超时当作服务端失败；
7. 在途请求被更高 revision 的其他客户端转换取代时，以更高 revision 状态为准并清除 pending。

用户切换到会议 Tab 只浏览会议工作区，不改变模式；开始会议仍由 `start_meeting` 命令完成。会议
`recording` 事件只能在协调器提交 `mode=meeting` 后发布，前端收到事件时只反映已提交事实。

### 8.3 字幕 WebSocket

`/ws/subtitles` 的语义固定为只读事件订阅：

- `mode=subtitles`：接受并接收普通字幕；
- `mode=meeting`：接受并接收会议捕获广播，不创建第二条 WLK 流；
- `mode=assistant/idle`：关闭码 `4409`，reason 使用固定非敏感文本“字幕模式未激活”；
- 客户端断开只移除广播队列，不改变全局模式；
- 多客户端共享同一 `SubtitleProxy` 连接，每个客户端继续使用独立有界发送队列。

模式校验必须同时覆盖“连接建立”和“连接存续”：字幕 WS 在注册广播队列后先读取当前 runtime state，
只在允许模式下接受订阅；连接存续期间并发监听 runtime state，模式提交到 `assistant/idle` 时主动以
`4409` 关闭。这样即使连接检查与模式切换交错，也会由初始快照或后续广播关闭，不留下竞态窗口。
`subtitles → meeting` 可以保留只读订阅；`meeting → idle` 必须撤销。

## 9. PCM 门控与队列语义

### 9.1 UIRuntime 双重门控

`AudioHub` sink 保持常驻，但回调必须在进入下游前校验协调器的 `pcm_owner`，不能只检查目标连接是否
ready：

```text
_enqueue_audio:
  仅 pcm_owner=assistant 且 interaction active 时写 interaction queue

_push_subtitle_audio:
  pcm_owner=subtitles 时写普通字幕流
  pcm_owner=meeting 时写会议捕获流
  其他模式直接丢弃
```

`SubtitleProxy.push_audio()` 保留现有自身状态校验作为第二道防线，不能只信任调用方。

### 9.2 切换屏障

转换按以下顺序建立屏障：

1. 准备目标工作负载并等待 ready，但目标不接收 PCM；
2. 在协调器临界区设置 `pcm_owner=none`，AudioHub 两个下游立即拒绝新 PCM；
3. 清空来源应用队列，取消来源发送任务并按来源语义关闭 stream；
4. 同步提升 prepared target；
5. 在同一临界区提交目标 mode、pcm owner 和 revision；
6. 发布完整 runtime state，目标从下一块新 PCM 开始处理。

旧音频不得被发送到目标模式。普通字幕不要求持久化切换期间丢弃的音频；会议模式仍按现有 gap 和
EOF 规则处理，不能静默丢弃已接受的会议音频。

`MeetingSession.prepare_start()` 期间虽然已经存在 meeting record 和 WLK stream，但
`capture_accept_audio=False`；只有 `commit_start()` 与 mode/owner 提交的临界区允许它接收后续 PCM。

### 9.3 普通字幕 epoch 与 SRT 边界

每次普通字幕模式激活建立新的 `subtitle_epoch`；活跃模式中的 WLK 网络重连也建立新 epoch，避免把
从零时间轴返回的新完整快照误当成旧快照延续。关闭旧 epoch 时：

1. 若存在 confirmed 内容，按现有原子写规则完成 `current.srt`，再归档为唯一的
   `session-<timestamp>[-N].srt`；
2. 原子清空 `current.srt`，然后清除 `_last_payload`、`_snapshot_signature`、
   `_persisted_confirmed_signature`、ready 和 session flags；
3. 向仍合法的浏览器订阅广播 `{"type":"reset","source_epoch":N}`；
4. 新客户端注册时只回放当前 epoch 的快照，绝不回放已归档 epoch 的 `_last_payload`。

没有 confirmed 内容的 epoch 不创建空归档。会议 capture 不进入这套 SRT 流程，也不写
`current.srt`。本期不跨 epoch 合并时间轴；历史通过归档文件保留，这是比隐式覆盖更安全的边界。

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

禁止通过抓取 WLK 日志推导应用健康。由于无语音时本来就可能长期没有转写事件，在缺少 VAD speech
区间或 vendor 原生 lag 前，`last_event_age_ms` 只作为原始诊断，不单独触发 `degraded`。本期
`degraded` 只由连接 backoff/error 或已提交 mode 的工作负载未 ready 产生。队列 drop 和会议 gap
保持独立累计计数，不因单次历史丢弃让 workload 永久 degraded；不得伪装成 WLK 内部 lag。

### 10.2 音频与 TTS 指标

增加下列本机诊断，不改变音频数据边界：

- `AudioHub` 每个 sink 的 `dropped_chunks`；
- interaction queue 的 `dropped_chunks`；
- SubtitleProxy 普通字幕和会议 buffer 的丢弃/gap 计数；
- TTS 首块耗时、源音频块数量、最大/中位块间隔、超过 `200ms` 的间隔次数；
- 最近一次模式切换耗时、目标、结果和回滚结果。

TTS 指标名称必须使用 `source_chunk_gap`，不能在没有音频设备回调证据时命名为“扬声器 underrun”。

### 10.3 外部负载边界

应用不枚举、终止或归因具体用户进程。本期只暴露 `last_event_age_ms`、drop/gap 计数、已存在的 ASR
性能数据和 TTS source chunk gap 等原始诊断，不自动弹出“请关闭高负载应用”提示，也不自动调整模型。

只有后续独立基准能够给出 speech-aware 的窗口、阈值、恢复条件和提示冷却时间后，才允许增加资源压力
提示。该提示不属于本设计的实现或验收范围。

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
| WLK 尚未 ready | prepared connection 超时并 abort，来源从未停止 | mode/revision/来源会话不变，`service_unavailable` |
| 普通字幕 WS 中断 | supervisor 退避重连，保持 `subtitles`，工作负载为 `degraded` | 控制面仍可停止或切换 |
| 切到助手时 interaction preparation 失败 | 停止 prepared interaction，来源字幕保持活动 | mode/revision 不变 |
| meeting preparation 失败 | abort stream/listeners，已创建记录标记 interrupted，来源保持活动 | 返回原异常映射 |
| 来源静默失败 | abort prepared target，按真实 active 状态恢复或强制 idle | 广播新的权威快照 |
| 会议 EOF 超时 | 沿用 interrupted/finalization timeout | 最终进入 `idle` |
| 浏览器字幕客户端慢 | 丢弃该客户端旧快照，保留最新快照 | 不影响 WLK 和其他客户端 |
| 已连接字幕客户端失去模式资格 | 服务端主动关闭 | `4409`，不改变模式 |
| 命令 ack 超时但事务继续 | 客户端等待广播或 GET `/api/runtime` 对账 | 不本地回滚服务端事实 |
| AudioHub sink 满 | 丢弃最旧块并累计计数 | 诊断标记 degraded，不阻塞采集线程 |
| 应用 shutdown | coordinator 取消事务、abort target、停止工作负载，再停止 hub/proxy | 强制 `idle/none`，不恢复来源 |

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
| `src/voice_realtime/meeting/runtime_mode.py` | 增加 `subtitles`、`pcm_owner`、两阶段转换、取消与补偿回滚 |
| `src/voice_realtime/meeting/session.py` | 将会议启动拆为 prepare/commit/abort，保证 recording 事件在 mode 提交后发布 |
| `src/voice_realtime/ui/subtitle_proxy.py` | 增加 prepared/active stream、显式停机、SRT epoch、ready 和诊断快照；删除会议后自动恢复 |
| `src/voice_realtime/ui/runtime.py` | 实现 `pcm_owner` 门控、队列屏障、目标 preparation，并发布 revision 变化 |
| `src/voice_realtime/ui/runtime_events.py` | 新增独立的有界 `RuntimeStateBroadcaster`，避免把多客户端状态分发塞入 server 路由 |
| `src/voice_realtime/ui/protocol.py` | 增加 `StartSubtitlesCommand`、`pcm_owner` 和 additive runtime/diagnostic 字段 |
| `src/voice_realtime/ui/control.py` | 路由 `start_subtitles` 并保持统一 ack/error 包络 |
| `src/voice_realtime/ui/server.py` | 接入 runtime broadcaster、约束字幕 WS 建立/存续模式、聚合健康 |
| `src/voice_realtime/audio/hub.py` | 暴露只读 sink drop 诊断，不改变背压策略 |
| `src/voice_realtime/ui/assistant_bridge.py` | 记录 TTS 源块节奏，不改变播放路径 |
| `ui/src/App.tsx` | 首快照门控、revision 对账、ack 后提交 Tab、维护 pending/超时状态 |
| `ui/src/hooks/useCommandSocket.ts` | 接受 unsolicited runtime state，只应用非过期 revision |
| `ui/src/protocol.ts` | 接受 `subtitles`、`pcm_owner` 和 additive 诊断字段 |
| 对应 `tests/` 与 `ui/src/*.test.tsx` | 冻结转换、回滚、多客户端、门控和 UI 时序 |

不移动 `RuntimeModeCoordinator` 文件。`SubtitleProxy` 只做完成 prepared/active 与 SRT epoch 所需的内部
整理；runtime 多客户端广播放入独立模块，避免继续扩大 `server.py` 或把 UI 订阅职责塞入 coordinator。

## 15. 测试设计

### 15.1 状态机单元测试

覆盖：

1. 四种模式合法转换与幂等调用；
2. assistant → subtitles 的 prepare → source stop → owner none → promote → commit 顺序；
3. subtitles → assistant 的 target ready → archive/deactivate → commit 顺序；
4. assistant/subtitles → meeting 的 prepared record、无 PCM、提交后事件顺序；
5. target preparation 失败时来源会话、PCM owner、mode 和 revision 全部不变；
6. 来源静默失败时 abort target，补偿失败后进入 idle；
7. 转换期间 `pcm_owner` 不出现双所有者；
8. 并发用户命令被锁串行化且不抢占；控制连接取消不取消服务端事务；
9. shutdown 取消 preparation、停止工作负载并进入 `idle/none`；
10. `runtime_revision` 在成功提交、补偿恢复、强制降级或 shutdown 清理时增长。

### 15.2 SubtitleProxy 测试

覆盖：

- `start()` 不自动连接；
- prepare 等待 ready 但不接受 PCM，token 只能 commit/abort 一次；
- target preparation 失败不会触碰活跃普通字幕或助手；
- deactivate 关闭任务、清空 PCM、归档 SRT epoch 并清除旧 payload；
- 新激活和活跃模式重连均建立新 epoch，旧 SRT 不被覆盖；
- 会议租约释放后不自动恢复普通字幕 supervisor；
- 会议 prepared capture 在 commit 前不接受 PCM、不发布 recording；
- 重连只在字幕模式持续；
- shutdown 不恢复 supervisor；
- 多客户端共享同一 stream，慢客户端隔离；
- 诊断时间与计数使用单调时钟，可注入 fake clock。

### 15.3 UIRuntime 与服务器测试

覆盖每种 `pcm_owner` 下两个 AudioHub sink 的放行/拒绝矩阵；验证 prepared target 收不到 PCM；验证
`/ws/subtitles` 在 assistant/idle 建连返回 `4409`，已连接客户端在模式变为 assistant/idle 后也收到
`4409`，并覆盖“模式提交恰好发生在 WS 注册前后”的竞态。验证 RuntimeStateBroadcaster 慢客户端只保留
最新 revision，验证 `/api/services` 兼容旧字段并增加诊断字段。

### 15.4 前端测试

覆盖：

- 命令 ack 前不切换 Tab、不挂载 `SubtitleStream`；
- 命令失败保留原 Tab 并显示提示；
- 首快照到达前不挂载字幕；`localStorage=subtitles` 且服务端 assistant/idle 时不得直连字幕；
- ack 超时后由更高 revision 广播收敛，不执行本地反向命令；
- 快速重复点击只存在一个 pending 转换；
- meeting 事件优先于 pending 普通模式切换；
- runtime state 的 `subtitles` 模式正确渲染；
- 多窗口收到同一 revision 广播并卸载失去资格的字幕面板；
- 乱序到达的旧 ack/广播不能覆盖更高 revision 状态；
- assistant 运行时仍可浏览会议历史 Tab，导航选择不被误当作 mode。

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

1. AudioHub 和交互依赖可用时启动后处于 assistant；依赖不可用时如实处于 idle。WLK 进程可预热，
   但没有普通字幕 PCM。
2. assistant → subtitles：WLK preparation ready 前助手保持活动且上下文不变；提交后助手完整停止，UI
   才切换，字幕正常产生。
3. subtitles → assistant：普通字幕 stream 关闭且旧 PCM 不进入助手，TTS 正常播报。
4. assistant → meeting → idle：recording 事件只在 mode 提交后出现；会议转录、speaker、EOF、
   PostgreSQL 和纪要行为不回退，结束后普通字幕不会自行恢复。
5. subtitles → meeting preparation 失败：普通字幕连接、SRT epoch、模式和 revision 保持不变；若已创建
   meeting record，则记录为 interrupted 而非删除。
6. 两个浏览器窗口分别尝试助手与字幕：服务端始终只有一个 PCM owner，两个窗口最终应用同一最高
   revision；失去资格的字幕 WS 被 `4409` 关闭。
7. WLK 启动未完成时请求字幕：明确失败并保持原助手 mode、PCM owner、InteractionSession 和上下文。
8. 人为延迟命令 ack：服务端提交后，客户端通过 runtime broadcast 或 `/api/runtime` 对账到真实模式。
9. WLK 活跃时模拟断线：显示 degraded、自动重连，新 epoch 不覆盖旧 SRT，控制面仍能切到
   idle/assistant。
10. 检查磁盘和 PostgreSQL：没有新增音频持久化，普通字幕历史归档存在，会议数据边界不变。
11. 在外部高负载下重复短冒烟，只验证原始 drop/gap/chunk-gap 诊断可读取且系统不产生内部双 PCM
    owner；不要求自动提示，也不作为实时延迟通过门禁。

## 17. 验收标准

1. 自动化测试能够证明任意时刻 `pcm_owner` 只有一个值；prepared target 的发送计数为零。
2. assistant 模式连续注入测试 PCM 时，WLK 发送计数保持为零。
3. subtitles/meeting 模式连续注入测试 PCM 时，interaction queue 写入计数保持为零。
4. 任一模式转换失败后，最终 mode 与真实 active workload 一致。
5. target preparation 失败时来源会话未停止；尤其 assistant → subtitles 失败不重建 LM response chain。
6. 多浏览器不能绕过服务端模式冲突，并最终应用相同最高 revision。
7. 已连接字幕 WS 在 assistant/idle 下必定被关闭；模式检查与注册交错也不存在残留订阅。
8. 普通字幕模式激活只承担连接/ready 等待，不重新加载 WLK 模型；每个 connection epoch 的 SRT
   要么为空，要么在重置前归档，不被后续零时间轴快照覆盖。
9. meeting recording 事件的 runtime revision 对应已经提交的 `mode=meeting/pcm_owner=meeting`。
10. 现有会议 EOF、gap、PostgreSQL 和回声双防线测试全部通过。
11. 诊断字段不包含音频、完整正文、凭据或私有环境变量。
12. 全量质量门禁通过，pytest 分支覆盖率不低于 80%。
13. 默认模型、下载策略、LAN/localhost 监听选择和公开端口保持不变。

## 18. 发布与回退

### 18.1 发布顺序

1. 后端两阶段状态机、meeting prepare/commit、代理显式启停、runtime state 广播、控制协议、前端
   revision 对账及对应测试可以分提交开发，但必须作为一个原子产品变更发布；不得把新
   `/ws/subtitles` 模式约束或新事件顺序单独部署给旧 UI。
2. 发布前执行全量门禁，并完成 assistant ↔ subtitles、assistant/subtitles → meeting 的真实闭环。
3. 验收通过后更新总体架构文档和运行手册，再进入产品版本发布流程。

### 18.2 回退方式

该变更不修改数据库 schema、模型文件或外部服务配置；prepared meeting abort 只使用现有字段保留
`interrupted/mode_switch_aborted` 记录。若发布后出现阻断问题，回退整个原子产品变更并重启 `vr-ui`，
恢复发布前前后端组合；不要只回退前端或后端，也不引入临时双拓扑开关。回退不得删除数据、清理
会议记录或改变模型；新代码产生的 interrupted 记录由旧版本按现有模型继续读取。

## 19. 后续独立议题

以下事项只有在本设计验收完成并获得新证据后再立项：

- 单说话人字幕与会议分人使用两个 WLK profile/实例；
- 动态启停 Sortformer；
- TTS 自适应预缓冲；
- speech-aware 的主机资源压力提示阈值与冷却策略；
- 基于主机压力的模型降级；
- 将 WLK/TTS 服务进程纳入 UI supervisor。

这些事项不属于本设计实施计划，不能作为延迟当前资源所有权修复的前置条件。

## 20. 审查意见闭环

| 审查问题 | 本修订的处理 | 主要验证位置 |
|---|---|---|
| 多窗口、首屏和超时无法对账 | RuntimeStateBroadcaster + ownership revision + 首快照门控 | §8.1、§8.2、§15.4 |
| 目标预检失败会重置助手上下文 | target preparation 在来源停止前完成 | §6.1、§7.2、§17.5 |
| coordinator 可被自动恢复和存量 WS 绕过 | 删除会议后自动恢复；存量订阅随模式关闭 | §6.2、§8.3、§17.7 |
| 新 WLK 零时间轴覆盖旧字幕/SRT | 每次激活和重连建立、归档、重置 epoch | §9.3、§17.8 |
| meeting event 早于 mode commit，取消语义矛盾 | meeting prepare/commit/publish 分离；用户命令非抢占 | §6.1、§7.4、§17.9 |
| 资源压力提示无阈值、不可测试 | 本期仅输出原始诊断，提示阈值独立立项 | §10.3、§16.11、§19 |
