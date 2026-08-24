# ASR Scientific Comparison Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 Qwen3-ASR、SenseVoiceSmall 与 Fun-ASR-Nano 的可复现本机科学对比，分别选出字幕/会议和交互助手的唯一生产后端，并在生产验收后删除落选模型与专用接入。

**Architecture:** 科学 runner 只依赖统一 `StreamingTranscriber` 契约；三种原生离线模型各由独立 adapter 吸收 vendor 差异。所有模型加载、服务启动、基准运行和全量测试都先取得项目外的主机级排他锁并由主 Agent 串行调度；只有 Stage 1 质量门禁通过的候选才建设流式 runtime，最终生产配置固定一个后端，不建设运行时切换。

**Tech Stack:** Python 3.12、asyncio、PyTorch/MPS、FunASR、WhisperLiveKit/Qwen3-ASR、Pydantic v2、pytest、cluster bootstrap、PostgreSQL（仅系统链路 confirmed 文本）。

**Spec:** `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

## Global Constraints

- Python 严格保持 `>=3.12,<3.13`，默认离线且禁止隐式下载。
- 模型、语料、逐字稿和逐样本实验产物只放项目外；Git 只保存契约、聚合指标和脱敏报告。
- 主机排他锁默认位于用户 cache 根目录的 `voice-realtime/locks/asr-experiment.lock`；锁目录 `0700`、锁文件 `0600`。
- 模型加载、ASR/TTS/LLM 服务、基准实验、故障注入和全量质量门禁不得并行；服务启动前再次检查端口与锁持有者。
- worker 只允许并行进行只读调查或修改彼此不重叠的文件；测试、模型加载、提交与删除由主 Agent 串行执行。
- `PYTORCH_ENABLE_MPS_FALLBACK=0`；MPS 实验必须验证真实参数 device，失败单列 `infeasible`，不得静默转 CPU。
- blind set 只在 `analysis-plan.json`、模型 revision、profile 和阈值冻结后开封一次；人工查看后调参必须创建新实验 family。
- 会议不保存音频；保留 `EchoSuppressionProcessor` 与 `SelfEchoFilter` 双层回声防线。
- 删除模型或生产接入只在最终分类、真实试运行和回退证据完成后执行，并精确记录删除清单。

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

- [ ] **Step 1: 写竞争与权限 RED 测试**

覆盖首次持锁、同进程第二个非阻塞竞争者收到稳定 `RESOURCE_BUSY`、超时、异常退出自动释放、锁目录
`0700`、锁文件 `0600`，以及 `score`/`compare` 不取锁。测试使用 `tmp_path` 和独立子进程，不触碰默认
主机锁。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/benchmarks/test_resource_lock.py tests/benchmarks/test_asr_cli.py -q --no-cov`

Expected: `voice_realtime.benchmarks.resource_lock` 不存在而失败。

- [ ] **Step 3: 实现最小排他锁**

使用 macOS/POSIX `fcntl.flock(LOCK_EX | LOCK_NB)`；采用单调时钟和短轮询实现有界等待。锁内容仅写 PID、
UTC started_at、command 与 run_id，不写环境变量。CLI 增加 `--resource-lock` 和
`--lock-timeout-secs`，默认路径通过 `Path.home()` 解析到项目外 cache。

- [ ] **Step 4: 运行 GREEN 与回归**

Run: `uv run pytest tests/benchmarks/test_resource_lock.py tests/benchmarks/test_asr_cli.py tests/benchmarks/test_asr_replay.py -q --no-cov`

Expected: PASS，且现有 runner 产物契约不变。

- [ ] **Step 5: 提交**

```bash
git add src/voice_realtime/benchmarks tests/benchmarks
git commit -m "feat(asr): 增加实验主机资源排他锁"
```

### Task 2: 增加 Qwen3-ASR 原生离线实验臂

**Files:**
- Create: `src/voice_realtime/asr/adapters/qwen3_native.py`
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
- Consumes: 项目外固定 Qwen3-ASR-1.7B snapshot、内存 16kHz mono float32/PCM、冻结 language/context。

- [ ] **Step 1: 写 vendor 行为与 adapter RED 测试**

