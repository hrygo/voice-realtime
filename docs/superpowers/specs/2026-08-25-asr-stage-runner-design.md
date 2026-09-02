---
title: "ASR Stage 2–5 统一执行器设计"
description: "ASR 科学对比 Stage 2-5 统一生命周期执行器与决策报告生成设计"
status: implemented
type: technical_spec
category: asr
version: "v1.0.0"
date: 2026-08-25
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "sona-core"
tags:
  - asr
  - stage-runner
  - benchmark-runner
---

# ASR Stage 2–5 统一执行器设计

## 1. 文档状态

- 设计日期：2026-08-25（Asia/Shanghai）
- 状态：书面规范已批准；实施计划自审产生的 finalize/cursor、model root 与 eligibility 三项接口澄清等待复核
- 适用分支：`feature/asr-benchmark-runner`
- 代码基线：`0d098b1d9ffe39d9288cda849e65cd2680639851`
- 索引证据：Codebase Memory Tier 2，generation `2026-08-24T19:50:07Z`，与上述
  `HEAD` 匹配；相关路径无已记录索引缺口（best-effort，不代表绝对完整）
- 上位方案：[`Fun-ASR 与现有 ASR 后端科学对比测试方案`](../../solutions/Fun-ASR与现有ASR后端科学对比测试方案.md)

## 2. 背景与问题

Stage 0、Stage 1 的盲测、评分、统计和正式证据链已经具备程序化入口；Stage 2–5 目前只有
`ScheduleManifest`、`FaultPlan`、`StageRunManifest`、`ArtifactIndex` 和
`StageDecisionReport` 等不可变数据契约，没有负责执行这些契约的统一生命周期。

如果直接为流式、会议和交互分别编写脚本，会产生以下问题：

1. Screen 通过后是否保持同一模型、进程和会话无法统一证明。
2. Stage 3 与 Stage 5 共用 60 分钟会议会话时，容易重复运行或形成相互引用的制品哈希。
3. 输入字节、故障游标、资源锁、失败保留和文件权限可能因脚本而异。
4. `StageDecisionReport` 的字段校验只能证明报告形状合法，不能证明其数值来自真实运行制品。
5. 并行运行模型、服务或全量测试会争用本机统一内存、MPS、CPU、端口和 PostgreSQL。

因此，需要一个只负责编排和证据封存的统一执行器；具体 ASR、会议和交互实现通过窄接口注入。

## 3. 目标与非目标

### 3.1 目标

1. 用同一状态机执行 Stage 2–5，并复用现有契约与全局资源锁。
2. 逐字节验证冻结输入，保证 baseline 与 candidate 消费相同 schedule。
3. 保证 Stage 2/4 的 Screen → Confirm 不重启模型或会话。
4. 让会议 candidate 的一次连续 60 分钟运行同时提供 Stage 3 与 Stage 5 证据，避免重复墙钟。
5. 在正常结束、门禁早停和可捕获失败时均保留可审计制品。
6. 只有完整、相互绑定的正式证据可以生成决策；合成执行器永远不能生成生产 `Promote`。
7. 模型、PCM、reference、逐字稿和原始运行结果始终位于项目目录外。
8. 从正式运行身份校验开始到制品封存结束，全程持有唯一高负载资源锁。

### 3.2 非目标

- 不建设生产运行时热切换或冷切换；选型后仍固定唯一胜出后端。
- 不下载、迁移、删除或隐式发现模型与语料。
- 不改变 Stage 1 的序贯统计、盲测或开盲协议。
- 不读取 reference 生成推理输入，也不把 reference 放入执行器进程。
- 不自动并行模型、服务、实验或全量质量门禁。
- 不让统一执行器直接依赖某个 vendor 的进程启动命令、JSON 或错误格式。
- 不把原始音频、敏感逐字稿、绝对路径或 vendor payload 提交到 Git。

## 4. 选定方案与备选方案

### 4.1 选定：注入式统一执行器

新增通用 `run_stage()` 编排器。它拥有生命周期、锁、路径和哈希验证、输入游标、Screen 边界、
故障注入、制品封存与失败语义；通过 `StageExecutor` 调用具体运行时，通过纯函数 evaluator 解释阶段
指标。真实执行器只负责与目标系统交互，不自行决定是否晋级。

该方案的优点是证据语义只有一套，真实链路可以按 Stage 1 结果按需实现，同时测试可以用确定性合成
执行器覆盖状态机而不启动模型或服务。

