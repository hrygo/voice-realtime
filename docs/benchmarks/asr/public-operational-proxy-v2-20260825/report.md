---
title: "ASR Public Operational Proxy v2 盲测评测报告"
description: "基于 AISHELL-4 与 ASCEND 的序贯盲测实验报告与结论"
status: completed
type: benchmark_report
category: asr
version: "v1.3.0"
date: 2026-08-25
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-core"
tags:
  - asr
  - benchmark
  - blind-test
  - futility-stopping
  - qwen3-asr
  - fun-asr
  - sensevoice
scope:
  - "voice_realtime.asr"
related_documents:
  - "docs/solutions/Fun-ASR与现有ASR后端科学对比测试方案.md"
  - "docs/benchmarks/asr/corpus-v12-20260825/source-inventory.md"
---

# ASR Public Operational Proxy v2 Core 报告

**执行日期：** 2026-08-25（Asia/Shanghai）  
**实验提交：** `3c144c6b9f5305c3f98e243f8e31967c6958ac14`  
**证据类别：** `public-operational-proxy`  
**程序化结论：** 两个 family 均 `futility_rejected=true`；状态降级为 `Experimental / No decision`；不运行 Reserve，不改变生产默认后端。

## 结论

Fun-ASR-Nano-2512 在本轮公共运营代理 Core 上速度最快，且真实非语音间隙的非空输出率低于两个
基线；但它没有表现出预注册的 5% 相对 CER 改善。相对 Qwen 的会议 family，Fun-ASR 宏平均 CER
高 2.92 个百分点；相对 SenseVoice 的交互 family，高 0.49 个百分点。两个 family 的 Core 条件功效
都远低于 0.20，程序化决策均触发 futility。

这不是“Fun-ASR 在目标域被正式证明更差”：语料是公共代理，可能存在训练污染，且完整 105 分钟设计
的预注册功效只有 0.3095。它支持的工程动作是停止当前候选的 Reserve、流式适配和长跑投入，继续保留
Qwen 作为字幕/会议默认、SenseVoice 作为交互默认。

## 数据与冻结身份

Core/Reserve 使用 AISHELL-4 Test + ASCEND，模型、语料、reference、profile 与结果全部位于项目外的
`~/.cache/voice-realtime/benchmarks/asr/`。合成 Dev 只有 99 条专项样本，明确排除在 blind 证据之外。

| 项目 | Core | Reserve |
|:---|---:|---:|
| 时长 | 3,600,000ms | 2,700,000ms |
| 样本 | 802 | 541 |
| Session / Speaker / Cluster | 14 / 72 / 14 | 14 / 57 / 14 |
| Manifest SHA-256 | `21bced1787d7924805d3e2729ab19a4281367abcba3c67316db69902b32cafcb` | `5c4b26908abfb787442be67d64bcbb6a0342b21605bcf55ff9a32cf12f8e2d32` |
| Reference SHA-256 | `32c7de3af68f8cc36cbfcb63d4960d230ce088caa63e00250f11832c29354f8e` | `e3f2ea3cef24780502113d7c3cf9190ee8bdded033d6c5c165a375084e19357a` |
| 执行状态 | 已运行、统一开盲评分后复封 | 未运行、始终封存 |

Core/Reserve 的 session、speaker、content group 与 analysis cluster 交集均为空。`analysis-plan.json`
SHA-256 为 `43dd4888d5d505a7b1f670b475db8e4985116ba9e921e698678311fc87eb8dda`；
开盲前 10,000 次模拟得到 Core power=0.0656、Final power=0.3095、模拟 FWER=0.024。

## 执行控制

- 使用固定提交的干净 detached worktree；用户现有 UI 改动未参与实验，也未被修改。
- Qwen → SenseVoice → Fun-ASR 严格串行运行，共用一个项目外主机级排他锁。
- 每次换模型前复核 8100、8765、8001、10095 无监听且无 ASR 残留进程。
- 三个模型均从项目外 ModelScope cache 加载；没有并发模型或后台服务竞争资源。
- 三臂全部完成后才把 Core reference 从 `000` 显式打开为 `0600` 评分，随后恢复为 `000`；Reserve
  reference 始终为 `000`。
- 每臂固定 16kHz mono s16le、20ms chunk、无 hotword/context、确定性离线推理和 120 秒 final timeout。

比较阶段发现并修复了一个契约边界：负样本按设计没有 CER，因此 cluster manifest 可以包含这些不可
评分样本，但仍必须覆盖全部可评分样本。修复前正式比较 fail-closed；修复后新增“允许额外不可评分样本”
和“拒绝缺失可评分样本”回归测试，18 个指标测试通过。

