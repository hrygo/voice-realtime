# ASR Scientific Comparison Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 Qwen3-ASR、SenseVoiceSmall 与 Fun-ASR-Nano 的可复现本机科学对比，分别选出字幕/会议和交互助手的唯一生产后端，并在生产验收后删除落选模型与专用接入。

**Architecture:** 科学 runner 只依赖统一 `StreamingTranscriber` 契约；三种原生离线模型各由独立 adapter 吸收 vendor 差异。Qwen 官方 `qwen-asr==0.0.6` 与主项目 Transformers 版本冲突，因此通过 WhisperLiveKit 的隔离 Python 环境和持久子进程 worker 推理，不修改主项目依赖锁。所有模型加载、服务启动、基准运行和全量测试都先取得项目外的主机级排他锁并由主 Agent 串行调度；只有 Stage 1 质量门禁通过的候选才建设流式 runtime，最终生产配置固定一个后端，不建设运行时切换。

**Tech Stack:** Python 3.12、asyncio、PyTorch/MPS、FunASR、WhisperLiveKit/Qwen3-ASR、Pydantic v2、pytest、cluster bootstrap、PostgreSQL（仅系统链路 confirmed 文本）。

**Spec:** `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

**Decision:** `docs/decisions/0004-asr-sequential-evaluation.md`

**Spec Revision:** v1.2（60 分钟 Core + 45 分钟 Reserve、两 look alpha spending、finalist-only 验收）。

**Execution status (2026-08-24):** Task 1 已按 TDD 完成并提交（`4a73bd9`、`ffbf810`）；Task 2/3 的
Qwen 隔离 worker、Qwen adapter 与 SenseVoice CPU adapter 基础层已提交（`e8a23b2`）；Task 2–4 的
运行级资源复用、离线 profile 调度、冻结身份核验和 runner 接线已提交（`7d3e241`）。ASR/benchmark
专项测试、strict mypy 与 ruff 已通过。2026-08-25 已批准 v1.2 时间优化；Qwen/Sense/Fun MPS Stage 0
已按同一协议串行完成，Fun CPU 保留历史兼容证据。三个 primary 臂均为 `feasible`，模型和服务已停止。

## Global Constraints

- Python 严格保持 `>=3.12,<3.13`，默认离线且禁止隐式下载。
- 模型、语料、逐字稿和逐样本实验产物只放项目外；Git 只保存契约、聚合指标和脱敏报告。
- 主机排他锁默认位于用户 cache 根目录的 `voice-realtime/locks/asr-experiment.lock`；锁目录 `0700`、锁文件 `0600`。
- 模型加载、ASR/TTS/LLM 服务、基准实验、故障注入和全量质量门禁不得并行；服务启动前再次检查端口与锁持有者。
- worker 只允许并行进行只读调查或修改彼此不重叠的文件；测试、模型加载、提交与删除由主 Agent 串行执行。
- `PYTORCH_ENABLE_MPS_FALLBACK=0`；MPS 实验必须验证真实参数 device，失败单列 `infeasible`，不得静默转 CPU。
- `blind-core` 与 `blind-reserve` 的 reference、cluster、顺序、manifest、候选和阈值必须在任何 Core
  输出可见前同时冻结；只允许在 60/105 分钟两个固定 look 决策。
- 目标域最大 blind 固定为 105 分钟、约 98–105 个独立切片、约 2.2 万字；Core 60 分钟、Reserve
  45 分钟均覆盖全部主层，正交多标签不得重复累计唯一音频时长。
- 确定性准确率只运行一次序贯实验；性能只在短 `perf-block` 运行 3 次（1 cold + 2 warm），禁止
  完整 blind 重复三遍或额外 seed sweep。所有机器墙钟按实际 RTF 和进入阶段的臂数累加。
- 完整 Public 只对最终 baseline + winner 运行，移出选型关键路径；Fun CPU 在 MPS 可行时不进入正式排名。
- 会议不保存音频；保留 `EchoSuppressionProcessor` 与 `SelfEchoFilter` 双层回声防线。
- 删除模型或生产接入只在最终分类、真实试运行和回退证据完成后执行，并精确记录删除清单。

## Revised Time Budget (Spec v1.2)

| 范围 | 冻结规模 / 单臂预算 | 执行说明 |
|---|---:|---|
| Public Reproducibility | 1–2h 音频 | 只在最终 baseline + winner 运行，不阻塞选型 |
| Target-domain Blind | 60m Core + 45m Reserve | 最大 105m；Reserve 只由 `Continue` family 开封 |
| 标注 | 约 10–15 人工工时 | 全部 reference 在 Core 前冻结；5m calibration，不达标扩至 15m |
| Stage 1 | 按 $60/105\times$ 各臂 RTF | Qwen、Sense、Fun MPS；Fun 输出复用两个决策，Fun CPU 不排名 |
| Stage 2 | 每臂 8–10m Screen，通过后延长至总计 15–20m | baseline/finalist 同一冻结 block，不重复启动 |
| Stage 3 | baseline 30m；candidate 60m 的前 30m | 前 5m 是同一会话 preflight，不另跑 15m |
| Stage 4 | 每臂 5 轮 Screen，通过后延长至总计 10–15 轮 | baseline/finalist 同一冻结话术与 session |
| Stage 5 | 每决策方向最多 1×60m | 会议与 Stage 3 共用 candidate 连续会话；交互 finalist 另跑 |
| 生产收敛 | 3–5 轮增量 smoke | 身份不变时不重复 Stage 3/4/5 或固定 30 轮 |
| 全实验机器墙钟 | Core 早决策约 4.4–5.1h；Reserve 全开约 4.5–5.3h | 以 Stage 0 三臂实测 warm RTF 排程；实时长跑为主体 |

---

### Task 1: 建立主机级实验排他锁

**Files:**
- Create: `src/voice_realtime/benchmarks/resource_lock.py`
- Modify: `src/voice_realtime/benchmarks/asr/cli.py`
- Create: `tests/benchmarks/test_resource_lock.py`
- Modify: `tests/benchmarks/test_asr_cli.py`

**Interfaces:**
- Produces: `exclusive_resource_lock(path: Path | None, timeout_secs: float) -> ContextManager[ResourceLockMetadata]`。
- Preserves: `score`、`compare` 为纯分析命令，不争抢模型锁；`run` 从模型身份校验前到产物封存后全程持锁。

- [x] **Step 1: 写竞争与权限 RED 测试**

覆盖首次持锁、同进程第二个非阻塞竞争者收到稳定 `RESOURCE_BUSY`、超时、异常退出自动释放、锁目录
`0700`、锁文件 `0600`，以及 `score`/`compare` 不取锁。测试使用 `tmp_path` 和独立子进程，不触碰默认
主机锁。

- [x] **Step 2: 运行 RED**

Run: `uv run pytest tests/benchmarks/test_resource_lock.py tests/benchmarks/test_asr_cli.py -q --no-cov`

Expected: `voice_realtime.benchmarks.resource_lock` 不存在而失败。

- [x] **Step 3: 实现最小排他锁**

使用 macOS/POSIX `fcntl.flock(LOCK_EX | LOCK_NB)`；采用单调时钟和短轮询实现有界等待。锁内容仅写 PID、
UTC started_at、command 与 run_id，不写环境变量。CLI 增加 `--resource-lock` 和
`--lock-timeout-secs`，默认路径通过 `Path.home()` 解析到项目外 cache。

- [x] **Step 4: 运行 GREEN 与回归**

Run: `uv run pytest tests/benchmarks/test_resource_lock.py tests/benchmarks/test_asr_cli.py tests/benchmarks/test_asr_replay.py -q --no-cov`

Expected: PASS，且现有 runner 产物契约不变。

- [x] **Step 5: 提交**

```bash
git add src/voice_realtime/benchmarks tests/benchmarks
git commit -m "feat(asr): 增加实验主机资源排他锁"
```

### Task 2: 增加 Qwen3-ASR 原生离线实验臂

**Files:**
- Create: `src/voice_realtime/asr/adapters/qwen3_native.py`
- Create: `src/voice_realtime/asr/workers/__init__.py`
- Create: `src/voice_realtime/asr/workers/qwen3_native_worker.py`
- Modify: `src/voice_realtime/asr/adapters/__init__.py`
- Modify: `src/voice_realtime/asr/profiles.py`
- Modify: `src/voice_realtime/asr/defaults.py`
- Modify: `src/voice_realtime/benchmarks/asr/cli.py`
- Create: `tests/asr/test_qwen3_native_adapter.py`
- Modify: `tests/asr/test_profiles.py`
- Modify: `tests/asr/test_defaults.py`
- Modify: `tests/benchmarks/test_asr_cli.py`

**Interfaces:**
- Produces: `Qwen3NativeProfile(kind="qwen3-asr-native")`、`Qwen3NativeEngine`、
  `Qwen3NativeAdapter`，backend ID `qwen3-asr-native`。
- Consumes: 项目外固定 Qwen3-ASR-1.7B snapshot、WhisperLiveKit 隔离解释器、内存 16kHz mono float32/PCM、冻结 language/context。

- [x] **Step 1: 写 vendor 行为与 adapter RED 测试**

以 fake processor/model 覆盖：模型一次加载、多样本复用；PCM 合并只发生在 adapter 内存；中文、英文
与 auto 语言；context 长度边界；空白输出；vendor 异常映射；最终窗口边界；MPS 参数 device 检查；
非 `offline` mode 拒绝。

- [x] **Step 2: 运行 RED**

Run: `uv run pytest tests/asr/test_qwen3_native_adapter.py tests/asr/test_profiles.py tests/benchmarks/test_asr_cli.py -q --no-cov`

Expected: 新 profile 和 adapter import 失败。

- [x] **Step 3: 实现最小原生离线边界**

主项目 adapter 启动一个持久隔离 worker，并用有界二进制帧传输元数据和 PCM；worker 按当前本机
`qwen-asr` 真实签名加载本地 snapshot，分别对 model/processor 强制本地路径和离线边界。输入统一为
16kHz；只接受结构合法且包含实际 device/dtype 的结果。profile 冻结隔离解释器、device、dtype、
language_source、context 和 decoder 参数，manifest 逐字段核对；任何 CPU fallback 都失败。

- [x] **Step 4: 运行 GREEN 与类型检查**

Run:

```bash
uv run pytest tests/asr/test_qwen3_native_adapter.py tests/asr/test_profiles.py tests/asr/test_defaults.py tests/benchmarks/test_asr_cli.py -q --no-cov
uv run mypy src/voice_realtime/asr/adapters/qwen3_native.py src/voice_realtime/asr/workers/qwen3_native_worker.py src/voice_realtime/benchmarks/asr/cli.py
```

Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add src/voice_realtime/asr tests/asr src/voice_realtime/benchmarks/asr/cli.py tests/benchmarks/test_asr_cli.py docs/superpowers/plans/2026-08-24-asr-scientific-comparison-completion.md
git commit -m "feat(asr): 增加Qwen3原生离线实验臂"
```