### 4.2 未选：四套独立 runner

Stage 2、3、4、5 分别实现可以更快写出第一个脚本，但会复制锁、权限、失败、游标和制品逻辑，且
Stage 3/5 共用会话难以表达。长期审计成本高，未采用。

### 4.3 未选：Shell/subprocess 总控

外层脚本调用不同 CLI 对现有代码侵入较小，但难以提供类型化 observation、同会话证明、异常后的原子
封存和纯函数门禁。它可用于人工启动外部依赖，不作为正式证据编排层。

## 5. 总体架构

```text
CLI / library caller
        │
        ▼
StageRunRequest ──► run_stage()
                         │
          ┌──────────────┼─────────────────┐
          ▼              ▼                 ▼
  StagePolicy       StageExecutor     StageArtifactWriter
  阶段规则/门禁      真实系统适配       原子写入/封存
          │              │                 │
          └──── observation / metrics ─────┘
                         │
                         ▼
                  ArtifactIndex（最后写）
                         │
                         ▼
                verify_stage_decision()
                         │
                         ▼
                 StageDecisionReport
```

模块职责建议如下：

| 模块 | 职责 |
|:---|:---|
| `stage_contracts.py` | 扩展输入绑定、状态、执行 observation 等稳定数据契约 |
| `stage_runner.py` | 请求验证、状态机、schedule 游标、Screen/Confirm、故障编排、异常收敛 |
| `stage_artifacts.py` | 私有目录、原子 JSON、JSONL/CSV、hash、`ArtifactIndex` 封存 |
| `stage_evaluators.py` | Stage 2–5 的纯函数门禁与决策输入，不启动服务、不写文件 |
| `stage_executors.py` | `StageExecutor`、能力描述与注册表；不包含阶段决策 |
| `cli.py` | `run-stage`/`decide-stage` 参数边界和执行器选择 |

文件可在实施时按规模合并，但上述职责边界不能混淆。

## 6. 稳定接口

### 6.1 `StageRunRequest`

请求必须携带真实制品路径，而不是让调用者直接填写自报 hash：

```python
@dataclass(frozen=True)
class StageRunRequest:
    run_id: str
    stage: Literal[2, 3, 4, 5]
    covered_stages: tuple[Literal[2, 3, 4, 5], ...]
    family_id: str
    arm: Literal["baseline", "finalist"]
    candidate_id: str
    evidence_tier: Literal["formal", "experimental"]
    executor_id: str
    model_manifest_path: Path
    model_root: Path
    profile_path: Path
    runtime_config_path: Path
    schedule_path: Path
    input_manifest_path: Path
    input_root: Path
    output_root: Path
    repository_root: Path
    eligibility_path: Path | None = None
    upstream_report_paths: Mapping[UpstreamStage, Path] = field(default_factory=dict)
    fault_plan_path: Path | None = None
    lock_path: Path | None = None
    lock_timeout_secs: float = 0.0
```

`run_stage()` 重新读取并哈希这些制品，构造实际 `StageRunManifest`。调用者不能以参数覆盖实际
`git_commit`、profile、runtime、schedule、fault plan 或模型身份。

`model_manifest_path` 使用 typed `StageModelManifest` 保存 model ID、immutable revision，以及相对于
`model_root` 的必需文件路径、大小和 SHA-256。formal 运行逐项拒绝 symlink/non-regular/path escape，
并重新计算实际大小和 hash；core runner 不加载模型，也不允许 executor 绕过已验证 root。

`covered_stages` 对普通运行必须等于 `(stage,)`；唯一例外是会议 candidate 的物理
`stage=5, covered_stages=(3, 5)`。该 lineage 同时写入 final manifest 和 summary，Stage 3/5 report
必须引用同一个 run identity 与 artifact index。其他跨阶段组合一律拒绝。

formal 请求必须提供 `StageEligibilityEvidence` 及其列出的全部 upstream report paths；runner 重新计算
hash，并核对 target stage、family、candidate 和唯一状态。`eligible=false` 时只封存
`planned → deferred`，完全不构造 executor；`eligible=true` 才允许进入 `running`。experimental 请求可
省略 eligibility，但其证据不能进入 Promote。

`run_stage()` 返回只含 `run_id`、terminal status、manifest/index hash、外部相对制品标识和停止原因的
`StageRunResult`；不返回 reference、原始音频、逐字稿或绝对路径。

