---
title: "ADR-004：ASR 选型采用两阶段序贯盲测与 Finalist-Only 验收"
description: "确立 ASR 评测采用 Stage 0 可行性门禁 + Core/Reserve 序贯盲测，防止偏倚与算力浪费"
status: accepted
type: decision_record
category: asr
date: 2026-08-24
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-core"
tags:
  - adr
  - asr
  - benchmark
  - sequential-evaluation
  - futility-stopping
scope:
  - "voice_realtime.asr"
related_documents:
  - "docs/solutions/Fun-ASR与现有ASR后端科学对比测试方案.md"
  - "docs/superpowers/plans/2026-08-24-asr-scientific-comparison-completion.md"
---

# ADR-004：ASR 选型采用两阶段序贯盲测与 Finalist-Only 验收

## 状态

Accepted

## 日期

2026-08-25

## 背景

ASR v1.1 方案保留了 105 分钟目标域 Blind Set、公开集、流式、会议、交互和每晋级臂 60 分钟长跑，
但把“单臂预算”误作全实验预算。严格串行且禁止模型资源竞争时，所有实验臂完整运行 Public、Fun CPU、
独立 Stage 3 冒烟、每臂 Stage 5 和生产收敛重复试运行，规划墙钟可达到约 13–15 小时；其中多项运行
并不改变生产决策。

直接把 Blind Set 固定缩短为 60 分钟会降低不确定结果的统计功效，并允许事后选择性补测。继续让每个
候选固定执行完整 105 分钟和全部系统长跑虽然保守，却会把明显失败的候选带入昂贵的实时和可靠性阶段。
本项目还要求任何时刻只加载一个 ASR 模型，因此不能用并行运行掩盖重复工作。

## 决策

- 保留 105 分钟最大目标域 Blind Set，但在任何模型输出可见前同时冻结 60 分钟 `blind-core` 和
  45 分钟 `blind-reserve`；两个 look 使用互不重叠的完整分析 cluster，并覆盖全部主层。
- Stage 1 采用当前 cluster-bootstrap runner 可直接实现的保守 alpha spending：Core look
  $\alpha_1=0.01$，Core+Reserve final look $\alpha_2=0.04$；每个固定决策 family 在每个 look 内
  使用 Holm 校正，总体错误率通过 union bound 控制在 0.05 以内。
- 只允许冻结程序在 60/105 分钟两个固定点输出 `Advance-Early`、`Reject-Hard`、
  `Reject-Futility`、`Continue`、`Finalist` 或 `Experimental`。不得逐样本查看并停止；Reserve
  reference 不得在 Core 输出可见后才标注。
- Qwen、SenseVoice、Fun MPS 是 Stage 1 的三个 primary 臂。Fun MPS 输出同时服务字幕/会议与交互
  两个决策；Fun CPU 在 MPS 可行时只保留 Stage 0 兼容证据。完整 Public 延后到最终 baseline + winner。
- Stage 2/4 的 Screen 是同一 run/session 的前置窗口，通过后直接延长至 Confirm 总量；baseline 与
  candidate 使用相同冻结输入。Stage 3 不再独立运行 15 分钟冒烟，前 5 分钟并入主会话 preflight。
- Stage 1–4 只能产生 `Finalist / Reliability Pending`。每个决策方向最多一个 finalist 执行
  60 分钟 Stage 5；会议候选的 Stage 3 前 30 分钟与同一 60 分钟连续长跑复用。
- 生产收敛按 `git_commit + model_hash + profile_hash + runtime_config_hash` 复用 Stage 3/4/5 证据，
  默认只做 3–5 轮增量 smoke，不固定重复完整会议、交互或 30 轮回归。
- 模型加载、服务、实验和全量门禁继续严格串行，并由项目外主机锁覆盖完整生命周期。

## 备选方案

### 固定 60 分钟 Blind Set

执行最快，但 pilot 方差较大时只能得到 `Experimental / No decision`，且事后追加样本会产生可选停止
偏差。拒绝把最大证据集永久缩短为 60 分钟。

### 固定 105 分钟并让所有候选完成 Stage 2–5

证据最保守，但会重复 Public、CPU 对照、系统冒烟、每臂长跑和生产试运行；明显失败候选仍消耗昂贵
实时墙钟。拒绝把完整路径作为每个候选的默认路径。

### Lan-DeMets O'Brien–Fleming 边界

理论效率更高，但当前 runner 没有 canonical joint distribution、stagewise-adjusted p-value 和
sequential CI。直接把普通 bootstrap CI 套到 O'Brien–Fleming 边界会夸大证据，因此暂不采用；
未来只有在这些统计能力完整实现并验证后才能用后继 ADR 替换。

## 后果

- 在 Qwen RTF 尚未实测、暂以 1.0 排程的假设下，典型机器墙钟约 5.4–6.1 小时，Reserve 全开约
  6.3–7.0 小时；Stage 0 后必须用实测 RTF 重算，不能把排程假设当性能结论。
- 最坏情况仍保留完整 105 分钟证据；早期明确失败可以同时跳过 Reserve 和后续系统阶段。
- 完整 Core/Reserve reference 必须在首个 look 前冻结，因此机器早停不会自动减少约 10–15 人工工时。
- `Advance-Early` 不是生产 Promote；只有用途专属 60 分钟可靠性和全部硬门禁通过后才能替换默认后端。
- 未执行 Stage 5 的候选必须标记 `deferred/not_run`，不能错误归类为 Promote 或 Reject。

## 参考

- [`../solutions/Fun-ASR与现有ASR后端科学对比测试方案.md`](../solutions/Fun-ASR与现有ASR后端科学对比测试方案.md)
- [`../superpowers/plans/2026-08-24-asr-scientific-comparison-completion.md`](../superpowers/plans/2026-08-24-asr-scientific-comparison-completion.md)
- https://www.fda.gov/media/78495/download
- https://doi.org/10.1016/S0197-2456(00)00057-X