### Task 3: 增加 SenseVoiceSmall 原生离线实验臂

**Files:**
- Create: `src/voice_realtime/asr/adapters/sensevoice_native.py`
- Modify: `src/voice_realtime/asr/adapters/__init__.py`
- Modify: `src/voice_realtime/asr/profiles.py`
- Modify: `src/voice_realtime/asr/defaults.py`
- Modify: `src/voice_realtime/benchmarks/asr/cli.py`
- Create: `tests/asr/test_sensevoice_native_adapter.py`
- Modify: `tests/asr/test_profiles.py`
- Modify: `tests/asr/test_defaults.py`
- Modify: `tests/benchmarks/test_asr_cli.py`

**Interfaces:**
- Produces: `SenseVoiceNativeProfile(kind="sensevoice-native")`、`SenseVoiceNativeEngine`、
  `SenseVoiceNativeAdapter`，backend ID `sensevoice-native`。
- Preserves: 与生产 Pipecat 基线一致的 `device="cpu"`、`use_itn=True` 和本地 snapshot 解析。

- [x] **Step 1: 写 vendor 行为与 adapter RED 测试**

覆盖 `AutoModel.generate()` 的真实参数形状、模型一次加载、逐样本 language、ITN、空输出、tag 清理、
异常映射、CPU-only profile、外部绝对模型路径与非离线 mode 拒绝。