### 6.2 输入绑定

`ScheduleManifest` 只保存顺序和 `input_sha256`，不保存路径。新增 `StageInputManifest`，以
`segment_id` 显式绑定相对于 `input_root` 的输入、类型、大小、SHA-256 与格式。

输入类型固定为两种：

- `pcm`：实际音频字节；绑定声道、采样率和 sample format。
- `interaction_script`：UTF-8 canonical JSON；只包含允许的 action enum、时间线、opaque audio asset
  ID、相对 asset binding 和 asset SHA-256，不包含 reference 或必须持久化的明文话术。其引用的 PCM
  asset 也必须由同一 manifest 显式绑定和逐字节验证。

正式规则：

- 映射必须与 schedule 的全部 `segment_id` 一一对应，禁止缺失和重复。
- `relative_path` 必须是规范相对路径，解析后仍位于显式 `input_root` 内。
- 输入必须是 regular file，拒绝 symlink、device、socket 和路径穿越。
- 每次 feed 前重新计算实际字节数、规范序列化 SHA-256 和可推导时长，并与 input manifest、schedule
  双重核对。
- Stage 2/3 PCM 固定 16 kHz、mono、s16le；Stage 2 固定 20 ms feed frame。Stage 4 由
  `interaction_script` 冻结 PCM、外放、插话和等待动作，executor 不得自行解释额外指令。
- runner 只读取显式绑定文件，不扫描目录，不猜测文件名，不读取 reference。
- repetition 只重复相同冻结字节，不复制或改写输入文件。
- runner 严格按 schedule 顺序和 repetition index 执行，不得动态插入、跳过或重排；每次执行记录
  `cursor_start_ms/cursor_end_ms`。完整运行必须满足 `executed_cursor_ms == total_duration_ms`，按计划
  Screen 早停则记录已执行 cursor 和未消费 segment 集合。

### 6.3 `StageExecutor`

```python
class StageExecutor(Protocol):
    executor_id: str
    capabilities: StageExecutorCapabilities

    async def prepare(self, context: StageExecutionContext) -> None: ...
    async def start(self, context: StageExecutionContext) -> SessionIdentity: ...
    async def feed_segment(
        self,
        segment: ScheduleSegment,
        resolved_input: ResolvedStageInput,
        cursor_range: CursorRange,
    ) -> SegmentObservation: ...
    async def inject_fault(self, event: FaultEvent) -> FaultObservation: ...
    async def snapshot(self) -> RuntimeObservation: ...
    async def finalize(
        self,
        finalization_fault: FaultEvent | None,
    ) -> FinalObservation: ...
    async def close(self) -> CloseObservation: ...
```

接口约束：

1. `prepare()` 可以校验依赖，但不得推进音频游标。
2. `start()` 每次物理运行只调用一次；返回不含秘密和绝对路径的 `session_id`、进程/epoch 身份。
3. `feed_segment()` 接收 runner 已验证的 `ResolvedStageInput` 和冻结 cursor range，不接收任意路径。
   PCM 输入暴露确定帧流；interaction 输入暴露判别联合 action 流及已验证 asset，不允许 executor 自行
   读取其他音频或解释未知 action。cursor range 可以是完整 segment，也可以是 runner 在固定故障
   cursor 处切出的确定 slice；executor 只能消费该 range 对应的字节/action，不能越界推进。
4. `inject_fault()` 只执行 runner 指定的事件，返回开始、恢复、结果和受影响身份。
5. `snapshot()` 提供资源和状态快照，不给出晋级结论。
6. `finalize(finalization_fault)` 完成 EOF/flush 并返回最终 observation；参数只能为该 run 固定
   `finalization_delay` 或 `None`。executor 必须按“发送 EOF → 应用延迟 → 接收 terminal”执行，并在
   `FinalObservation` 中返回对应 fault observation；runner 决定是否满足门禁。
7. `close()` 必须幂等；runner 在成功和异常路径都调用，并通过 `CloseObservation` 核验自有进程、
   端口、AudioHub/连接、后台 task 和文件描述符已经释放。
8. executor 的异常必须使用稳定错误类别；原始 vendor 内容仅进入受限诊断制品。
9. runner 串行调用 feed、fault、snapshot 和 finalize；禁止 executor 在 runner 不知情时并发推进 cursor
   或自行注入故障。

