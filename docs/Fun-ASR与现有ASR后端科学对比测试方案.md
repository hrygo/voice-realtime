# Fun-ASR 与现有 ASR 后端科学对比测试方案

## 1. 目的与决策对象

本方案不是一次演示性跑分，而是用于回答两个独立生产决策：

1. **字幕/会议决策**：Fun-ASR-Nano 是否应替换当前
   `Qwen3-ASR-1.7B + qwen3-streaming + Sortformer`。
2. **交互助手决策**：Fun-ASR-Nano 是否应替换当前
   `SenseVoiceSmall + Pipecat FunASRSTTService(CPU)`。

两个决策分别给结论，不把不同目标、协议和延迟预算合并成一个“总冠军”。质量优先于内存节省，
但实时性、数据完整性、离线边界与长期稳定性是硬门禁。

本方案依赖先完成
[`ASR 后端可插拔架构评估与前置设计`](superpowers/specs/2026-08-24-asr-backend-pluggability-design.md)
及其[实施计划](superpowers/plans/2026-08-24-asr-backend-pluggability.md)。在前置 runner 尚未实现时，
允许先做 Stage 0 可行性探测，但不得把不同脚本、不同切块或不同计时口径的结果用于最终选型。

## 2. 已知事实、假设与待检验项

### 2.1 当前实测与源码事实（2026-08-24）

- 主机：Apple M5 Max、128GB 统一内存、macOS 26.6.2。
- Python 3.12.14；PyTorch 2.13.0；MPS built/available 均为 true。
- 当前字幕/会议默认：Qwen3-ASR-1.7B、MPS、windowed、2.0s chunk、12.0s 左上下文、
  640ms 右上下文、hold-back 6、stable iterations 2、Sortformer 最多 4 人。
- 当前交互助手 STT：SenseVoiceSmall、CPU、ITN 开启、`ttfs_p99_latency=0.5`。
- 当前 WLK `backend="funasr"` 指向 SenseVoiceSmall，不代表 Fun-ASR-Nano。
- Fun-ASR 官方实时 WebSocket 与当前 WLK `/asr` 协议不同。
- 本机不满足 vLLM 的 CUDA/Ampere 前提，因此 vLLM 只记录为非本机参考，不进入本机排名。

### 2.2 候选事实

- Fun-ASR-Nano-2512 是约 800M 参数的中文/英文/日文及中文方言口音模型；31 语言能力属于独立的
  Fun-ASR-MLT-Nano-2512，不得混称。
- 官方提供 PyTorch/FunASR、实时 WebSocket、vLLM 与 llama.cpp/GGUF 路径；不同运行时必须作为
  不同实验臂。
- 官方 speaker diarization 由外部 CAM++ 组合提供，不是 Nano checkpoint 原生输出。
- 开源 checkpoint 的字符/词时间戳存在公开争议，因此时间戳能力必须本机独立验证；不能用 VAD
  分段边界冒充词级时间戳。

### 2.3 预注册假设

在首次查看完整测试集结果前冻结以下假设：

- H1：Fun-ASR-Nano 在中文目标域的 macro CER 显著低于当前 Qwen3 字幕基线。
- H2：Fun-ASR-Nano 对人名、缩写、数字和领域词的 recall 高于当前基线。
- H3：Fun-ASR-Nano 在 Apple Silicon 上可达到实时要求，且 P95 confirmed 延迟不劣于基线容限。
- H4：Fun-ASR-Nano 不增加静音幻觉、尾段截断、重连丢字或长会话资源漂移。
- H5：在固定 Sortformer 时，ASR 替换不会使 speaker-attributed CER 或 DER 超出非劣界值。

H1/H2 是质量优势假设；H3-H5 是非劣与安全假设。任何硬门禁失败均覆盖平均准确率优势。

## 3. 实验臂与可行性门禁

### 3.1 字幕/会议实验臂