- [x] **Step 2: 运行 RED**

Run: `uv run pytest tests/asr/test_sensevoice_native_adapter.py tests/asr/test_profiles.py tests/benchmarks/test_asr_cli.py -q --no-cov`

Expected: 新 profile 和 adapter import 失败。

- [x] **Step 3: 实现最小原生离线边界**

复用当前 `resolve_stt_model()` 的离线快照规则，以 FunASR 本机源码的真实输入类型调用 SenseVoice；
禁止 hub 更新和下载。输出在 adapter 边界清理 SenseVoice emotion/event/language tags，但原始 vendor
结果仍进入受限事件文件。

- [x] **Step 4: 运行 GREEN 与生产等价回归**

Run:

```bash
uv run pytest tests/asr/test_sensevoice_native_adapter.py tests/asr/test_pipecat_sensevoice.py tests/test_pipeline.py tests/benchmarks/test_asr_cli.py -q --no-cov
uv run mypy src/voice_realtime/asr/adapters/sensevoice_native.py
```

Expected: PASS，生产 `PipecatSenseVoiceFactory` 默认参数不变。

- [x] **Step 5: 提交**

```bash
git add src/voice_realtime/asr tests/asr src/voice_realtime/benchmarks/asr/cli.py tests/benchmarks/test_asr_cli.py
git commit -m "feat(asr): 增加SenseVoice原生离线实验臂"
```

### Task 4: 统一离线 profile 调度与冻结身份校验

**Files:**
- Create: `src/voice_realtime/benchmarks/asr/backend_factory.py`
- Modify: `src/voice_realtime/benchmarks/asr/cli.py`
- Modify: `src/voice_realtime/asr/profiles.py`
- Create: `tests/benchmarks/test_asr_backend_factory.py`
- Modify: `tests/benchmarks/test_asr_cli.py`

**Interfaces:**
- Produces: `BenchmarkBackendFactory.create(profile, manifest) -> BenchmarkBackendRuntime`。
- Preserves: 三个原生 engine 每个 run 只加载一次，WS profile 仍走 loopback，所有原生 profile 只接受
  `--mode offline`。

- [x] **Step 1: 写判别调度 RED 测试**

覆盖五种现有 profile 与两种新 profile、未知 profile、device/dtype/parameters 不一致、逐样本语言复制、
模型目录逃逸和原生 profile 错用 realtime。

- [x] **Step 2: 运行 RED**