`StageExecutionContext` 额外携带仅驻留内存、`repr=False` 的 `ValidatedRuntimeInputs`：已验证
`model_root`、typed model manifest、profile payload 和 runtime config payload。该对象允许真实 executor
加载目标运行时，但禁止进入 observation、JSON/CSV、错误消息或 decision artifact。

`StageExecutorCapabilities` 至少声明支持的 stage、输入格式、是否支持同会话 continuation、支持的故障
类型和 `is_synthetic`。formal 请求必须拒绝 `is_synthetic=True` 的 executor。

### 6.4 执行器注册表

CLI 通过显式 `executor_id` 从注册表构造执行器，未知或重复 ID 立即失败。注册表不做模型发现、不加载
所有候选，也不允许 `synthetic` 作为正式 CLI 默认值。

真实适配器按 finalist 结果延迟落地：

- Stage 2：`StreamingStageExecutor`，包装已注册的 `StreamingTranscriber`。
- Stage 3/5：`MeetingStageExecutor`，包装测试专用会议运行时组合，不修改生产默认后端。
- Stage 4/5：`InteractionStageExecutor`，使用受控 PCM 注入和固定 LLM/TTS/VAD/回声配置。

测试使用 `SyntheticStageExecutor`，只由测试代码显式注入，产物标记 `experimental`，不能进入正式
decision chain。

## 7. 生命周期与状态机

```text
request
  └─ acquire exclusive lock
       └─ validate all identities and external paths
            └─ allocate new run directory
                 └─ planned
                      ├─ deferred（前置 finalist 条件不满足，未启动 executor）
                      └─ running
                           ├─ completed（含按计划 Screen-Fail/硬门禁早停）
                           └─ failed（基础设施、executor 或 writer 异常）
```

规则：

- 输出 run 目录必须不存在；禁止覆盖、续写、复用或自动删除旧 run。
- `state.json` 记录当前状态、时间戳、canonical cursor、session identity 和停止原因，使用原子替换。
- `manifest.json` 是当前运行快照；进入 terminal 状态后写入最终值并由 `ArtifactIndex` 绑定，此后不可改。
- 所有状态转移同步写入 `events.jsonl`，非法回退或第二个 terminal 状态立即失败。
- Screen 门禁失败是一次按设计完成的运行：run status 为 `completed`，decision 为 `Screen-Fail`。
- `failed` 只表示执行基础设施没有完成既定实验，不能被解释为模型质量 `Reject`。
- `deferred` 只允许在 executor 启动前产生，并明确记录缺少 finalist、非唯一 finalist 或上游未完成。

常规异常由 runner 捕获并写入 `failed`、`failures.jsonl` 和 partial artifact index。若进程被
`SIGKILL`、断电或 writer 本身不可用，目录保留 `running` 且没有 `ArtifactIndex`；后续运行不得接管或
覆盖。此类目录是未封存证据，不能进入决策，恢复工具只能显式封存为 interrupted/failed，不能续跑成
同一个正式 run。

terminal manifest/state 必须先写入，`artifact-index.json` 随后作为最后一个文件落盘。只有
`terminal status + 有效 ArtifactIndex` 同时存在时，状态才对外生效；若两者之间发生不可捕获崩溃，
该目录按 unsealed 处理，即使快照里已经出现 `completed` 也不能用于决策。清理 observation 不完整时，
runner 将状态改为 `failed`，不能以 `completed` 返回。

## 8. Screen → Confirm 连续性

Stage 2/4 的 schedule 必须先出现全部 `screen`，再出现 `confirm`。runner 在最后一个 Screen segment
后执行纯函数 evaluator：

- `Screen-Fail`：立即 finalize/close，封存已有制品，不消费 Confirm 输入。
- `Screen-Pass`：继续向同一个 executor 实例、`session_id` 和活动运行时发送 Confirm 输入。

同会话证明至少包含：

- `start_count == 1`；
- Screen/Confirm 共享 `session_id`；
- 模型、profile、runtime config 和 schedule hash 不变；
- 非故障注入场景中进程/epoch 身份不变；
- Confirm 的首个 cursor 严格接续 Screen 的末 cursor。

如果 executor 不支持 continuation，formal 请求在运行前失败，不能以两次独立运行拼接 Confirm。

## 9. Stage 3 与 Stage 5 的 60 分钟复用