| ID | 模型与运行时 | 设备 | 角色 | 排名资格 |
|---|---|---|---|---|
| `Q3-WLK-MPS` | Qwen3-ASR-1.7B / WLK qwen3-streaming | MPS | 当前主基线 | 必选 |
| `SV-WLK-CPU` | SenseVoiceSmall / WLK LocalAgreement | CPU | 轻量对照 | 必选 |
| `FA-PT-MPS` | Fun-ASR-Nano-2512 / PyTorch-FunASR | MPS | 主要候选 | 通过 Stage 0 后 |
| `FA-PT-CPU` | Fun-ASR-Nano-2512 / PyTorch-FunASR | CPU | 兼容对照 | MPS 不可用时仍单列 |
| `FA-WS-MPS` | Fun-ASR 官方实时 WS / PyTorch | MPS | 流式候选 | 通过协议和设备门禁后 |
| `FA-GGUF-Q5` | Fun-ASR-Nano GGUF Q5 / llama.cpp | CPU | 速度/体积候选 | 通过正确性门禁后 |
| `FA-GGUF-Q8` | Fun-ASR-Nano GGUF Q8 / llama.cpp | CPU | 质量量化候选 | 通过正确性门禁后 |

`FA-PT-MPS` 失败时不得静默落到 CPU；必须把该臂标记为 `infeasible`，另跑 `FA-PT-CPU`。
每个模型制品记录来源、revision、SHA-256、文件清单和运行时识别结果。下载时优先检查 ModelScope；
只有目标 GGUF/版本缺失或运行时明确要求时才回退官方 Hugging Face/GitHub release，并记录原因。

### 3.2 排除臂

- Fun-ASR 7.7B：未开放可复现实验权重，不参与。
- Fun-ASR vLLM：本机无 CUDA/Ampere，不参与 Apple Silicon 排名。
- CAM++ diarization：主实验固定 Sortformer，避免同时改变 ASR 与分人；CAM++ 可另立后续因子实验。
- 云端 API：违反全本地/离线目标，不参与。

### 3.3 Stage 0 可行性门禁

每个候选先用 10 个公开或自有短样本完成：

1. 本地离线加载，网络禁用时不尝试下载。
2. 16kHz mono s16le 输入正确，非 16kHz 输入由统一预处理器转换一次。
3. 普通话、英文、静音各能得到结构合法结果。
4. 流式臂能完成 ready → partial/confirmed → final，EOF 不死锁。
5. 设备、dtype 与实际执行后端一致；MPS fallback 必须从日志和 profiler 中排除。
6. 结果无 NaN、负时间戳、时间倒退或超出音频长度。
7. 进程退出后显存/统一内存和端口释放。

门禁失败的臂保留错误、环境和日志证据，状态记为 `infeasible`，不进入统计排名，也不以零分填充。

## 4. 语料设计

### 4.1 三层语料

| 层 | 用途 | 最小规模 | 说明 |
|---|---|---:|---|
| Public reproducibility | 与公开研究可对照 | ≥ 5 小时 | 合法取得的普通话/英文/会议公开测试集；固定版本和 checksum |
| Target-domain blind set | 生产选型主依据 | ≥ 11 小时 | 项目真实声学与词汇分布；测试前封存，候选调参不可见 |
| Reliability set | 长会与故障 | ≥ 6 小时 | 3×120 分钟或 6×60 分钟，含静音、重连、短尾段 |

公开集和目标域集分别报告，不用公开集均值掩盖本项目回退。涉及真实会议时需取得授权、脱敏并将
语料保存在项目目录外；项目只保存不可逆样本 ID、元数据和 SHA-256，不保存音频副本。

### 4.2 目标域分层配额

11 小时 blind set 按主要分析层分配；同一录音只归入一个主层，可附加多个标签用于探索分析。