## Core 聚合结果

> 表中加粗表示该指标的观测优势；CER、RTF 和负样本非空率均按“越低越好”判断，完成数相同则不加粗。

| 指标 | Qwen3-ASR MPS | SenseVoice CPU | Fun-ASR MPS |
|:---|---:|---:|---:|
| 完成 / 失败 | 802 / 0 | 802 / 0 | 802 / 0 |
| Macro CER | **11.47%** | 13.90% | 14.39% |
| Micro CER | **10.79%** | 13.19% | 14.42% |
| Sample Macro CER | **12.09%** | 15.50% | 14.96% |
| Public clean CER | **8.32%** | 10.36% | 8.86% |
| Public code-switch CER | **13.50%** | 14.51% | 18.93% |
| Public meeting CER | **12.57%** | 16.82% | 15.36% |
| RTF P50 / P95 | 0.098 / 0.173 | 0.135 / 0.352 | **0.059 / 0.092** |
| 负样本非空数 / 57 | 57 | 19 | **9** |
| 负样本非空率 | 100.00% | 33.33% | **15.79%** |

负样本来自 AISHELL-4 发布方标注中的真实非语音间隙，不等价于消声室绝对静音。这里的非空率是公开
代理 hallucination gate，不外推为生产静音误触发率，也不替代 Stage 2–5 的严重幻觉与回声安全测试。

## 预注册配对比较

候选减基线；正值表示 Fun-ASR CER 更高。只对 745 条 CER-supported 语音样本计分，57 条负样本进入
独立 hallucination gate；14 个 analysis cluster 使用 10,000 次全局 Bayesian cluster-weight
bootstrap，Core decision confidence=99%。

| Family | Baseline | CER 差 | 相对差 | 99% CI | p-value | 条件功效 | 决策 |
|:---|:---|---:|---:|---:|---:|---:|:---|
| meeting | **Qwen** | +2.92pp | +25.45% | [-0.15pp, +7.48pp] | 0.2447 | `3.75e-12` | futility |
| interaction | **SenseVoice** | +0.49pp | +3.51% | [-2.98pp, +5.38pp] | 0.9883 | `7.09e-6` | futility |

三项 Core non-inferiority gate（失败率、公共负样本幻觉、warm RTF P95）在两个 family 均通过。
这不构成晋级：superiority 边界未通过，且 conditional power 低于 0.20。

## 程序化停止与后续动作

`core-decision.json` 对 meeting 与 interaction 均记录：

- `advance_eligible=false`
- `hard_rejected=false`
- `futility_rejected=true`
- `required_gates_passed=true`
- `reason_codes=["futility", "underpowered_design"]`
- 顶层状态 `Experimental / No decision`

决策 JSON 的 `stopped_at` 为 `null`：当前契约在完整设计功效不足时不写入正式序贯停止标记，避免把
降级证据误报为充分证据。运营执行仍依据两个候选均已触发 futility、没有 `Continue`/finalist，停止
Reserve 和后续高成本阶段；这一动作不被表述为统计上正式拒绝。

因此 Reserve、Stage 2–5 和生产切换均停止。当前实验不授权删除 Qwen、SenseVoice 或 Fun-ASR 模型；
只有新的模型 revision、实质不同的解码/上下文方案，或新取得的已授权目标域 blind，才值得新建不可与
本轮混合的 experiment family。

## 聚合制品指纹

| 制品 | SHA-256 |
|:---|:---|
| meeting comparison | `2832cdb7dedf76d08f9caeb61a539a89e7986039ad93c4a356a2c6aeb986b526` |
| interaction comparison | `9a56a11759c94d1c465beb25e33501f43637678b3a9dfa30a7aaf1565fe9de00` |
| Core gate source | `5effbf3cb99bd2983ba42591cc43bc18d27e4a78ac8855cf1e79f14213079862` |
| Core family evidence | `fb1c7e373b397d38115856d6c13ddb747e235aeef38bfb8f57a5218ca4d318db` |
| Core decision | `5c1c4e40af6a62506fe65f01affbb87fad818e84a488ba3d32ccfce0e6c00e70` |

## 局限

- 公共语料可能进入过候选模型训练，无法证明 out-of-distribution 泛化。
- 14 个 Core cluster 对 5% 相对改善的统计功效不足；futility 是 non-binding 停止，不是正式劣势证明。
- 本阶段是模型核心离线比较，不包含真实 1× 流式 commit latency、Sortformer、EOF、重连、回声或
  60 分钟稳定性；没有 finalist，因此这些高成本阶段未启动。
- 发布方 reference 不等价于本项目目标域的双人标注与第三方裁决。
