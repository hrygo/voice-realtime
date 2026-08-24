# Fun-ASR 与现有 ASR 后端科学对比测试方案

## 1. 目的与决策对象

本方案不是一次演示性跑分，而是用于回答两个独立生产决策：

1. **字幕/会议决策**：Fun-ASR-Nano 是否应替换当前
   `Qwen3-ASR-1.7B + qwen3-streaming + Sortformer`。
2. **交互助手决策**：Fun-ASR-Nano 是否应替换当前
   `SenseVoiceSmall + Pipecat FunASRSTTService(CPU)`。

两个决策分别给结论，不把不同目标、协议和延迟预算合并成一个“总冠军”。质量优先于内存节省，
但实时性、数据完整性、离线边界与长期稳定性是硬门禁。

本方案复用
[`ASR 后端可插拔架构评估与前置设计`](superpowers/specs/2026-08-24-asr-backend-pluggability-design.md)
及其[实施计划](superpowers/plans/2026-08-24-asr-backend-pluggability.md)已经完成的统一契约、adapter、
registry 与 benchmark runner。生产环境冷切换不再是科学对比的前置条件：模型候选必须先在独立
runner 中证明可行且值得晋级。最终只部署胜出的单一后端，不建设生产运行时切换事务。

### 1.1 当前落地状态（2026-08-24）

- ASR 契约、WLK 适配器、profile/registry、字幕注入边界和交互 STT factory 已合入 `main`。
- 可复现实验 runner 已在 `feature/asr-benchmark-runner` 实现：提供 `run`、`score`、`compare`，
  固定 16kHz mono s16le、20ms chunk、原始 vendor 事件分离记录、逐样本失败保留、1 秒资源采样、
  分层等权 macro CER 和 10,000 次配对 cluster bootstrap。
- runner 会核验干净 git checkout、代码 commit、语料 manifest SHA-256、模型文件 SHA-256、音频
  SHA-256/长度、相对路径与归一化版本；输出目录为 `0700`，逐字稿和事件文件为 `0600`，不复制
  音频。
- `FunASRNanoWSAdapter`、`funasr-nano-ws` 判别 profile、用途能力门禁和 benchmark runner 接线已在
  当前分支实现；mock 协议测试覆盖握手、partial/final、STOP 幂等、错误、断线与非法时间戳。
- `FunASRNanoPyTorchAdapter`、`funasr-nano-pytorch` profile 与 benchmark CLI 接线已在当前分支
  实现。engine 在一次 run 中只加载一次模型，样本 adapter 只缓冲内存 PCM；原生离线 profile
  强制 `--mode offline`，禁止误报为流式实验。
- Fun-ASR-Nano-2512 已迁移到项目外的 ModelScope cache。`modelscope scan-cache` 能识别该
  `FunAudioLLM/Fun-ASR-Nano-2512@master` 快照（21 个文件，约 2.0 GiB）；20 个非隐藏远端文件已
  通过 `modelscope cache verify`。校验器仍报告 `.gitattributes` 缺失，但当前文件实测存在，因此
  这是工具对隐藏文件的覆盖差异，不能据此声称整个快照未完成。
- 当前 Qwen3-ASR 1.7B 已迁移到 ModelScope cache，Sortformer 已迁移到 Hugging Face cache 的固定
  revision；项目 `runtime/` 不再包含模型文件或兼容 symlink。上游完整性核验失败的非默认
  Qwen3-ASR 0.6B ModelScope 旧快照已删除，不作为实验臂或回退来源。
- 尚未启动固定官方 WebSocket 服务、开封 blind set 或产生任何选型结论。checkpoint 已完成 MPS/CPU
  初步加载与推理探测，但这不等于完整 Stage 0、准确率比较或实时链路已通过。

runner 的 `--mode offline` 只表示“不等待 wall-clock 的 PCM 回放时序”：对 WS profile，它仍只是
流式 adapter 的快速回放；对 `funasr-nano-pytorch`，PCM 在内存中合并后才调用一次模型原生离线
推理，可用于 Stage 1 模型核心实验。两类结果必须使用不同 backend ID，不得混为同一实验臂。

### 1.2 本机执行顺序修订（2026-08-24）

原实施计划先建设生产冷切换，再做候选验证；当前决策和本机环境表明这既无必要，也会引入无效
前置工作：

- `UIRuntime` 只连接外部 ASR WebSocket，不拥有 WhisperLiveKit/Fun-ASR 服务进程，无法真实执行
  “停止旧模型进程 → 启动候选 → 失败回滚”。