| 主层 | 时长 | 最低说话人数 | 关键变量 |
|---|---:|---:|---|
| 近讲清晰普通话 | 60 min | 6 | 基础上限 |
| 远场会议普通话 | 90 min | 8 | 距离、混响 |
| 多人自然会议 | 120 min | 12 | 轮换、停顿、口语词 |
| 普英 code-switch | 60 min | 6 | 产品名、英文短语 |
| 方言与地区口音 | 120 min | 12 | 至少 4 个可合法获得的方言/口音组 |
| 噪声与混响 | 60 min | 6 | 键盘、风扇、街噪、音乐背景 |
| 重叠说话 | 45 min | 8 | 轻/中/重 overlap 标签 |
| 领域词与热词 | 45 min | 6 | 人名、缩写、术语；预先冻结词表 |
| 数字、日期、单位 | 30 min | 4 | ITN 原始与规范化双计分 |
| 静音/非语音负样本 | 30 min | 0 | 音乐、敲击、环境音、纯静音 |

### 4.3 标注协议

1. 两名标注员独立转写；差异由第三人裁决。标注员看不到模型输出。
2. 保存 `reference_raw` 与 `reference_normalized`；raw 保留大小写、标点和口语现象，normalized 使用
   版本化规则。
3. 中文按 Unicode 汉字/数字/拉丁 token 规则计算 CER；英文使用固定 tokenizer 计算 WER；
   code-switch 同时报 CER、WER 与混合 token error rate，禁止只挑最好看的指标。
4. 数字、日期、货币、百分比同时做 verbatim 和 ITN 评分。
5. speaker reference 以时间区间和匿名 speaker ID 标注；无法判断的重叠段显式标为 uncertain。
6. 每个领域词记录规范形式、允许变体、出现次数和是否提供给热词/context。
7. 先在 30 分钟双标子集上计算一致性；normalized CER 差异超过 1.0 个绝对百分点时，修订规范并
   重新标注受影响范围。

### 4.4 归一化规则

归一化实现必须版本化并对 reference/hypothesis 对称应用：Unicode NFKC、拉丁字母小写、全半角
统一、移除不承载语义的标点和空白。繁简转换、数字文本化、口语词删除均不进入主 normalized CER，
避免隐藏实际语义差异；它们只作为单独敏感性分析。英文缩写、否定词、人名和单位不得被停用词规则
删除。每次报告同时保留 raw、主 normalized 和 ITN 三个视图。

### 4.5 数据冻结与防泄漏

- `dev` 集用于参数选择，`blind` 集只在配置冻结后运行一次正式评估。
- 热词表仅来自部署时可获得的会议元数据或预声明词典，不从 blind reference 反向生成。
- 语料 manifest 固定 `corpus_version`、license/consent、sample SHA-256、duration、speaker、scenario、
  language、noise、overlap 和 reference revision。
- 任何人工查看 blind 输出后的配置修改都创建新 experiment family，不覆盖原结果。

## 5. 实验控制

### 5.1 共同输入

- 统一预处理为 16kHz、mono、signed 16-bit little-endian PCM；保留原始文件 hash。
- 模型核心实验使用相同的人工边界或同一 VAD 边界，隔离识别器质量。
- 系统实验使用各生产 pipeline，但 VAD/diarization 作为明确因子记录。
- 流式实验按固定 20ms PCM 帧、1× wall-clock 回放；禁止一次性快速发送后声称实时延迟。
- 每个后端接收完全相同的 chunk 序列、chunk 时间表和 hotword/context 信息。

### 5.2 运行环境控制

1. 接通电源，固定系统电源模式；关闭无关高负载任务。
2. 每个实验臂先完成不计分 warm-up；冷启动另作独立指标。
3. 采用按语料 block 的 Latin square 轮换后端顺序，减少温度、缓存和时间漂移。
4. 每个性能 block 运行 5 次；第 1 次作为冷启动，第 2-5 次作为 warm 重复。
5. 轮次间记录温度/频率可用指标；出现 thermal throttling 时整 block 作废重跑，不删改单个差结果。
6. greedy/deterministic 解码只需一次准确率输出，但性能仍重复；存在采样时固定 seed 并运行 3 个
   seed，结果按录音配对。