会议 candidate 使用一个 stage=5 的物理运行和一个连续 60 分钟 schedule：

1. `0–5 min` 执行 preflight；通过后这 5 分钟同时计入 Stage 3 的 `0–30 min` 窗口。
2. `5–30 min` 继续 Stage 3 主会议，30 分钟处形成 checkpoint。
3. 后 30 分钟继续同一 session，并按固定 cursor 注入 Stage 5 故障。
4. 只有完整 60 分钟结束后才封存物理 run；随后从同一 ArtifactIndex 生成 Stage 3 和 Stage 5 两份
   逻辑决策报告。

这避免在运行中修改已经被 hash 的 manifest，也避免 Stage 3 report 与 Stage 5 artifact index 形成循环。
Stage 3 checkpoint 在 `events.jsonl` 和 `summary.json` 中记录，但不是运行中途的最终决策制品。

停止语义：

- preflight 或前 30 分钟硬门禁失败：按冻结规则早停，生成 Stage 3 失败结论；Stage 5 为
  `not_run`，不能是 `Promote`。
- 前 30 分钟通过、后半段失败：Stage 3 可依据已封存 checkpoint 独立分类；Stage 5 不能
  `Promote`。
- baseline 仍只运行一次 stage=3、30 分钟无故障会议，不伪造 Stage 5 证据。
- 交互 Stage 4 与 Stage 5 不共用运行：Stage 4 为 10–15 轮受控对话，唯一 finalist 另跑 60 分钟。

“Stage 3 失败结论”进一步区分：有完整可验证 observation 的预注册硬门禁失败产生 `Reject`；服务、
writer 或其他基础设施异常产生 run `failed` 和 `Experimental / No decision`，不得把基础设施失败伪装为
候选质量 `Reject`。Stage 5 后半段同样区分证据性硬门禁失败与实验基础设施失败。

## 10. Canonical cursor 与故障注入

- canonical cursor 只按成功提交给 executor 的输入音频毫秒数推进，不使用 wall clock。
- runner 在跨越故障 cursor 前把 resolved input 切成确定 cursor slice；executor 在 slice 内仍按冻结
  frame size 发送 PCM。runner 在两个 slice 之间注入故障，确保事件在精确 cursor 触发且仅触发一次。
- Stage 5 必须加载 `FaultPlan(stage=5, duration_ms=3_600_000)`，并核对固定
  `3 disconnect + 1 asr_crash + 1 finalization_delay`。
- `fault-execution.jsonl` 对每次事件记录 planned cursor、actual cursor、started、recovered、outcome、
  时长、故障前后 session/process/epoch 身份。
- fault 状态固定为 `planned → attempt_started → applied → recovered`，失败可转为 `failed`，进程在临界点
  消失且无法证明是否应用时为 `unknown`；每个 event ID 最多 attempt 一次。
- Promote 的 executed count 只统计 `applied` 且达到 `recovered` 的事件；尝试、failed、unknown 不能
  冒充成功数。
- 非 Stage 5 正式运行拒绝 fault plan；人工临时故障只能作为 `experimental` 新 run，不能混入正式证据。

`FaultEvent.cursor_ms` 始终相对于整个 60 分钟 session 起点，`duration_ms` 表示 wall-clock 故障持续
时间，不增加 canonical audio cursor；因此契约不再用 `cursor + duration` 判断音频越界。三个
disconnect 和一个 `asr_crash` 必须位于 `< 3_600_000 ms` 的冻结 cursor；
`finalization_delay` 固定在 `cursor_ms == 3_600_000` 且 `duration_ms > 0`。runner 不把它传给普通
`inject_fault()`，而是在全部音频提交后作为唯一参数传给 `finalize(finalization_fault)`；executor 必须
按“发送 EOF → 应用延迟 → 接收 terminal”执行。其含义是延迟 ASR/会议的
terminal/ready-to-stop 确认，不是重复发送音频或任意 sleep。实际允许 cursor 误差固定为 0 ms；
wall-clock 调度误差单独记录，不改变 fault identity。

Promote 中现有 `actual_duration_ms=3_600_000` 表示 canonical 连续音频覆盖；metrics 另记录从
`start()` 到 `finalize()` 的 monotonic wall elapsed，必须不少于 60 分钟并包含故障恢复时间。两者都从
runner observation 计算，禁止由 CLI 手填。

## 11. 制品与封存

