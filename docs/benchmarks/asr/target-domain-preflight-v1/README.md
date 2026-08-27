---
title: "ASR 目标域预检协议与规范"
description: "目标域 ASR 录音盲测预检协议、脱敏约束与外部数据规范"
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
  - preflight
  - target-domain
  - privacy
scope:
  - "voice_realtime.asr"
related_documents:
  - "docs/solutions/Fun-ASR与现有ASR后端科学对比测试方案.md"
  - "docs/benchmarks/asr/corpus-v12-20260825/source-inventory.md"
---

# 目标域 ASR blind metadata-only 前置包

状态：`tooling_ready / data_not_provided`（2026-08-25）。本目录只保存操作契约，不保存音频、逐字稿、
授权原件、姓名、speaker 映射、个人绝对路径或实际 metadata。

## 目的

`preflight-corpus` 在不读取音频和 reference 正文的条件下，提前发现目标域 Stage 1 的配额、授权、
脱敏、匿名身份、跨 look 泄漏和人工标注状态问题。成功状态仅为 `metadata_ready`，不代表音频、标注或
正式 blind 已冻结，更不能触发模型运行或 `Promote`。

## 项目外目录

由数据负责人明确提供项目外根目录，推荐结构：

```text
<external-root>/voice-realtime/asr/<corpus-version>/
├── preflight/
│   ├── blind-preflight.json
│   └── preflight-report.json
├── blind-core.json
├── blind-reserve.json
├── sealed/
│   ├── blind-core.references.json
│   └── blind-reserve.references.json
├── pcm/
├── pilot/power-simulation.json
├── analysis-plan.json
├── runs/
├── comparisons/
└── checksums.json
```

## Metadata 契约

`blind-preflight.json` 使用严格 schema，顶层字段为：

- `corpus_version`、`normalization_version`；
- `sources`：只含 `source_token`、source snapshot SHA-256、匿名 `authorization_ref`、授权状态、
  脱敏状态和人工复核布尔值；
- `candidates`：opaque sample/session/content/cluster/speaker token、相对 source locator、frame 区间、
  唯一时长、主场景和正交标签，且必须 `synthetic=false`；
- `references`：只含 sample ID、reference 制品 SHA-256、revision、normalization、双人标注和裁决状态，
  严禁 `reference_raw`；
- Core/Reserve 固定时长、场景配额和 speaker 下限。

匿名 token 必须使用 `source:`、`authorization:`、`session:`、`content:`、`cluster:`、`speaker:`
命名空间，不得包含 `/`、反斜杠、空格、姓名或私人路径。

## 串行操作

```bash
VR_ASR_EXTERNAL_ROOT=/path/to/external/voice-realtime/asr/<corpus-version>

uv run vr-asr-benchmark preflight-corpus \
  --metadata "$VR_ASR_EXTERNAL_ROOT/preflight/blind-preflight.json" \
  --output-report "$VR_ASR_EXTERNAL_ROOT/preflight/preflight-report.json" \
  --repo-root .
```

预检报告不可覆盖，权限为 `0600`。修正 metadata 后使用新的版本目录重新生成，不修改旧报告。
报告同时冻结完整 metadata hash、cluster-set hash 和 Core→Reserve sample-order hash；正式
`freeze-analysis` 会再次与实际 input manifests 比对，避免只凭相同时长和 cluster 集误绑定其他样本。

## `metadata_ready` 后仍需完成

1. 人工核验 source snapshot、授权/同意、脱敏映射和 annotation revision；
2. 读取真实音频并制备 16 kHz mono s16le，验证 PCM 长度、frame、channel 和 hash；
3. 校验 reference 与 input 的 split/version/normalization/sample set/hash；
4. Core/Reserve reference 同时设为 mode `000`；
5. 绑定 preflight、显式 `analysis_cluster_id`、候选 profile 和 dev/pilot 统计设计，执行
   `freeze-analysis`；冻结时必须再次提供原始 metadata、已物化 PCM 根目录和 10,000 次 power
   simulation，重新核验 hash/字节长度/跨 look 隔离；
6. 为每个实验臂生成带 `candidate_id` 和冻结 `profile_sha256` 的 run manifest；
7. 严格串行运行 Stage 1 Core，只有 `Continue` 才开 Reserve；正式 `compare/decide` 必须核验 run、
   scored hypotheses 与 comparison 的完整 SHA-256 证据链。