7. 记录并冻结线程数、device、dtype、量化、VAD、context、chunk/window、beam 和 decoder 参数。

### 5.3 因子隔离

按以下顺序执行，禁止直接用端到端结果推断模型本体优劣：

```text
Stage 1  模型核心：同音频 + 同分段 + 无分人
Stage 2  流式核心：同 PCM chunks + 同 1× schedule
Stage 3  系统链路：各 adapter + 固定 Sortformer + EOF/重连
Stage 4  交互链路：固定 VAD/LLM/TTS/回声防线，只换 STT factory
Stage 5  长时可靠性与故障注入
```

## 6. 指标定义

### 6.1 准确率主指标

令 substitution、deletion、insertion 分别为 `S`、`D`、`I`，reference token 数为 `N`：

```text
CER/WER = (S + D + I) / N
```

- **Primary endpoint A**：目标域 9 个有语音主层的 normalized CER macro-average，各层等权。
- **Primary endpoint B**：多人会议层的 speaker-attributed CER（SA-CER）。
- 同时报告全局 micro CER，防止 macro 指标隐藏大样本总错误，也防止 micro 指标被清晰普通话支配。
- 分别报告 S/D/I；空 reference 样本只进入 hallucination 指标，不进入 CER 分母。

### 6.2 关键词与语义敏感指标

```text
hotword precision = 正确命中数 / 全部预测命中数
hotword recall    = 正确命中数 / reference 出现数
hotword F1        = 2PR / (P + R)
```

另报人名准确率、缩写准确率、数字 exact match、ITN exact match、语言混淆率和严重语义错误率。
严重语义错误预先定义为否定词、数字、姓名、行动项主体或时间被改变。

### 6.3 流式指标

- `TTFP`：首个非空 partial 到达时间 − 首个语音帧发送时间。
- `TTFC`：首个 confirmed 到达时间 − 首个语音帧发送时间。
- `commit_latency(word)`：该词首次进入不再回滚的 confirmed 时间 − reference word end time。
- `finalization_latency`：EOF/STOP 发送到 final/ready 到达。
- `revision_burden`：相邻 partial 的 Levenshtein edit 总量 / final 字符数。
- `rollback_rate`：曾显示但未出现在 final 的字符数 / 曾显示字符数。
- `deadline_miss_rate`：处理时间超过对应音频推进预算的 chunk 比例。
- 所有延迟报告 median、P90、P95、P99、max 和 95% bootstrap CI。

如果某后端没有可靠 word timestamp，`commit_latency(word)` 标为 `unsupported`，同时仍报告可观测的
TTFP、TTFC 和 finalization latency；禁止用 segment/VAD 边界代替词结束时间。

### 6.4 分人与时间轴指标

- DER = `(missed speech + false alarm + speaker confusion) / reference speaker time`。
- JER：逐 speaker Jaccard error 的平均。
- SA-CER：将 speaker attribution 纳入 token 对齐后的 CER。
- speaker flip rate：confirmed 窗口修订中已出现 segment 的 speaker key 改变比例。
- 时间戳 MAE/P95：仅对通过可靠性门禁的原生或强制对齐时间戳计分。

主比较固定 Sortformer。若 ASR 文本/标点影响分人边界，DER/SA-CER 的变化作为系统效应保留，
但不得宣传为模型原生 diarization 能力。

### 6.5 性能与资源

- `RTF = ASR wall time / audio duration`；离线另报 `RTFx = 1 / RTF`。
- 冷/热模型加载时间、首段峰值、稳态吞吐。
- 进程 CPU%、GPU/MPS 可用率、peak RSS、统一内存、磁盘读取、能耗和温控状态。
- 120 分钟运行的内存斜率（MB/hour）和队列高水位。
- 丢帧、队列溢出、WebSocket 重连次数、gap 时长、异常与重启次数。

### 6.6 可靠性与数据完整性