- 固定官方 Fun-ASR WebSocket 服务使用 vLLM/CUDA，本机 Apple Silicon 不具备运行条件。
- 科学 runner 已能独立冻结输入、记录事件和比较指标，无需经过生产控制 WebSocket。

因此按本机条件采用以下顺序：

```text
本地 checkpoint 完整性
  → PyTorch MPS 加载探测（禁止 fallback）
  → 独立 PyTorch CPU 加载探测
  → 原生离线 adapter 与 Stage 1 dev 比较
  → 通过质量门禁后才建设本机流式服务与 Stage 2
  → 胜出后固定为生产唯一后端
```

测试期间保留多个 adapter/实验 profile 是为了公平复现，不代表生产系统需要动态切换。官方 vLLM
WS 保留为协议参考并标记本机 `infeasible`，不再阻塞 PyTorch 实验臂。

### 1.3 选型后的收敛与清理原则

- 生产配置只保留一个默认 ASR 后端，不暴露热切换、冷切换或用户选择入口。
- 胜出方案完成真实试运行和验收后，删除落选模型权重、专用服务启动项及只为其生产接入存在的代码；
  共用 benchmark 契约、聚合报告和不含敏感逐字稿的失败证据继续保留，用于复核结论。
- 删除前生成最终决策报告，记录模型 revision、配置、指标、失败原因和制品 SHA-256。删除模型 bytes
  不影响复现实验身份；将来如需复核，按固定来源和 hash 重新取得。
- 清理动作只在最终结论明确且生产验收完成后执行，不在 Stage 0/1 探测期间提前删除仍需比较的基线。

## 2. 已知事实、假设与待检验项

### 2.1 当前实测与源码事实（2026-08-24）

- 主机：Apple M5 Max、128GB 统一内存、macOS 26.6.2。
- Python 3.12.14；PyTorch 2.13.0；MPS built/available 均为 true。
- 当前字幕/会议默认：Qwen3-ASR-1.7B、MPS、windowed、2.0s chunk、12.0s 左上下文、
  640ms 右上下文、hold-back 6、stable iterations 2、Sortformer 最多 4 人。
- 当前交互助手 STT：SenseVoiceSmall、CPU、ITN 开启、`ttfs_p99_latency=0.5`。
- 当前 WLK `backend="funasr"` 指向 SenseVoiceSmall，不代表 Fun-ASR-Nano。
- Fun-ASR 官方实时 WebSocket 与当前 WLK `/asr` 协议不同。
- 固定 commit 的官方 `serve_realtime_ws.py` 使用 vLLM；本机没有 vLLM，且 Apple Silicon 不满足其
  CUDA/Ampere 前提。因此官方 WS 仅完成客户端协议兼容，不进入本机排名。若后续实现 PyTorch
  本机 WS 服务，必须作为不同 runtime 实验臂登记，不能沿用官方 vLLM 身份。

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
| `FA-WS-vLLM-CUDA` | Fun-ASR 固定官方实时 WS / vLLM | CUDA | 协议参考 | 本机排除 |
| `FA-WS-PT-MPS` | 待实现的 PyTorch 本机 WS 服务 | MPS | 流式候选 | 独立登记并通过 Stage 0 后 |
| `FA-GGUF-Q5` | Fun-ASR-Nano GGUF Q5 / llama.cpp | CPU | 速度/体积候选 | 通过正确性门禁后 |
| `FA-GGUF-Q8` | Fun-ASR-Nano GGUF Q8 / llama.cpp | CPU | 质量量化候选 | 通过正确性门禁后 |

`FA-PT-MPS` 失败时不得静默落到 CPU；必须把该臂标记为 `infeasible`，另跑 `FA-PT-CPU`。
每个模型制品记录来源、revision、SHA-256、文件清单和运行时识别结果。下载时优先检查 ModelScope；
只有目标 GGUF/版本缺失或运行时明确要求时才回退官方 Hugging Face/GitHub release，并记录原因。

模型制品不得放在 Git 工作树内。当前本机使用 ModelScope cache 的标准 repo/snapshot 布局；每台
执行主机通过 `modelscope scan-cache` 定位实际绝对路径，并把该路径写入本地、不入库的
`profile.json`。`FunASRNanoWSProfile` 拒绝相对 `model_dir`，防止候选模型重新落回 `runtime/`。