Run: `uv run pytest tests/benchmarks/test_asr_backend_factory.py tests/benchmarks/test_asr_cli.py -q --no-cov`

Expected: factory 模块不存在而失败。

- [x] **Step 3: 从 CLI 抽离构建职责**

CLI 只负责解析、验证和持锁；factory 拥有 profile → engine/registry 映射与 manifest 身份核对，避免后续
增加实验臂时继续扩大条件分支。

- [x] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/benchmarks tests/asr -q --no-cov`

Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add src/voice_realtime/benchmarks/asr tests/benchmarks src/voice_realtime/asr/profiles.py tests/asr/test_profiles.py
git commit -m "refactor(asr): 统一基准后端构建与身份核验"
```

### Task 5: 建立外部语料制备与盲测冻结工具

**Files:**
- Create: `src/voice_realtime/benchmarks/asr/corpus.py`
- Create: `src/voice_realtime/benchmarks/asr/analysis_plan.py`
- Modify: `src/voice_realtime/benchmarks/asr/cli.py`
- Create: `tests/benchmarks/test_asr_corpus.py`
- Create: `tests/benchmarks/test_asr_analysis_plan.py`
- Modify: `tests/benchmarks/test_asr_manifest.py`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

**Interfaces:**
- Produces: `vr-asr-benchmark prepare-corpus` 与 `freeze-analysis`。
- Produces outside repository: 16kHz mono s16le PCM、`dev.json`、`blind-core.json`、
  `blind-reserve.json`、`reliability.json`、`analysis-plan.json` 与 checksums。

- [x] **Step 1: 写隐私、格式与冻结 RED 测试**

覆盖 WAV/FLAC 统一转码身份、时长/hash 校验、相对路径、symlink 逃逸、重复 sample ID、许可/同意缺失、
blind reference 不可读模式、正交多标签、60m Core/45m Reserve 分层配额、两个 manifest hash、cluster
跨 look 泄漏、analysis plan alpha/seed/MDE/conditional power、已冻结文件拒绝覆盖。配额按唯一音频
时长计算，禁止标签重复计时；Core/Reserve session 与 speaker 均不得重叠。

- [x] **Step 2: 运行 RED**

Run: `uv run pytest tests/benchmarks/test_asr_corpus.py tests/benchmarks/test_asr_analysis_plan.py tests/benchmarks/test_asr_manifest.py -q --no-cov`

Expected: corpus/analysis_plan 模块不存在而失败。

- [x] **Step 3: 实现确定性制备与冻结**

只调用本机可验证的 `ffmpeg` 进行一次转码，记录原始与 PCM SHA-256；reference/hypothesis 共用版本化
归一化函数。生成 `blind-core.json`、`blind-reserve.json` 与不可变配额摘要；两段 reference 同时进入
加密/权限受限封存，runner 在对应 look 正式开封前不得读取。`analysis-plan.json` 写入
`look_alpha=[0.01,0.04]`、`conditional_power_futility=0.20`、两个 bootstrap seed 和固定候选集合。

- [ ] **Step 4: 制备公开集、dev 与 blind 目录**

在 `~/.cache/voice-realtime/benchmarks/asr/corpora/` 下创建版本化目录。Public 先冻结版本、许可、来源
与 checksum，但完整 1–2 小时运行延后到 baseline + winner。目标域同时冻结 60m Core/约 58 切片与
45m Reserve/约 40 切片；两段各覆盖近讲、会议、code-switch、口音、噪声、实体和负样本，合计至少
20 名全局唯一说话人。Reliability Set 固定为 1×60m canonical cursor，不与 2×30m 重复执行。
目标域只纳入已授权录音；不足配额时明确缺口，禁止用合成音频冒充 blind。

- [x] **Step 5: 运行 GREEN 并提交工具与文档**

Run: `uv run pytest tests/benchmarks/test_asr_corpus.py tests/benchmarks/test_asr_analysis_plan.py tests/benchmarks/test_asr_manifest.py -q --no-cov`

```bash
git add src/voice_realtime/benchmarks/asr tests/benchmarks docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "feat(asr): 增加语料制备与盲测冻结工具"
```

### Task 6: 串行补齐三模型四实验臂 Stage 0 门禁