以 fake processor/model 覆盖：模型一次加载、多样本复用；PCM 合并只发生在 adapter 内存；中文、英文
与 auto 语言；context 长度边界；空白输出；vendor 异常映射；最终窗口边界；MPS 参数 device 检查；
非 `offline` mode 拒绝。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/asr/test_qwen3_native_adapter.py tests/asr/test_profiles.py tests/benchmarks/test_asr_cli.py -q --no-cov`

Expected: 新 profile 和 adapter import 失败。

- [ ] **Step 3: 实现最小原生离线边界**

按当前本机 Qwen3-ASR/WhisperLiveKit 源码的真实签名加载本地 snapshot，显式 `local_files_only` 或等价
离线边界；输入统一为 16kHz；只接受结构合法的文本结果。profile 冻结 device、dtype、language_source、
context 和 decoder 参数，manifest 逐字段核对。

- [ ] **Step 4: 运行 GREEN 与类型检查**

Run:

```bash
uv run pytest tests/asr/test_qwen3_native_adapter.py tests/asr/test_profiles.py tests/asr/test_defaults.py tests/benchmarks/test_asr_cli.py -q --no-cov
uv run mypy src/voice_realtime/asr/adapters/qwen3_native.py src/voice_realtime/benchmarks/asr/cli.py
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/voice_realtime/asr tests/asr src/voice_realtime/benchmarks/asr/cli.py tests/benchmarks/test_asr_cli.py
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

- [ ] **Step 1: 写 vendor 行为与 adapter RED 测试**

覆盖 `AutoModel.generate()` 的真实参数形状、模型一次加载、逐样本 language、ITN、空输出、tag 清理、
异常映射、CPU-only profile、外部绝对模型路径与非离线 mode 拒绝。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/asr/test_sensevoice_native_adapter.py tests/asr/test_profiles.py tests/benchmarks/test_asr_cli.py -q --no-cov`

Expected: 新 profile 和 adapter import 失败。

- [ ] **Step 3: 实现最小原生离线边界**

复用当前 `resolve_stt_model()` 的离线快照规则，以 FunASR 本机源码的真实输入类型调用 SenseVoice；
禁止 hub 更新和下载。输出在 adapter 边界清理 SenseVoice emotion/event/language tags，但原始 vendor
结果仍进入受限事件文件。

- [ ] **Step 4: 运行 GREEN 与生产等价回归**

Run:

```bash
uv run pytest tests/asr/test_sensevoice_native_adapter.py tests/asr/test_pipecat_sensevoice.py tests/test_pipeline.py tests/benchmarks/test_asr_cli.py -q --no-cov
uv run mypy src/voice_realtime/asr/adapters/sensevoice_native.py
```

Expected: PASS，生产 `PipecatSenseVoiceFactory` 默认参数不变。

- [ ] **Step 5: 提交**

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

- [ ] **Step 1: 写判别调度 RED 测试**

覆盖五种现有 profile 与两种新 profile、未知 profile、device/dtype/parameters 不一致、逐样本语言复制、
模型目录逃逸和原生 profile 错用 realtime。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/benchmarks/test_asr_backend_factory.py tests/benchmarks/test_asr_cli.py -q --no-cov`

Expected: factory 模块不存在而失败。

- [ ] **Step 3: 从 CLI 抽离构建职责**

CLI 只负责解析、验证和持锁；factory 拥有 profile → engine/registry 映射与 manifest 身份核对，避免后续
增加实验臂时继续扩大条件分支。

