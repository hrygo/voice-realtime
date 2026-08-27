---
title: "ASR Public Proxy v1 预评测报告"
description: "ASR 公共代理语料初测与流程校验报告"
status: archived
type: benchmark_report
category: asr
version: "v1.0.0"
date: 2026-08-25
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-core"
tags:
  - asr
  - benchmark
  - pilot-test
  - qwen3-asr
  - fun-asr
  - sensevoice
scope:
  - "voice_realtime.asr"
related_documents:
  - "docs/solutions/Fun-ASR与现有ASR后端科学对比测试方案.md"
  - "docs/benchmarks/asr/public-operational-proxy-v2-20260825/report.md"
---

# ASR Public Proxy Core 对比报告

**执行时间：** 2026-08-25（Asia/Shanghai）  
**状态：** `Experimental / Public Proxy`；不得用于生产 Promote  
**冻结语料：** `public-proxy-v1-20260825` Proxy Core，60 分钟、1,185 条、8 个
`content_group_id` cluster

## 结论

在 AliMeeting + ASCEND 公共代理 Core 上，Qwen3-ASR-1.7B 的 macro CER 为 `10.11%`，低于
Fun-ASR-Nano 的 `13.34%` 和 SenseVoiceSmall 的 `13.69%`。Fun-ASR 相对 Qwen 的分层等权
macro CER 高 `3.23` 个百分点，10,000 次配对 cluster bootstrap 95% CI 为
`[2.31, 3.85]` 个百分点；该公开代理证据不支持用 Fun-ASR 替换当前 Qwen 字幕/会议基线。

Fun-ASR 相对 SenseVoice 的 macro CER 低 `0.36` 个百分点，但 95% CI 为
`[-1.60, 0.63]` 个百分点，跨越 0，质量上不能区分。Fun-ASR 的离线 RTF 明显更低，但
峰值 RSS 更高；离线短片段结果不能替代语音助手的端到端时延、外放恢复和回声安全测试。

这些公开数据可能已进入候选模型训练，且不代表本产品目标域。结果只能作为 adapter、资源采样、
评分和报告管线校准以及公开代理证据；正式选型仍需预冻结已授权目标域 Core/Reserve。在此之前，
三个后端均不得因本报告获得生产 `Promote` 或触发落选模型清理。

## 冻结条件

- 三臂使用同一 git commit `42bac3561d7aae75d4aecc14d1c38a792afbd8ef`。
- Core input manifest SHA-256：
  `5fc2a7a10599140090b0e71a3dfd9b564bd55439d01fa21139dc153b1fb9e357`。
- Core reference manifest SHA-256：
  `ff4edf5175b66fbbc0671683027488e9cd8b31a0887a2387690e1e035c595a19`。
- 场景配额：meeting 30 分钟、code-switch 10 分钟、clean 20 分钟；Core/Reserve
  `content_group_id` 零交叉。
- 运行顺序固定为 Qwen MPS → SenseVoice CPU → Fun-ASR MPS；模型、服务、评分和测试均未并行。
- LM Studio、Voice UI、TTS bridge、字幕服务在模型实验期间均未运行；实验使用项目外排他锁。
- 三臂全部完成后才显式打开 Core references；Reserve references 保持封存，未运行、未评分。

## 结果

### 质量

> 表中加粗表示该指标的观测优势；CER、RTF、墙钟与 RSS 均按“越低越好”判断，失败数相同则不加粗。

| 实验臂 | Macro CER | Micro CER | 样本 Macro CER | Clean | Code-switch | Meeting | 失败 |
|:---|---:|---:|---:|---:|---:|---:|---:|
| Qwen3-ASR MPS | **10.11%** | **9.09%** | **9.93%** | **8.86%** | **10.92%** | **10.54%** | 0/1,185 |
| SenseVoice CPU | 13.69% | 12.09% | 12.96% | 11.58% | 16.22% | 13.28% | 0/1,185 |
| Fun-ASR MPS | 13.34% | 11.35% | 12.88% | 11.37% | 15.08% | 13.56% | 0/1,185 |

### 性能与资源

| 实验臂 | RTF P50 | RTF P95 | 回放墙钟 | 进程树峰值 RSS |
|:---|---:|---:|---:|---:|
| Qwen3-ASR MPS | 0.0704 | 0.1075 | 4.27 min | 5.06 GB |
| SenseVoice CPU | 0.1889 | 0.3857 | 8.66 min | **3.34 GB** |
| Fun-ASR MPS | **0.0650** | **0.1036** | **3.72 min** | 7.43 GB |

回放墙钟包含 1,185 个短片段的 adapter 调度、收尾和资源采样，不等于纯模型推理时间。RTF 是逐样本
推理时长与音频时长之比；Qwen RSS 覆盖隔离 worker 子进程树。

### 配对 Cluster Bootstrap

差值方向固定为 `candidate - baseline`；CER 为正表示 candidate 更差。

| Baseline | Candidate | Macro CER 差 | 95% CI | Cluster / 样本 | 判断 |
|:---|:---|---:|---:|---:|:---|
| **Qwen3-ASR** | Fun-ASR | +3.23pp | [+2.31pp, +3.85pp] | 8 / 1,185 | Public Proxy 上 Fun-ASR 明确更差 |
| SenseVoice | **Fun-ASR** | **-0.36pp** | [-1.60pp, +0.63pp] | 8 / 1,185 | CI 跨 0，不能区分 |

bootstrap 在 `public-clean`、`public-code-switch`、`public-meeting` 三层内分别以
`content_group_id` 重采样完整 cluster，再对三层样本均值等权汇总；每层 4 个 cluster。旧的无
`--corpus` 样本级 CI 只保留为敏感性分析，不作为本报告主 CI。总计只有 8 个独立内容组且每层仅
4 个 cluster，因此这些 CI 仍属于公共代理探索性证据，不外推到目标域。

配对表中的加粗仅表示观测点估计方向上的 CER 优势，不等于统计显著或晋级。

## 运行异常与修复

首个 Qwen v1 运行完成 1,048 条、失败 137 条；失败条目全部属于 `zh-en` code-switch，合计恰好
10 分钟。根因是 runner 把冻结语料的混合语言标签原样交给只接受单语言名的 Qwen adapter，属于
公共接口缺口，不是模型推理质量失败。

修复提交 `42bac35` 将 `zh-en`、`en-zh`、`mixed` 统一映射为后端原生 auto-detect：Qwen 传
`None`、SenseVoice 传 `auto`、Fun-ASR 不注入语言提示。修复由回归测试覆盖，旧 v1 失败产物保留为
诊断证据；三臂均以新的 v2 run ID 从头完整重跑，不拼接旧输出。

评分后又发现旧 `compare` 虽命名为 cluster bootstrap，实际按短样本重采样。现已新增可选
`--corpus`，通过冻结 manifest 的 `content_group_id` / `session_id` 执行真正的 cluster bootstrap，
并在结果中显式记录 `resampling_unit`、cluster 数及各层 cluster 数。

## 后续

1. 保持 Proxy Reserve 45 分钟封存；当前 Core 无管线异常，不追加公共代理运行。
2. 获取并在任何模型输出可见前冻结已授权目标域 Core/Reserve、cluster、reference 与分析计划。
3. 三臂串行执行目标域 Stage 1A；仅按预注册边界决定是否打开 Reserve。
4. 只有目标域 finalist 才进入字幕流式、交互外放和 60 分钟可靠性测试。