**Files:**
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`
- Create: `docs/benchmarks/asr/stage0-v12-20260825/report.md`
- Create: `docs/benchmarks/asr/stage0-v12-20260825/summary.csv`

- [x] **Step 1: 确认独占环境**

确认 8100/8765/8001/10095 无监听，主机实验锁可独占取得，网络下载关闭，模型位于项目外且 hash 与
manifest 一致。先核验既有 Fun MPS/CPU Stage 0 的 commit、profile、模型 hash 与产物。因 runner 超时
收敛修复改变 Fun MPS 执行身份，最终统一重跑 Fun MPS；Fun CPU 仍标记 `reused`。不得同时运行任何
其他模型或全量测试。

- [x] **Step 2: 冻结 Qwen3 与 SenseVoice Stage 0 manifests**

使用与既有 Fun-ASR Stage 0 相同 10 条门禁语料、PCM bytes、分段和逐样本 language；分别固定 Qwen3
MPS、SenseVoice CPU、Fun-ASR MPS 的模型文件全量 hash 和 profile 参数。Stage 0 只要求普通话、英文、静音、
结构、真实 device 和资源释放，不得扩展成准确率实验。

- [x] **Step 3: 依次运行，不并行**

顺序执行 Qwen3 MPS → SenseVoice CPU → Fun-ASR MPS；随后只读取核验既有 Fun-ASR CPU 结果。每臂
进程退出且锁释放后才开始下一臂。Fun MPS 可行时，Fun CPU 保留设备兼容证据但不进入 Stage 1 正式排名。

- [x] **Step 4: 汇总可行性而不宣称质量**

报告新运行的加载时间、warm RTF、峰值 RSS、失败和输出一致性，并明确区分 `executed` 与 `reused`；
模型自带/合成样例不进入正式准确率结论。Qwen 实测 RTF 回填后重算 v1.2 全实验墙钟。

- [x] **Step 5: 验证并提交聚合报告**

Run: `uv run pytest tests/benchmarks tests/asr -q --no-cov`

```bash
git add docs/Fun-ASR与现有ASR后端科学对比测试方案.md docs/benchmarks/asr/stage0-v12-20260825
git commit -m "docs(asr): 回填三模型Stage0门禁结果"
```

### Task 7: 冻结并执行 Stage 1 dev 参数选择

**Files:**
- Create: `docs/benchmarks/asr/stage1-v12-20260825/analysis-plan.json`
- Create: `docs/benchmarks/asr/stage1-v12-20260825/dev-report.md`
- Create: `docs/benchmarks/asr/stage1-v12-20260825/dev-summary.csv`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

- [ ] **Step 1: 验证 dev 语料独立性与功效输入**

确认 dev 未进入 Core/Reserve，分层、说话人、许可、参考标注与 hash 完整；以会议/录音为 cluster，
禁止以字符为独立样本。先在 5 分钟分层 `label-calibration-pilot` 验证一致性；normalized CER 差异
超过 1.0 个绝对百分点时扩展至 15 分钟并修订规范，再一次性冻结完整 Core/Reserve reference。

- [ ] **Step 2: 冻结等调参预算**

Qwen、SenseVoice、Fun MPS 分别限定相同数量的预声明配置；基础无 context 是唯一 blind primary。
统一 context 与生产 context 只在 dev/finalist 子集运行。固定归一化、设备、dtype、线程、decoder、
单一 seed 与短 `perf-block`；Fun CPU 不参与。

- [ ] **Step 3: 采用 Latin square 串行运行**

每个 block 顺序轮换，但任何时刻只加载一个模型。确定性 greedy 准确率对完整 dev 只运行 1 次；性能
只在短 `perf-block` 运行 3 次（1 cold + 2 warm），禁止完整 dev 重复和 seed sweep。检测 thermal
throttling 时整 block 作废并保留原因，不能删改单个差结果。

- [ ] **Step 4: 生成统计与功效报告**

报告 macro/micro CER、S/D/I、WER/MER、实体/数字/严重语义错误、失败率、资源和 10,000 次配对
cluster bootstrap。分别模拟 60m Core 与 105m 完整设计；目标完整 power `>0.85`、双侧 family-wise
alpha 0.05、最小相对 CER 改善 5%。如实测 pilot 方差不支持，blind 开封前调整设计或把结论降级为
`Experimental`，不得直接复用预计 power。

- [ ] **Step 5: 冻结 blind `analysis-plan.json` 并提交聚合结果**

文件写入主 endpoints、固定 Holm family、`look_alpha=[0.01,0.04]`、Core 99%/final 96% CI、
`conditional_power_futility=0.20`、MDE、两个 bootstrap seed、Core/Reserve manifest SHA-256、cluster
分配、候选 profile SHA-256 与唯一允许的停止状态。

```bash
git add docs/benchmarks/asr docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "docs(asr): 冻结Stage1盲测分析计划"
```

### Task 8: 执行 Stage 1 Core/Reserve 并做序贯晋级决策

> 2026-08-25 前置实现已完成：确定性 `report.py`、Holm family、Core/Final 状态机、formal/exploratory
> 隔离、正式 cluster 门禁、metadata-only preflight 与 formal freeze 语义绑定均已有测试。正式 Core/Reserve
> 尚未执行；必须等待获授权目标域音频、双人标注/裁决和两段 reference 同时封存。

**Files:**
- Create: `src/voice_realtime/benchmarks/asr/report.py`
- Create: `tests/benchmarks/test_asr_report.py`
- Create: `docs/benchmarks/asr/stage1-v12-20260825/blind-report.md`
- Create: `docs/benchmarks/asr/stage1-v12-20260825/blind-summary.csv`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

- [x] **Step 1: 写报告门禁 RED 测试**

覆盖配对样本缺失、失败率、macro/micro/分层、Core 99%/final 96% CI、每 look Holm、固定候选集合、
conditional power、两 manifest 并集、look 之外停止拒绝，以及 `Advance-Early/Reject-Hard/
Reject-Futility/Continue/Finalist/Experimental` 分类。

- [x] **Step 2: 实现并验证确定性报告器**

Run: `uv run pytest tests/benchmarks/test_asr_report.py tests/benchmarks/test_asr_metrics.py -q --no-cov`

Expected: PASS。

- [ ] **Step 3: 核验冻结身份后串行执行 Core look**

核对 code commit、Qwen/Sense/Fun MPS profile、Core/Reserve/analysis hashes 与排他锁，再依次运行
Qwen Core → SenseVoice Core → Fun MPS Core。Fun 输出只生成一次并同时进入两个 family 的配对分析。
性能 3 次仅使用冻结 `perf-block`。完整 Public 与 Fun CPU 不运行。失败样本保留，只有可证明与模型
无关的基础设施故障才能整 block 重跑。

- [ ] **Step 4: 程序化决策并按需追加 Reserve**

冻结程序分别输出两个 family 状态；禁止人工查看 hypothesis 后决定。先对 `Continue` family 的 Qwen
和/或 SenseVoice baseline 依次追加 Reserve；只要任一 family 为 `Continue`，Fun Reserve 统一运行
一次并复用。随后以 Core+Reserve 全量 cluster 重算。多候选仍可能获胜时全部完成 Reserve，禁止
60m 与 105m 直接排名。Stage 1 只能产生 Reject、Experimental 或
`Finalist / Reliability Pending`，不能直接生产 Promote。

- [ ] **Step 5: 提交代码与聚合报告**

```bash
git add src/voice_realtime/benchmarks/asr/report.py tests/benchmarks/test_asr_report.py docs/benchmarks/asr docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "feat(asr): 生成Stage1序贯盲测决策报告"
```

### Task 9: 仅为 Stage 1 晋级的候选建设本机流式 runtime

**Conditional:** 仅当字幕/会议 family 将 Fun-ASR 分类为 `Finalist / Reliability Pending` 时执行；
否则标记 `not-applicable`，不得为其增加生产服务。交互 family 不需要单独建设此字幕 WS runtime。

**Files:**
- Create: `src/voice_realtime/asr/services/funasr_nano_streaming.py`
- Create: `src/voice_realtime/asr/services/__init__.py`
- Create: `tests/asr/test_funasr_nano_streaming_service.py`
- Modify: `pyproject.toml`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

- [ ] **Step 1: 写协议、背压与 EOF RED 测试**

覆盖 loopback-only、单模型实例、单并发/有界队列、START/LANGUAGE/HOTWORDS/PCM/STOP、partial/final、
超时、断线、重复 STOP、客户端取消、排他锁和服务退出释放端口。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/asr/test_funasr_nano_streaming_service.py -q --no-cov`