- [ ] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/benchmarks tests/asr -q --no-cov`

Expected: PASS。

- [ ] **Step 5: 提交**

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
- Produces outside repository: 16kHz mono s16le PCM、`corpus.json`、`analysis-plan.json`、checksums。

- [ ] **Step 1: 写隐私、格式与冻结 RED 测试**

覆盖 WAV/FLAC 统一转码身份、时长/hash 校验、相对路径、symlink 逃逸、重复 sample ID、许可/同意缺失、
blind reference 不可读模式、分层配额摘要、analysis plan hash、已冻结文件拒绝覆盖。

- [ ] **Step 2: 运行 RED**

Run: `uv run pytest tests/benchmarks/test_asr_corpus.py tests/benchmarks/test_asr_analysis_plan.py tests/benchmarks/test_asr_manifest.py -q --no-cov`

Expected: corpus/analysis_plan 模块不存在而失败。

- [ ] **Step 3: 实现确定性制备与冻结**

只调用本机可验证的 `ffmpeg` 进行一次转码，记录原始与 PCM SHA-256；reference/hypothesis 共用版本化
归一化函数。blind manifest 分离可运行元数据与加密/权限受限 reference，runner 在正式开封命令前
不得读取 reference。

- [ ] **Step 4: 制备公开集、dev 与 blind 目录**

在 `~/.cache/voice-realtime/benchmarks/asr/corpora/` 下创建版本化目录；公开数据记录数据集官方版本、
许可、来源与 checksum。目标域数据仅纳入已授权录音；不足配额时在冻结报告中明确缺口，禁止用合成
音频冒充真实 blind set。

- [ ] **Step 5: 运行 GREEN 并提交工具与文档**

Run: `uv run pytest tests/benchmarks/test_asr_corpus.py tests/benchmarks/test_asr_analysis_plan.py tests/benchmarks/test_asr_manifest.py -q --no-cov`

```bash
git add src/voice_realtime/benchmarks/asr tests/benchmarks docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "feat(asr): 增加语料制备与盲测冻结工具"
```

### Task 6: 串行完成三后端 Stage 0 对等门禁

**Files:**
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`
- Create: `docs/benchmarks/asr/stage0-20260824/report.md`
- Create: `docs/benchmarks/asr/stage0-20260824/summary.csv`

- [ ] **Step 1: 确认独占环境**

确认 8100/8765/8001/10095 无监听，主机实验锁可独占取得，网络下载关闭，模型位于项目外且 hash 与
manifest 一致。不得同时运行任何其他模型或全量测试。

- [ ] **Step 2: 冻结 Qwen3 与 SenseVoice Stage 0 manifests**

使用与现有 Fun-ASR Stage 0 相同 10 条门禁语料、PCM bytes、分段、逐样本 language 与 chunk 配置；
分别固定 Qwen3 MPS、SenseVoice CPU 的模型文件全量 hash 和 profile 参数。

- [ ] **Step 3: 依次运行，不并行**

顺序执行 Qwen3 MPS → SenseVoice CPU → Fun-ASR MPS → Fun-ASR CPU；每臂进程退出且锁释放后才开始
下一臂。每臂检查普通话、英文、静音、结构、真实 device、NaN/时间边界、RSS/FD 回收。

- [ ] **Step 4: 汇总可行性而不宣称质量**

报告加载时间、warm RTF、峰值 RSS、失败和输出一致性；模型自带/合成样例不进入正式准确率结论。

- [ ] **Step 5: 验证并提交聚合报告**

Run: `uv run pytest tests/benchmarks tests/asr -q --no-cov`

```bash
git add docs/Fun-ASR与现有ASR后端科学对比测试方案.md docs/benchmarks/asr/stage0-20260824
git commit -m "docs(asr): 回填三后端Stage0门禁结果"
```

### Task 7: 冻结并执行 Stage 1 dev 参数选择

**Files:**
- Create: `docs/benchmarks/asr/stage1-<family>/analysis-plan.json`
- Create: `docs/benchmarks/asr/stage1-<family>/dev-report.md`
- Create: `docs/benchmarks/asr/stage1-<family>/dev-summary.csv`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

- [ ] **Step 1: 验证 dev 语料独立性与功效输入**

确认 dev 未进入 blind，分层、说话人、许可、参考标注与 hash 完整；以会议/录音为 cluster，禁止以字符
为独立样本。

- [ ] **Step 2: 冻结等调参预算**

三后端分别限定相同数量的预声明配置；基础无 context 为主实验，统一 context 与生产 context 分开。
固定归一化版本、设备、dtype、线程、decoder、seed 和重复策略。

- [ ] **Step 3: 采用 Latin square 串行运行**