当前快照的关键文件 SHA-256（2026-08-24 迁移前后复核一致）：

| 相对路径 | SHA-256 |
|---|---|
| `model.pt` | `81fec8616083c69377f3ceef36aba3655660ee0ca69a5d4a1e9810cd340ca499` |
| `config.yaml` | `daed38ea6484f5650fb32cbd9069b9aa13880acaf2bcb1f0bf4be2712837917c` |
| `configuration.json` | `b64a3a55d35bcbe2cf4d31f2d3ef25a423d3ba2ebff203298c27fa055f3c7612` |
| `multilingual.tiktoken` | `747979631e813193436aabcff7c1c235d37de8097b71c563ec8b63b7a515c718` |

正式 manifest 仍须列出所有影响推理的文件，不能只复制上述关键文件摘要。

### 3.2 排除臂

- Fun-ASR 7.7B：未开放可复现实验权重，不参与。
- Fun-ASR vLLM：本机无 CUDA/Ampere，不参与 Apple Silicon 排名。
- CAM++ diarization：主实验固定 Sortformer，避免同时改变 ASR 与分人；CAM++ 可另立后续因子实验。
- 云端 API：违反全本地/离线目标，不参与。

### 3.3 Stage 0 可行性门禁

每个候选先用 10 个公开或自有短样本完成：

1. 从项目外已校验 snapshot 本地离线加载，网络禁用时不尝试下载。
2. `FA-PT-MPS` 必须显式使用 MPS 并检查 profiler/log；任何 CPU fallback 都判该臂
   `infeasible`，不得把 fallback 结果记为 MPS。
3. MPS 无论成功与否都独立运行 `FA-PT-CPU`，两者使用不同 manifest 和 run ID。
4. 16kHz mono s16le 输入正确，非 16kHz 输入由统一预处理器转换一次。
5. 普通话、英文、静音各能得到结构合法结果。
6. 流式臂能完成 ready → partial/confirmed → final，EOF 不死锁；本机暂不要求官方 vLLM WS。
7. 结果无 NaN、负时间戳、时间倒退或超出音频长度。
8. 进程退出后统一内存和文件描述符释放；仅流式服务臂检查端口释放。

门禁失败的臂保留错误、环境和日志证据，状态记为 `infeasible`，不进入统计排名，也不以零分填充。

### 3.4 Stage 0 初步探测记录（2026-08-24 22:21-22:23 CST）

以下结果来自模型自带 `zh.mp3`、`en.mp3`、`ja.mp3` 和临时生成的 3 秒 16kHz mono s16le 静音，
只证明本机运行时可行；模型自带样例不具备独立准确率证据，且当前 4 条样本尚未满足 10 条 Stage 0
门禁。

| 设备 | 真实参数 device | 加载时间 | zh RTF | en RTF | ja RTF | 静音结果 | 进程峰值 RSS |
|---|---|---:|---:|---:|---:|---|---:|
| MPS | 全部 `mps:0` | 8.728s | 0.0818 | 0.1404 | 0.0713 | 仅空白 | 约 6.91 GiB |
| CPU（4 threads） | 全部 `cpu` | 8.376s | 0.1870 | 0.2868 | 0.3437 | 仅空白 | 约 6.91 GiB |

MPS 进程设置 `PYTORCH_ENABLE_MPS_FALLBACK=0`，并在推理前检查所有参数 device；MPS/CPU 都使用项目
外同一 snapshot、FunASR 1.4.2、PyTorch 2.13.0 和关闭更新/联网的环境。三种语言的文本在两设备间
一致；静音文本经主归一化后为空。未加热词时中文样例把“开放”识别为“开饭”，加入“开放时间”
热词后可纠正，因此热词实验必须与无 context 主实验分开登记。

单独的首次 MPS generate 曾为 3.101s，后续同进程样例明显更快，说明存在显著 warm-up 效应；上表
仅是一次功能探测，不用于宣称设备性能优劣。正式 Stage 1 仍按 §5.2 分离冷启动与 warm 重复、轮换
顺序并报告置信区间。

新增 adapter 的真实 MPS 接线复测已完成：5616ms raw PCM 按 20ms chunk 输入后产生
`ready → final`，最终文本为“开放时间早上九点至下午五点。”，整段人工边界为 5616ms，全部模型
参数仍在 `mps:0`，且未写临时音频。首次实现曾因 FunASR 1.4.2 的 `FunASRNano.generate_chatml`
实际只接受 `str`/`torch.Tensor` 而拒绝文档所称可用的裸 ndarray；现已在 engine 边界显式把内存
float32 ndarray 转为 tensor，并加入 vendor 行为回归测试。

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
├─ vendor-events.jsonl
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