每次运行位于项目外的唯一目录：

```text
<external-run-root>/<run_id>/
├── manifest.json
├── state.json
├── events.jsonl
├── metrics.json
├── resources.csv
├── fault-execution.jsonl
├── failures.jsonl
├── summary.json
├── vendor-events.jsonl       # 可选、脱敏、受限
└── artifact-index.json       # 最后写入
```

约束：

1. 目录权限固定 `0700`，文件固定 `0600`；不改变既有父目录权限。
2. 输出目录、临时文件和最终文件均拒绝 symlink；索引前重新确认每项为同一 run 目录下的 regular
   file。
3. JSON 使用同目录临时文件、`fsync`、原子 replace；JSONL/CSV 定期 flush，并在 checkpoint/terminal
   强制 `fsync`。
4. `ArtifactIndex` 绑定最终 `manifest.json` 的 SHA-256，并列出除自身和 manifest 外的全部最终制品
   相对路径、大小与 SHA-256；index 自身不纳入，避免哈希循环。
5. 正常或可捕获失败都必须先 close executor，再写 terminal manifest，最后写 artifact index。
6. decision report 位于独立外部 decision 目录，在 artifact index 之后生成并引用其 hash；它不反向
   纳入 run index。
7. 项目内只允许后续写入聚合报告、匿名 failure ID 和不含敏感正文的可复现元数据。
8. 异常堆栈、vendor payload 和错误消息写入前移除项目外根路径、用户名、token、URL query 和超长字段。

## 12. 决策证据链

`StageDecisionReport` 是序列化契约，不是证据验证器。新增 `verify_stage_decision()`，在生成报告前
读取并验证真实源文件：

1. run manifest 为 terminal 且身份与 family/candidate/stage 视图一致。
2. artifact index hash 正确，列出的每个 artifact 字节、大小和权限与索引一致。
3. metrics、fault execution、summary 的 hash 与语义字段一致，不接受调用方自报值。
4. Screen/Confirm 报告验证 schedule 边界、cursor 连续性和 start count。
5. Promote 验证 60 分钟连续 duration、固定故障成功计数、八个固定 hard gate、Stage 1–4 报告链和
   每个 family 唯一 finalist。
6. 每个上游 report 必须打开、重新哈希，并核对 family、candidate、evidence tier 和 git/model/profile/
   runtime identity；不能只检查 64 位字符串形状。
7. `experimental`、synthetic、unsealed、failed 或 deferred run 永远不能生成 `Promote`。
8. `unique_finalist` 从独立 finalist selection report 计算，不能由调用方传入布尔值；Stage 1–4 report
   链必须按顺序合法，且 family/candidate/identity 全部一致。

当前 `StageDecisionReport` 对所有状态都要求八个 hard-gate key。Stage 2–4 报告因此必须完整写出固定
registry，并以 `not_applicable` 或有证据的 `unsupported` 表达尚未测量项，不能传空字典或自造 gate。

Stage 3/5 共用会议运行时，两份 report 可以引用同一 run manifest/artifact index，但各自必须引用
对应 checkpoint/metrics slice 的 hash，不能把后半段故障指标冒充 Stage 3 主会议结果。

## 13. 资源互斥与执行纪律

正式 `run_stage()` 从身份与路径验证前开始调用现有 `exclusive_resource_lock()`，直到 executor 已关闭、
全部文件已 flush 且 artifact index 已落盘才释放。锁覆盖模型加载、服务、实验和封存，不只覆盖推理
循环。

执行规则：

- `lock_timeout_secs=0` 默认 fail-fast，已有 owner 时返回稳定 `ResourceBusyError`。
- 锁竞争时不得构造 executor、创建 run 目录或留下 partial output；公共 `run_stage()` 是唯一锁 owner，
  CLI 不重复嵌套加锁。
- 模型/服务/实验/全量测试严格串行；同一时刻最多一个高负载 owner。
- 只读文档审查和代码分析可并行，但不得修改同一文件或启动运行时。
- 一个实验结束后，先验证子进程、端口、文件描述符和资源锁均释放，再开始下一项。
- `CloseObservation` 不完整时，本 run 标记 `failed`；在显式人工处置前不得启动下一项高负载工作，
  不能删除所谓“过期锁”或按 PID 猜测抢占。
