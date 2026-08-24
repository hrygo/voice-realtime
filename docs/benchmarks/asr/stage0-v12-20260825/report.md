# ASR Stage 0 v1.2 可行性门禁报告

**执行日期：** 2026-08-25（Asia/Shanghai）  
**运行代码身份：** `75e7c48532328b82d534343e7950d246f32b0942`  
**证据范围：** 本机离线加载、统一回放协议、设备真实性、结构合法性、失败率与资源释放；不构成模型质量排名。

## 结论

Qwen3-ASR-1.7B MPS、SenseVoiceSmall CPU 与 Fun-ASR-Nano-2512 MPS 均完成 10/10 条门禁样本，失败数为 0，状态均为 `feasible`。Fun-ASR CPU 的 2026-08-24 结果保留为历史设备兼容证据，不进入 Stage 1 正式排名。

本轮语料包含模型自带样例、本机合成短句和静音，存在来源偏倚与合成语音偏倚。因此下表中的 CER 只用于验证评分链路，不得据此选择或淘汰后端。正式质量结论必须使用尚未开封的独立目标域 Core/Reserve blind set。

## 统一实验身份

- 三个本轮实验臂使用同一 `corpus-input.json`，SHA-256 为 `ee7321a6e5f1fb87ecd852097c16d882a81d91a291199c9d9b33880c01dacd78`。
- 同一 reference manifest SHA-256 为 `2c92ce148a32c24f2bef57a375ff2b703e1f3d6fdb40ccc17d517819073367e2`；run 阶段不可读，三个盲输出完成后才显式开封评分。
- 三个本轮 run 均冻结 `chunk_ms=20`、`final_timeout_secs=120`、模型全量文件 hash、profile、device、dtype 和 runtime revision。
- 运行期间 8100/8765/8001/10095 均无监听，LM Studio 无已加载模型；各实验臂在同一主机排他锁下严格串行执行。
- 原始音频、reference、hypothesis、逐样本事件与资源记录均位于项目外 `~/.cache/voice-realtime/benchmarks/asr/stage0-v12-20260825/`；Git 仅保存聚合结果。

## 聚合结果

| 实验臂 | 状态 | 完成/失败 | 首条冷启动 wall | Warm RTF P50 / P95 | Warm wall P50 / P95 | Gate-only macro / micro CER | 峰值 RSS |
|:---|:---:|---:|---:|---:|---:|---:|---:|
| Qwen3-ASR-1.7B MPS | feasible | 10 / 0 | 3.747s | 0.0619 / 0.0783 | 266 / 422ms | 0.0971 / 0.0872 | 不可比¹ |
| SenseVoiceSmall CPU | feasible | 10 / 0 | 3.351s | 0.1080 / 0.1481 | 478 / 505ms | 0.1620 / 0.1831 | 3.11 GiB |
| Fun-ASR-Nano-2512 MPS | feasible | 10 / 0 | 10.850s | 0.0573 / 0.0710 | 242 / 388ms | 0.0699 / 0.1192 | 6.92 GiB |
| Fun-ASR-Nano-2512 CPU | reused / Stage 0-only | 10 / 0 | 12.859s² | 0.5924 / 0.7561² | 2623 / 2930ms² | 0.0699 / 0.1192² | 约 6.91 GiB² |

¹ Qwen 模型运行在隔离子进程，当前 runner 的 `resources.csv` 只采样父进程，记录的约 43 MiB 不能代表模型内存，故不参与资源比较。Stage 1 前必须补齐子进程树 RSS 采样。  
² 来自 commit `379ad7e6124db46f549504422b7e60dc3b9a6bb6` 的历史兼容门禁，只作复用证据；未按本轮 runner 身份重跑。

“首条冷启动 wall”包含模型加载、首次编译/预热和首条推理，不等同于纯模型加载时间。Warm 指标严格按原始 `hypotheses.jsonl` 的执行顺序排除第一条后计算；不能从按 sample ID 排序的评分文件推断执行顺序。

## 诊断过程与修复

| 实验臂 | 初始现象 | 根因 | 处理与最终证据 |
|:---|:---|:---|:---|
| Qwen MPS | 两次运行均为协议失败 | profile 将隔离 venv 的 `bin/python` 解析成基础解释器，丢失隔离环境 | 保留解释器入口的绝对路径并增加回归测试；最终 10/10 |
| SenseVoice CPU | 首次 10/10 加载失败 | 旧 Hugging Face 快照缺少 `tokens.json` | 切换到完整 ModelScope snapshot，并冻结完整文件 hash；最终 10/10 |
| Fun-ASR MPS | 两条样本超时后进程 SIGSEGV | `asyncio.to_thread` 超时不取消底层 MPS 推理，下一条样本与残留 Metal kernel 重叠 | adapter 关闭时等待 in-flight 推理收敛；冻结 120s final timeout；最终 10/10 |

所有失败产物均单独保留，没有并入最终聚合结果。Fun-ASR 的 crash 证据为本机 macOS DiagnosticReports 中的 PyTorch MPS `EXC_BAD_ACCESS/SIGSEGV`，未复制进仓库。

## 对后续计划的影响

按三个 primary 臂的实测 warm RTF P50（Qwen `0.0619`、SenseVoice `0.1080`、Fun-ASR `0.0573`）估算：

$$T_{Core}=60\times(0.0619+0.1080+0.0573)\approx13.6\text{ min}$$

$$T_{Full}=105\times(0.0619+0.1080+0.0573)\approx23.9\text{ min}$$

该估算仅用于排程，并额外预留冷启动、写盘、锁切换和温控恢复时间。相较原先以 Qwen RTF=1 的保守估算，Stage 1 Core/完整回放分别减少约 60/105 分钟。后续总墙钟仍主要由实时 Stage 2–5、人工标注和可靠性长跑决定。

## Stage 0 验收

- [x] 三个 primary 臂的本地模型、runtime、device 与文件 hash 可验证。
- [x] 三臂使用相同 PCM bytes、20ms 分块与逐样本语言。
- [x] 盲输出不含 reference、CER、S/D/I/N；评分显式开盲后生成独立文件。
- [x] 三臂严格串行，结束后模型进程、端口和 LM Studio 均为空闲。
- [x] Stage 0 只判定可行性，没有生成质量选型结论。
- [ ] Qwen 隔离子进程树 RSS 采样在 Stage 1 前补齐。