Expected: service 不存在而失败。

- [ ] **Step 3: 实现最小本机服务**

只使用 Stage 1 finalist 配置；模型位于项目外；不保存 PCM；所有队列有界；服务生命周期持有与 benchmark
相同的主机锁，避免任何第二模型或实验并发。

- [ ] **Step 4: 运行 GREEN 与资源回收测试**

Run: `uv run pytest tests/asr/test_funasr_nano_streaming_service.py tests/asr/test_funasr_nano_ws_adapter.py -q --no-cov`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/voice_realtime/asr/services tests/asr/test_funasr_nano_streaming_service.py pyproject.toml docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "feat(asr): 增加Fun-ASR本机流式候选服务"
```

### Task 10: 串行执行 Stage 2 流式 Screen 与 Confirm

> 2026-08-25 共用前置契约已完成：`stage_contracts.py` 冻结 Screen→Confirm 顺序、schedule/fault/config
> hash、Stage 5 固定故障预算和 artifact index，并禁止 Stage 2–4 输出 `Promote`；SubtitleProxy 的重连
> gap 已改为 canonical 输入游标的真实非零区间。候选专用 harness 仍严格等待 Stage 1 finalist。

**Files:**
- Create: `docs/benchmarks/asr/stage2-v12-20260825/report.md`
- Create: `docs/benchmarks/asr/stage2-v12-20260825/summary.csv`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

- [ ] **Step 1: 冻结每臂同一条 15–20 分钟 block，前 8–10 分钟为 Screen window，固定相同 20ms PCM/1× schedule**
- [ ] **Step 2: 先运行 Qwen Screen，通过则原 run 延长至 Confirm 后释放；再以相同规则运行 Fun，切换前确认端口、进程和锁释放**
- [ ] **Step 3: Screen 报告 TTFP、TTFC、finalization、revision、rollback、deadline miss、失败率与硬门禁**
- [ ] **Step 4: 配对汇总两臂 Confirm；任一臂只完成 Screen 时不得宣称正式延迟非劣，词时间戳缺失时 commit latency 标 `unsupported`**
- [ ] **Step 5: 按 Confirm 延迟非劣与 6.4 秒 finalization 硬门禁决定是否进入 Stage 3**

```bash
git add docs/benchmarks/asr/stage2-* docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "docs(asr): 回填Stage2流式对比结果"
```

### Task 11: 串行执行 Stage 3 与会议候选连续长跑

**Files:**
- Create: `tests/experiments/test_asr_stage3_system.py`
- Create: `docs/benchmarks/asr/stage3-v12-20260825/report.md`
- Modify: `docs/会议助手后端运行与前后端联调.md`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

- [ ] **Step 1: 写可重复的系统场景测试**

覆盖字幕订阅、`assistant → meeting → idle → assistant`、30 分钟 baseline 和 60 分钟 candidate
连续会话。candidate 前 5 分钟是同一会话 preflight，前 30 分钟构成 Stage 3 主会议，后 30 分钟
继续运行并按固定 cursor 注入 EOF 超时、断线、崩溃、慢客户端、epoch 重连、journal 回放和
exactly-once persistence；不另跑 15 分钟冒烟。

- [ ] **Step 2: 为每个实验臂重建独立临时 PostgreSQL schema**

严格检查测试 DSN，结束后执行 `DROP SCHEMA ... CASCADE`；不得读取或写入生产会议数据。

- [ ] **Step 3: 固定同一 Sortformer 并依次运行**

顺序运行 Qwen 30m 无故障 baseline → 完全释放 → Fun 60m candidate。Sortformer 与 ASR 服务不得
并行于另一实验臂；candidate 前 30m 硬失败立即停止。记录 DER/JER/SA-CER、speaker flip、EOF、
gap、重复 segment、内存斜率和恢复结果。

- [ ] **Step 4: 验证隐私和恢复硬门禁**

确认项目运行目录、数据库和 journal 无音频 payload，journal 权限/内容符合边界；candidate 60m
产物写入可由 Task 13 复用的 runtime/profile/model/config 身份 hash，不得在 Task 13 重跑会议长跑。

- [ ] **Step 5: 提交测试与聚合报告**

```bash
git add tests/experiments/test_asr_stage3_system.py docs/benchmarks/asr/stage3-* docs/会议助手后端运行与前后端联调.md docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "test(asr): 完成Stage3字幕会议系统验收"
```

### Task 12: 串行执行 Stage 4 交互助手链路

**Files:**
- Create: `tests/experiments/test_asr_stage4_interaction.py`
- Create: `docs/benchmarks/asr/stage4-v12-20260825/report.md`
- Modify: `docs/实时语音交互与字幕-方案与最佳实践.md`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

- [ ] **Step 1: 写固定链路实验测试**

固定 VAD、LLM、TTS、`silence_secs=0.45`、相同话术与双层回声防线，只替换 STT factory。SenseVoice
与 Fun 各预冻结同一组 10–15 轮话术，前 5 轮是 Screen：短指令、长问句、数字/人名、TTS 播报后
外放下一轮输入、插话/回声各一次。通过时保持当前 session 并延长到总计 10–15 轮 Confirm；不固定
执行旧计划的 30 轮。

- [ ] **Step 2: 依次启动所需单一服务并检查锁/端口**

任何时刻只运行当前 STT 与固定 LLM/TTS；先执行 Sense 的 5 轮 Screen，通过则原 session 继续至
10–15 轮 Confirm，随后完全释放；再以相同规则执行 Fun。每臂结束后验证端口和锁释放。

- [ ] **Step 3: 测量分段延迟与交互安全**

报告停说→final、LLM 首 token、TTS 首音频、插话成功、误打断和机器人自响应。机器人自响应或绕过
回声防线即 Reject。

- [ ] **Step 4: 分析语音播报后下一轮输入恢复**

回归验证 TTS 状态结束、echo tail hangover 与外放模式输入重新开启时序，确保此前“不稳定空等待”
修复在每个 Screen 都无回退。Confirm 产物记录完整身份 hash，Task 14 身份相同时直接复用，不重复
10–15 轮。

- [ ] **Step 5: 提交测试与聚合报告**

```bash
git add tests/experiments/test_asr_stage4_interaction.py docs/benchmarks/asr/stage4-* docs/实时语音交互与字幕-方案与最佳实践.md docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "test(asr): 完成Stage4交互链路验收"
```

### Task 13: 汇总会议证据并执行交互 Finalist Stage 5

**Files:**
- Create: `tests/experiments/test_asr_stage5_reliability.py`
- Create: `docs/benchmarks/asr/stage5-v12-20260825/report.md`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

- [ ] **Step 1: 冻结每个决策方向的唯一 finalist、60m canonical cursor 与注入计划**
- [ ] **Step 2: 会议方向核对 Task 11 的 60m identity hash 并直接复用，不重复运行**
- [ ] **Step 3: 仅当交互方向存在 finalist 时，独立运行 1×60m；禁止与会议/其他模型交错驻留**
- [ ] **Step 4: 在冻结 cursor 注入 3 次断线、1 次 ASR 崩溃、1 次 finalization delay，报告内存斜率、FD/task/端口、队列、gap、尾段和恢复**
- [ ] **Step 5: 完成对应 60m 且全部硬门禁通过后才标 `Promote`；未长跑候选为 `deferred/not_run`，硬失败为 `Reject`**

```bash
git add tests/experiments/test_asr_stage5_reliability.py docs/benchmarks/asr/stage5-* docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "test(asr): 完成Stage5长时可靠性验收"
```

### Task 14: 固定唯一生产后端并清理落选方案

**Files:**
- Modify: `src/voice_realtime/config.py`
- Modify: `src/voice_realtime/asr/defaults.py`
- Modify: `src/voice_realtime/interaction/pipeline.py`
- Modify: `src/voice_realtime/subtitles/launcher.py`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/系统总体架构与详细设计方案.md`
- Modify: `docs/实时语音交互与字幕-方案与最佳实践.md`
- Modify: `docs/会议助手后端运行与前后端联调.md`
- Create: `docs/benchmarks/asr/final-decision/report.md`
- Create: `docs/benchmarks/asr/final-decision/model-inventory.csv`
- Modify/Delete: 仅最终落选后端专用的生产 adapter、service、启动项与测试。