- 全量质量门禁五条命令顺序运行；不得与正式实验或模型服务并行。
- runner 本身不自动重试整 run。只有能证明与候选无关的基础设施故障，才由新 `run_id` 重跑并保留
  原失败证据。

为保证锁释放后仍不误启下一项，清理不完整时 runner 在默认项目外锁目录原子写入私有
`resource-quarantine.json`。后续 `run_stage()` 取得 flock 后必须先验证该 marker；存在时返回
`resource_quarantined`，不得构造 executor 或输出目录。只有显式资源审计确认 marker 记录的自有进程、
端口和连接均已消失后，才允许以可审计操作清除 marker；不得仅凭 PID 不存在或时间过期自动清除。

## 14. 阶段策略

| Stage | 输入与连续性 | 早停边界 | 完成输出 |
|:---:|:---|:---|:---|
| 2 | 15–20 分钟冻结 block；前 8–10 分钟 Screen；20 ms PCM | 延迟、EOF、状态机、资源硬门禁 | TTFP/TTFC、commit/finalization、revision、rollback、deadline |
| 3 baseline | 一次 30 分钟无故障会议 | 预注册系统硬门禁 | EOF、对账、gap、持久化、journal、模式闭环 |
| 3/5 candidate | 一次连续 60 分钟；前 30 分钟是 Stage 3 | preflight/前 30 分钟硬失败 | Stage 3 checkpoint + Stage 5 reliability |
| 4 | 10–15 轮固定话术；前 5 轮 Screen | 回声、外放恢复、延迟、状态硬门禁 | ASR/LLM/TTS 分段延迟、插话、误打断、自响应 |
| 5 interaction | 唯一交互 finalist 连续 60 分钟 | 隐私、安全、确定性崩溃等预注册硬门禁 | 长时资源、故障、恢复和 Promote 证据 |

`StagePolicy` 由 stage 和用途选择，必须是可复现纯函数；它只能消费已经记录的 observation，不能读取
reference、调用模型或修改原始制品。

## 15. 错误语义

稳定错误类别至少包括：

- `invalid_request`：契约、身份、stage/family/arm 不一致。
- `unsafe_path`：路径位于项目内、越界、符号链接逃逸或未显式授权。
- `identity_mismatch`：实际 hash 与冻结制品不一致。
- `resource_busy`：全局锁已被其他 owner 持有。
- `resource_quarantined`：前次运行清理不完整，尚未通过显式资源审计。
- `unsupported_executor`：executor 不支持 stage、continuation、格式或 fault。
- `execution_failed`：目标服务、进程、连接或 runtime 异常。
- `artifact_write_failed`：制品无法安全写入或封存。
- `unsealed_run`：缺少有效 artifact index。
- `evidence_mismatch`：决策源文件 hash、身份或语义不一致。

错误码稳定，具体错误消息不作为 API；原始异常作为 cause 保留在受限 failure 制品中。

## 16. 测试策略

### 16.1 P0 单元/合成集成测试

1. Screen 通过后 `start()` 仍只调用一次，session/cursor 连续；Screen 失败不消费 Confirm。
2. schedule/input manifest/实际字节三重 hash 校验，拒绝缺失、重复、路径逃逸和项目内 formal root。
3. 现有全局锁的竞争、超时、异常释放和权限行为保持不变。
4. 覆盖全部合法状态转移与非法回退；run 目录禁止覆盖。
5. executor、finalize、close 和 writer 各阶段失败时，尽可能保留 partial artifacts 与稳定失败原因。
6. Stage 5 只在精确 cursor 注入五个固定故障，完成计数与尝试计数分离。
7. Stage 3/5 只启动一个 meeting session，Stage 3 metrics slice 与 Stage 5 故障 slice 可分别验证。
8. synthetic/experimental、失败、未封存或不唯一 finalist 的证据不能构造 `Promote`。
9. 决策 verifier 检出篡改的 manifest、metrics、fault、artifact index 和上游 report。
10. 所有测试使用临时项目外目录，不启动模型、服务或真实 PostgreSQL，不读取私人录音。

### 16.2 真实 smoke 与正式验收

真实执行器仅在对应 Stage 1 family 产生唯一 finalist 后实现和运行：

- Stage 2：一个最短冻结片段验证连接、20 ms feed、partial/final、EOF 和 close。
- Stage 3/5：先验证 5 分钟 preflight，然后按同一 run 继续；不得把 smoke 结果计入 formal run。
- Stage 4/5：固定 LLM/TTS/VAD/回声身份，验证外放下一轮恢复与自回声为零。
- 每个 smoke 使用独立 `run_id` 和 `experimental` tier，正式 run 不复用其目录。