- 静音幻觉：负样本每小时非空字符数、非空 segment 数和严重幻觉数。
- EOF 完整率：reference 尾部最后 1 秒内容被 final 保留的会话比例。
- confirmed monotonicity：同 epoch 已确认内容被删除或时间倒退的次数。
- reconnect coverage：注入断线前后，除明确 gap 外的音频是否全部有唯一归属。
- exactly-once persistence：重复 full snapshot/重连/EOF 后 PostgreSQL 无重复 segment。
- 隐私：项目运行目录和数据库中不存在音频 payload；journal 仅含允许的 confirmed 文本操作。

## 7. 统计分析计划

### 7.1 分析单位

- 准确率以“录音/会议”为配对和 bootstrap cluster，不能把每个字符当独立样本。
- 性能以“录音 × 重复轮次”为单位，冷启动和 warm run 分开。
- 方言/口音层至少逐组报告样本数和 CI；样本不足时标记探索性，不做总体推广。

### 7.2 置信区间与检验

1. 对候选与基线的配对 CER 差、SA-CER 差和延迟差做 10,000 次 cluster bootstrap，报告 95% CI。
2. 主假设按字幕/会议和交互两个 family 分开；family 内多候选比较使用 Holm 校正。
3. 同时报绝对差、相对变化和置信区间；不能只报 p-value。
4. 预先固定方向：准确率做 superiority；延迟、DER、幻觉和可靠性做 non-inferiority。
5. 缺失或失败运行不删除：报告失败率；只有可证明与模型无关的基础设施故障才允许整 block 重跑。
6. 盲测开封前生成 `analysis_plan.json`，冻结主指标、归一化版本、过滤规则、候选和阈值。

### 7.3 样本量与检验功效

在 blind set 开封前，用独立 pilot/dev 录音的“会话级配对 CER 差”估计方差。以相对 CER 改善 5%
作为最小相关效应，通过 10,000 次 cluster bootstrap 模拟估计 power；目标 power ≥ 0.80、双侧
family-wise alpha 0.05。若不足，只能在开封前按原分层比例扩充 blind 语料。开封后不得根据已见
效果追加样本以追求显著性。

### 7.4 最小实际意义

统计显著但改善过小不自动晋级。采用以下预注册效果阈值：

- Primary macro CER：相对改善至少 5%，且配对差的 95% CI 上界 `< 0`。
- 多人会议 SA-CER：不劣于基线 1.0 个绝对百分点；若希望替换默认后端，目标为相对改善至少 5%。
- 热词 recall：不低于基线 2 个百分点；若主张热词优势，需提高至少 5 个百分点。
- P95 commit latency：不高于基线 `1.10×`，且绝对不超过 3.0 秒。
- P99 finalization latency：不超过项目 8 秒 timeout 的 80%，即 6.4 秒。
- 静音幻觉：不高于基线，且严重幻觉为 0。
- DER：固定 Sortformer 时不劣于基线 1.0 个绝对百分点。

阈值可在 pilot 后基于测量分辨率调整一次，但必须在 blind set 开封前冻结并留下 revision 记录。

## 8. 执行矩阵

### 8.1 Stage 1：模型核心离线质量

对所有可行臂运行 public、dev、blind，使用相同人工/VAD segment。分别测试：

- 无 context/hotword：测基础模型质量。
- 统一可表达 context：测公平部署能力；不支持者标记 unsupported。
- 生产 context：测真实系统收益，但不得替代基础比较。

输出：raw/normalized hypothesis、CER/WER、S/D/I、实体和语义错误、每段耗时与资源。

### 8.2 Stage 2：流式质量与延迟

只对通过 Stage 1 且具备流式路径的臂运行。固定 20ms PCM 帧和 1× 回放，测试：

- 0.5/1/2 秒短句尾段。
- 连续 30 秒自然语音。
- 频繁停顿、快速轮换、code-switch。
- 15 秒 partial window 边界附近的长句。
- EOF 在最后一个音节后立即发送。

输出 TTFP、TTFC、commit/finalization latency、revision burden、rollback 和 deadline misses。

### 8.3 Stage 3：字幕/会议系统链路