- [ ] **Step 1: 生成最终决策报告与精确清理清单**

分别给出字幕/会议和交互助手结论，包含 revision、配置、hash、CI、失败率、负面结果、硬门禁和回退
方式。列出待删模型绝对 resolved path、repo ID、revision、大小和可重下载来源，删除前再次确认不被
胜出链路引用。

- [ ] **Step 2: 复用主测量并完成部署增量验收**

比较 Stage 3/4/5 与拟部署产物的 `git_commit + model_hash + profile_hash + runtime_config_hash`。完全
一致时直接复用，不重复 30 分钟会议、10–15 轮交互或 60 分钟长跑；只做离线启动/重启、一次 EOF/
恢复、一次外放下一轮输入和 3–5 轮部署 smoke。只有身份变化影响对应链路时才重跑该链路。30 轮
回归不是固定门禁，仅在生产代码/配置变化且有明确回归风险时执行一次。失败则恢复基线，不删除模型。

- [ ] **Step 3: TDD 固定唯一默认后端**

先更新测试断言唯一默认值和生产构造路径，再最小修改配置/launcher/pipeline；benchmark 共用契约和
聚合报告保留，不暴露用户切换入口。

- [ ] **Step 4: 删除落选模型与专用生产接入**

仅删除报告清单中、项目外且已验证未被引用的模型目录；说明是否可按固定来源/hash 恢复。删除专用
生产代码后运行引用搜索，确保没有悬空配置或脚本。