### 9.5 Runner 命令契约

正式运行前，`manifest.json`、`corpus.json` 和 `profile.json` 必须在 blind set 开封前冻结。语料与
模型均位于项目目录外；runner 会拒绝解析后仍落在 Git 工作树内的 `model_dir`。清单只使用相对于
各自根目录的文件路径。

Fun-ASR WebSocket 候选的本地 `profile.json` 示例；`model_dir` 必须替换为执行机
`modelscope scan-cache` 返回的项目外绝对 snapshot 路径：

```json
{
  "kind": "funasr-nano-ws",
  "model_dir": "/absolute/path/outside/repository/FunAudioLLM--Fun-ASR-Nano-2512/snapshots/master",
  "language": "中文",
  "host": "127.0.0.1",
  "port": 10095,
  "hotwords": [],
  "connect_timeout_secs": 5.0,
  "final_timeout_secs": 10.0
}
```

原生 PyTorch 离线实验使用无端口 profile，并显式指定设备：

```json
{
  "kind": "funasr-nano-pytorch",
  "model_dir": "/absolute/path/outside/repository/FunAudioLLM--Fun-ASR-Nano-2512/snapshots/master",
  "language": "中文",
  "device": "mps",
  "hotwords": [],
  "itn": true,
  "ncpu": 4
}
```

该 profile 必须使用 `--mode offline`；MPS 与 CPU 分别冻结独立 profile、manifest 和 run ID。

```bash
uv run vr-asr-benchmark run \
  --manifest manifests/run.json \
  --corpus manifests/corpus.json \
  --corpus-root corpus-root \
  --profile manifests/profile.json \
  --repo-root . \
  --mode realtime-1x

uv run vr-asr-benchmark score \
  --run-dir runtime/benchmarks/asr/<run_id>

uv run vr-asr-benchmark compare \
  --baseline runtime/benchmarks/asr/<baseline-run-id> \
  --candidate runtime/benchmarks/asr/<candidate-run-id> \
  --output runtime/benchmarks/asr/comparisons/<comparison-id>.json \
  --bootstrap-iterations 10000 \
  --seed 20260824
```

`run` 拒绝覆盖已有产物；任一样本失败仍写入 `failures.jsonl` 和带显式 `error_status` 的
`hypotheses.jsonl`，不删除失败样本，也不把 `unsupported`、`missing` 或 `infeasible` 填成 0。

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
| Promote | 全部硬门禁通过；主 CER superiority；其他主指标满足非劣界值 | 完成受控真实试运行后设为唯一默认后端 |
| Specialized | 特定层显著更好，但总体或延迟未达默认门槛 | 不进入当前生产主链；仅在另立需求和实验后考虑独立用途 |
| Experimental | CI 跨越阈值或样本量不足，但无安全失败 | 扩充语料，不改生产默认后端 |
| Reject | 任一硬门禁失败，或质量/延迟明确越过劣界 | 保持现基线并归档失败证据 |
| Infeasible | 本机运行时/设备无法正确执行 | 不排名，不宣称质量优劣 |

字幕/会议与交互助手分别分类。Fun-ASR 可以在会议中 Promote，而在交互中因延迟 Reject；这是有效结论。

## 11. 执行顺序与停止规则

1. 使用现有统一 runner 复跑 Qwen3 基线，确认自身重复运行输出一致。
2. 对 Fun-ASR 执行本地 checkpoint、MPS 和 CPU Stage 0；官方 vLLM WS 直接记录本机
   `infeasible`。
3. 实现原生 PyTorch 离线 adapter，冻结公开/dev 语料、归一化、analysis plan、模型 revision 和
   配置。
4. 在 dev 上做有限参数选择，每个可行实验臂使用同等调参预算。
5. 冻结参数并运行 Stage 1 blind。
6. 只有准确率未明确劣于基线的臂才建设本机流式 runtime 并进入 Stage 2。
7. 只有实时门禁通过的臂才接入固定的生产候选配置并进入 Stage 3/4，不建设运行时切换。
8. 只有系统链路无硬失败的臂进入 Stage 5。
9. 生成带 CI、失败率、分层结果和 manifest 链接的决策报告；完成真实试运行后固定唯一后端并清理
   落选模型和专用生产接入。

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