真实结果只在完整制品封存和 verifier 通过后才进入决策。

## 17. 实施与回退顺序

1. 先扩展契约和失败测试。
2. 实现 artifact writer 与哈希/权限/不可覆盖测试。
3. 实现 `StageExecutor`、合成 executor 和统一状态机。
4. 实现 stage evaluator、Stage 3/5 共用会话与决策 verifier。
5. 接入 CLI；formal CLI 默认无 synthetic fallback。
6. 运行全部质量门禁并提交统一执行器。
7. 执行 Stage 1；只为实际 finalist 接入相应真实 Stage 2–5 executor。
8. 严格串行执行 smoke、正式阶段、决策和生产增量收敛。

本规范对应的首个实施计划只覆盖步骤 1–6，即稳定核心、合成验证、CLI 和完整质量门禁；这是一个可独立
验收和回退的实现单元。步骤 7–8 属于后续实验执行程序：Stage 1 产生实际 finalist 后，只为仍存活的
family 设计并接入对应真实 executor。真实 adapter 若完全落在本规范接口内，可按边界清晰的后续变更
实施；若需要改变 `StageExecutor`、lineage 或证据契约，则必须先修订本规范并重新取得批准。

任一步均可回退到前一提交；新增 runner 不改变生产默认后端。失败运行和正式证据只归档、不覆盖、不
自动删除。若没有 family finalist，后续 stage 只记录 `deferred/not_run`，不为满足流程而执行无意义长跑。

## 18. 验收标准

1. Stage 2/4 Screen→Confirm 可由制品证明同 executor、同 session、同身份、连续 cursor。
2. Stage 3/5 candidate 只消耗一次连续 60 分钟运行，且两阶段指标切片不能互相冒充。
3. 输入、模型、profile、runtime、schedule、fault plan 和 Git 身份均由 runner 读取实际制品后绑定。
4. 正常、早停和可捕获失败均有私有、不可覆盖、可 hash 验证的证据。
5. Stage 5 的时长、五个故障和八个 hard gate 来自真实源文件验证，不来自调用者自报字段。
6. synthetic/experimental、failed、deferred、unsealed 或非唯一 finalist 不能产生 `Promote`。
7. 所有模型、PCM、reference、逐字稿和原始结果均在项目外，项目内不出现私人内容或绝对路径。
8. 全局资源锁覆盖一次 run 的完整高负载生命周期，所有模型/服务/实验/全量门禁保持串行。
9. 不改变现有生产运行时默认后端、模式协调、回声双防线、会议持久化和零音频存储约束。
10. 全部后端、类型、lint、前端测试和前端生产构建门禁通过。

## 19. 已知取舍与后续边界

- runner 进程被不可捕获终止时不自动续跑；牺牲续跑便利性以保留正式实验不可篡改性。
- decision report 不纳入 run artifact index，以单向引用消除哈希循环；report 自身由上层实验索引绑定。
- 真实 executor 延迟到 finalist 产生后实现，减少无效接入和资源消耗；统一契约和合成测试先行。
- Stage 3 checkpoint 在物理 run 完成后才形成最终报告，因此不会在前 30 分钟结束时立即给出正式结论。
- 本设计不解决跨主机调度或分布式锁；当前科学对比限定在同一台本机串行执行。

## 20. 证据边界

- `StageRunManifest`、`ArtifactIndex` 和 `ScheduleManifest` 当前仅被契约测试引用，没有现有执行流；
  因此本任务属于新增子系统而非在既有 runner 上增加一个小分支。
- `exclusive_resource_lock()` 已被 ASR benchmark runner 和多进程测试使用，提供 POSIX fail-fast 排他锁、
  `0700/0600` 权限和异常释放，统一执行器应复用而非复制。
- Codebase Memory 对 `stage_contracts.py`、`resource_lock.py`、`cli.py` 及相关测试的 coverage 结果均为
  `no_recorded_issue/metadata_match`；该信号是 best-effort。
- 测试时长、阶段复用、故障计数和 Promote 八门禁以 v1.2 上位测试方案为准；若上位方案再次变更，
  必须先更新本设计和冻结 schedule，不能在运行中临时解释。