- [ ] **Step 5: 更新架构与运行文档**

所有持久化命令使用原生 `git`、`python3`、`uv`，不写入本机 wrapper、绝对缓存路径、凭据、实际
终端历史或敏感逐字稿。

- [ ] **Step 6: 提交生产收敛**

```bash
git add -A src tests README.md AGENTS.md docs pyproject.toml
git commit -m "feat(asr): 固定科学验证后的唯一生产后端"
```

### Task 15: 最终审查、全量门禁与仓库交付

**Files:**
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`
- Modify: `docs/superpowers/plans/2026-08-24-asr-scientific-comparison-completion.md`

- [ ] **Step 1: 五轴代码审查**

检查正确性、安全、性能、可维护性和测试质量；重点审计资源锁死锁/泄漏、隐式联网、MPS fallback、
blind 泄漏、模型/语料路径逃逸、音频落盘和 PostgreSQL 测试隔离。

- [ ] **Step 2: 执行全量后端门禁**

Run: `VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/`

Expected: 全部 PASS，分支覆盖率不低于 80%。

- [ ] **Step 3: 执行类型与 lint 门禁**

Run:

```bash
uv run mypy src/
uv run ruff check src/ tests/
```

Expected: 全部退出码为 0。

- [ ] **Step 4: 执行前端门禁**

Run:

```bash
cd ui && npm test -- --run
cd ui && npm run build
```

Expected: 全部退出码为 0。

- [ ] **Step 5: 复核运行态与资源竞争**

确认无残留 8100/8765/8001/10095 listener、无残留模型进程、实验锁可重新取得、项目内无模型 bytes、
工作树只含预期交付。重新计算最终报告引用的 commit、manifest 和模型 hash。

- [ ] **Step 6: 更新验收清单并提交**

把科学方案 §12 与本计划已完成步骤按实际证据勾选；无法验证的项保持未勾选并说明原因，不把计划
当完成证据。

```bash
git add docs/Fun-ASR与现有ASR后端科学对比测试方案.md docs/superpowers/plans/2026-08-24-asr-scientific-comparison-completion.md
git commit -m "docs(asr): 完成科学对比与生产收敛验收"
```