每个 block 顺序轮换，但任何时刻只加载一个模型；每臂一次 cold + 四次 warm。检测 thermal throttling
时整 block 作废并保留原因，不能删改单个差结果。

- [ ] **Step 4: 生成统计与功效报告**

报告 macro/micro CER、S/D/I、WER/MER、实体/数字/严重语义错误、失败率、资源和 10,000 次配对
cluster bootstrap。用 dev 会话级差异估计 blind power，必要扩容只能发生在 blind 开封前。

- [ ] **Step 5: 冻结 blind `analysis-plan.json` 并提交聚合结果**

文件写入主 endpoints、Holm family、superiority/non-inferiority 阈值、停止规则、候选 profile SHA-256
与 blind manifest SHA-256。

```bash
git add docs/benchmarks/asr docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "docs(asr): 冻结Stage1盲测分析计划"
```

### Task 8: 一次性执行 Stage 1 blind 并做晋级决策

**Files:**
- Create: `src/voice_realtime/benchmarks/asr/report.py`
- Create: `tests/benchmarks/test_asr_report.py`
- Create: `docs/benchmarks/asr/stage1-<family>/blind-report.md`
- Create: `docs/benchmarks/asr/stage1-<family>/blind-summary.csv`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

- [ ] **Step 1: 写报告门禁 RED 测试**

覆盖配对样本缺失、失败率、macro/micro/分层、CI、Holm 校正、unsupported/infeasible、硬门禁和
Promote/Specialized/Experimental/Reject/Infeasible 分类。

- [ ] **Step 2: 实现并验证确定性报告器**

Run: `uv run pytest tests/benchmarks/test_asr_report.py tests/benchmarks/test_asr_metrics.py -q --no-cov`

Expected: PASS。

- [ ] **Step 3: 核验冻结身份后开封一次**

核对 code commit、模型/profile/corpus/analysis hashes 与排他锁，再按 Latin square 串行运行。失败样本
必须保留，只有可证明与模型无关的基础设施故障才能整 block 重跑。

- [ ] **Step 4: 分别做字幕/会议与交互结论**

按预注册阈值报告相对改善、绝对差与 95% CI；不以综合分数混合两个用途。未通过质量门禁的候选停止，
不建设其流式 runtime。

- [ ] **Step 5: 提交代码与聚合报告**

```bash
git add src/voice_realtime/benchmarks/asr/report.py tests/benchmarks/test_asr_report.py docs/benchmarks/asr docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "feat(asr): 生成Stage1盲测决策报告"
```

### Task 9: 仅为 Stage 1 晋级的候选建设本机流式 runtime

**Conditional:** 若 Fun-ASR 未晋级，则本任务标记 `not-applicable`，不得为其增加生产服务。

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

只使用 Stage 1 胜出配置；模型位于项目外；不保存 PCM；所有队列有界；服务生命周期持有与 benchmark
相同的主机锁，避免任何第二模型或实验并发。

- [ ] **Step 4: 运行 GREEN 与资源回收测试**