固定 `AudioHub`、PCM fan-out、Sortformer、PostgreSQL、会议对账和 SRT 规则。运行：

1. 普通字幕浏览器订阅。
2. `assistant → meeting → idle → assistant`。
3. 会议 30/60/120 分钟。
4. EOF 正常、EOF 超时、WebSocket 断线、服务进程崩溃/重启。
5. 慢客户端、音频队列接近高水位、重连 epoch。
6. journal 临时降级与回放；只验证 confirmed 文本，不保存音频。

### 8.4 Stage 4：交互助手链路

只有具备 `ConversationSTTFactory` 的臂进入。固定 LLM、TTS、Silero VAD、`silence_secs=0.45`、
回声双防线和测试话术，测：

- 用户停说到 final transcript、LLM 首 token 和 TTS 首音频的分段延迟。
- 插话成功率、误打断率、机器人自响应率。
- 短指令、长问句、数字/人名、连续 30 轮会话。

任何删除/绕过双层回声防线的配置均不具备比较资格。

### 8.5 Stage 5：长时与故障

- 每个晋级臂至少 3 次 120 分钟运行。
- 在固定音频 cursor 注入 3 次网络断开、1 次 ASR 子进程崩溃、1 次 finalization delay。
- 检查内存斜率、文件描述符、后台 task、端口、队列、gap、重复持久化和恢复后字幕。

## 9. 结果数据契约

### 9.1 `manifest.json`

```json
{
  "schema_version": "1.0",
  "run_id": "20260824T120000Z-Q3-WLK-MPS-blind-r2",
  "git_commit": "<40-hex>",
  "corpus_manifest_sha256": "<64-hex>",
  "backend_id": "wlk-qwen3-streaming",
  "model_id": "Qwen/Qwen3-ASR-1.7B",
  "model_revision": "<immutable revision>",
  "model_files_sha256": {"config.json": "<64-hex>"},
  "runtime": {"name": "WhisperLiveKit", "revision": "<40-hex>"},
  "device": "mps",
  "dtype": "<measured value>",
  "parameters": {},
  "environment": {
    "host": "Apple M5 Max",
    "memory_bytes": 137438953472,
    "macos": "26.6.2",
    "python": "3.12.14",
    "torch": "2.13.0"
  },
  "started_at": "<UTC RFC3339>",
  "status": "completed"
}
```

尖括号是 schema 示例中的运行时值，不是允许省略的字段；runner 必须在写入时填入真实值。

### 9.2 `hypotheses.jsonl`

每行至少包含：`sample_id`、`scenario`、`reference_raw`、`reference_normalized`、
`hypothesis_raw`、`hypothesis_normalized`、`language`、`duration_ms`、`S/D/I/N`、`error_status`。

### 9.3 `events.jsonl`

每行至少包含：`sample_id`、`audio_cursor_ms`、`arrival_monotonic_ms`、`event_kind`、`text`、
`is_final`、`source_epoch`、`segments`、`backend_id`。原始 vendor payload 写入单独受限文件，展示前
做长度限制和字段脱敏。

### 9.4 汇总产物

```text
runtime/benchmarks/asr/<run_id>/
├─ manifest.json
├─ hypotheses.jsonl
├─ events.jsonl
├─ resources.csv
├─ failures.jsonl
└─ summary.json

docs/benchmarks/asr/<experiment-family>/
├─ analysis-plan.json
├─ summary.csv
├─ report.md
└─ plots/
```

`runtime/` 产物不入库；入库报告只包含聚合数据、失败样本匿名 ID 和可复现元数据，不含音频或敏感
逐字稿。

## 10. 判定规则

### 10.1 硬门禁

任一项失败即不能成为默认后端：