Run: `uv run pytest tests/asr/test_funasr_nano_streaming_service.py tests/asr/test_funasr_nano_ws_adapter.py -q --no-cov`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/voice_realtime/asr/services tests/asr/test_funasr_nano_streaming_service.py pyproject.toml docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "feat(asr): 增加Fun-ASR本机流式候选服务"
```

### Task 10: 串行执行 Stage 2 流式质量与延迟

**Files:**
- Create: `docs/benchmarks/asr/stage2-<family>/report.md`
- Create: `docs/benchmarks/asr/stage2-<family>/summary.csv`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

- [ ] **Step 1: 冻结相同 20ms PCM 与 1× schedule**
- [ ] **Step 2: 依次运行基线与晋级候选，服务切换之间确认端口和锁释放**
- [ ] **Step 3: 报告 TTFP、TTFC、finalization、revision、rollback、deadline miss 与失败率**
- [ ] **Step 4: 对有可靠词时间戳的臂报告 commit latency；其余显式 `unsupported`**
- [ ] **Step 5: 按延迟非劣与 6.4 秒 finalization 硬门禁决定是否进入 Stage 3/4**

```bash
git add docs/benchmarks/asr/stage2-* docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "docs(asr): 回填Stage2流式对比结果"
```

### Task 11: 串行执行 Stage 3 字幕/会议系统链路

**Files:**
- Create: `tests/experiments/test_asr_stage3_system.py`
- Create: `docs/benchmarks/asr/stage3-<family>/report.md`
- Modify: `docs/会议助手后端运行与前后端联调.md`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

- [ ] **Step 1: 写可重复的系统场景测试**

覆盖字幕订阅、`assistant → meeting → idle → assistant`、30/60/120 分钟、EOF 正常/超时、断线、
崩溃、慢客户端、epoch 重连、journal 回放和 exactly-once persistence。

- [ ] **Step 2: 为每个实验臂重建独立临时 PostgreSQL schema**

严格检查测试 DSN，结束后执行 `DROP SCHEMA ... CASCADE`；不得读取或写入生产会议数据。

- [ ] **Step 3: 固定同一 Sortformer 并依次运行**

Sortformer 与 ASR 服务不得并行于另一实验臂；记录 DER/JER/SA-CER、speaker flip、EOF、gap、重复
segment 和恢复结果。

- [ ] **Step 4: 验证隐私和恢复硬门禁**

确认项目运行目录、数据库和 journal 无音频 payload，journal 权限/内容符合边界。

- [ ] **Step 5: 提交测试与聚合报告**

```bash
git add tests/experiments/test_asr_stage3_system.py docs/benchmarks/asr/stage3-* docs/会议助手后端运行与前后端联调.md docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "test(asr): 完成Stage3字幕会议系统验收"
```

### Task 12: 串行执行 Stage 4 交互助手链路

**Files:**
- Create: `tests/experiments/test_asr_stage4_interaction.py`
- Create: `docs/benchmarks/asr/stage4-<family>/report.md`
- Modify: `docs/实时语音交互与字幕-方案与最佳实践.md`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

- [ ] **Step 1: 写固定链路实验测试**

固定 VAD、LLM、TTS、`silence_secs=0.45`、相同话术与双层回声防线，只替换 STT factory。覆盖短指令、
长问句、数字/人名、插话和连续 30 轮。

- [ ] **Step 2: 依次启动所需单一服务并检查锁/端口**

任何时刻只运行当前 STT 与固定 LLM/TTS；每臂结束后停止并验证端口释放，再开始下一臂。

- [ ] **Step 3: 测量分段延迟与交互安全**

报告停说→final、LLM 首 token、TTS 首音频、插话成功、误打断和机器人自响应。机器人自响应或绕过
回声防线即 Reject。

- [ ] **Step 4: 分析语音播报后下一轮输入恢复**

回归验证 TTS 状态结束、echo tail hangover 与外放模式输入重新开启时序，确保此前“不稳定空等待”
修复在每个候选后端均无回退。

- [ ] **Step 5: 提交测试与聚合报告**

```bash
git add tests/experiments/test_asr_stage4_interaction.py docs/benchmarks/asr/stage4-* docs/实时语音交互与字幕-方案与最佳实践.md docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "test(asr): 完成Stage4交互链路验收"
```

### Task 13: 串行执行 Stage 5 长时与故障注入

**Files:**
- Create: `tests/experiments/test_asr_stage5_reliability.py`
- Create: `docs/benchmarks/asr/stage5-<family>/report.md`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

- [ ] **Step 1: 冻结可靠性 cursor 与注入计划**
- [ ] **Step 2: 每个晋级臂依次完成 3×120 分钟，禁止并行或交错模型驻留**
- [ ] **Step 3: 每轮注入 3 次断线、1 次 ASR 崩溃、1 次 finalization delay**
- [ ] **Step 4: 报告内存斜率、FD/task/端口、队列、gap、重复持久化、尾段和恢复**
- [ ] **Step 5: 任何硬门禁失败即记录 Reject，保留失败证据并停止该臂后续试运行**

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

- [ ] **Step 2: 先完成受控真实试运行**

固定一个生产候选，串行验证字幕、会议 EOF/恢复、交互 30 轮、外放下一轮输入、重启和离线启动。
失败则恢复当前基线，不执行模型删除。

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