1. 默认离线加载失败或发生未授权联网。
2. 120 分钟内崩溃、死锁、无界内存增长或不可恢复资源泄漏。
3. EOF 尾段截断、finalization 超时率高于基线，或正常样本存在 confirmed 回退。
4. 静音严重幻觉不为 0。
5. 重连后出现未声明 gap 的丢音、重复 segment 或错误时间轴。
6. 会议音频被写入磁盘/数据库，或 journal 权限和内容边界回退。
7. 交互测试中出现机器人自响应，或为候选删除回声双防线。
8. 模型、运行时、设备或量化身份不能通过 manifest 追溯。

### 10.2 晋级分类

| 结果 | 条件 | 动作 |
|---|---|---|
| Promote | 全部硬门禁通过；主 CER superiority；其他主指标满足非劣界值 | 先设为 opt-in，完成真实试运行后再改默认 |
| Specialized | 特定层显著更好，但总体或延迟未达默认门槛 | 保留为方言/离线批处理等显式 profile |
| Experimental | CI 跨越阈值或样本量不足，但无安全失败 | 扩充语料，不做默认切换 |
| Reject | 任一硬门禁失败，或质量/延迟明确越过劣界 | 保持现基线并归档失败证据 |
| Infeasible | 本机运行时/设备无法正确执行 | 不排名，不宣称质量优劣 |

字幕/会议与交互助手分别分类。Fun-ASR 可以在会议中 Promote，而在交互中因延迟 Reject；这是有效结论。

## 11. 执行顺序与停止规则

1. 完成前置架构 Phase 0-2，并让 Qwen3 对自身重复运行的输出一致。
2. 冻结语料、归一化、analysis plan、模型 revision 和配置。
3. 跑 Stage 0；淘汰不可行运行时。
4. 在 dev 上做有限参数选择，每个实验臂使用同等调参预算。
5. 冻结参数并运行 Stage 1 blind。
6. 只有准确率未明确劣于基线的臂进入 Stage 2。
7. 只有实时门禁通过的臂进入 Stage 3/4。
8. 只有系统链路无硬失败的臂进入 Stage 5。
9. 生成带 CI、失败率、分层结果和 manifest 链接的决策报告。

提前停止只允许：硬件/运行时不可行、隐私违规、连续三次确定性崩溃、或在至少 30% blind 样本上
质量已明显越过预注册劣界且置信区间不再可能恢复。提前停止原因必须写入 manifest。

## 12. 验收清单

- [ ] 每个实验臂的模型、运行时和设备身份可验证。
- [ ] 同一 block 的音频 bytes、切块和发送时间表一致。
- [ ] blind set 在配置冻结后才开封。
- [ ] 原始与 normalized 指标并列，S/D/I 可追溯。
- [ ] macro、micro、分层、失败率和 95% CI 均报告。
- [ ] unsupported 与 infeasible 不被填成 0。
- [ ] Sortformer 在主比较中固定。
- [ ] EOF、重连、长会、静音和隐私硬门禁均执行。
- [ ] 字幕/会议与交互助手分别做决策。
- [ ] 报告包含负面结果和失败样本类别，不只展示平均值。

## 13. 外部依据

- [QwenAudio/Fun-ASR 固定 commit](https://github.com/QwenAudio/Fun-ASR/tree/53a56d80667320b44a7dd779f5bf8c024b6c30a8)
- [Fun-ASR 官方实时 WebSocket 服务](https://github.com/QwenAudio/Fun-ASR/blob/53a56d80667320b44a7dd779f5bf8c024b6c30a8/serve_realtime_ws.py)
- [Fun-ASR vLLM 指南](https://github.com/QwenAudio/Fun-ASR/blob/53a56d80667320b44a7dd779f5bf8c024b6c30a8/docs/vllm_guide.md)
- [Fun-ASR-Nano-2512 模型卡](https://huggingface.co/FunAudioLLM/Fun-ASR-Nano-2512)
- [FunAudio-ASR Technical Report](https://arxiv.org/abs/2509.12508)
- [开源 checkpoint 时间戳问题 #106](https://github.com/QwenAudio/Fun-ASR/issues/106)

官方报告与模型卡用于形成候选假设，不作为本机选型结论；最终结论只来自上述冻结语料、统一 runner
和本机重复实验。
