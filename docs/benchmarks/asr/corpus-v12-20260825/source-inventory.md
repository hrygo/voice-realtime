---
title: "ASR 语料库 v1.2/v1.3 资产清单与源溯源"
description: "ASR 评测语料清单、切片来源、许可协议与哈希资产台账"
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
  - corpus
  - source-inventory
  - dataset
scope:
  - "voice_realtime.asr"
related_documents:
  - "docs/solutions/Fun-ASR与现有ASR后端科学对比测试方案.md"
  - "docs/benchmarks/asr/public-operational-proxy-v2-20260825/report.md"
---

# ASR v1.3 公共代理语料来源与完整性清单

**核验时间：** 2026-08-25（Asia/Shanghai）  
**状态：** Public Operational Proxy v2 的 60 分钟 Core 已完成并触发 futility；45 分钟 Reserve 仍封存。

## 结论

采用 AliMeeting Eval、ASCEND、HI-MIA-CW 与 AISHELL-4 Test 的最小候选组合，不再下载
MagicData-RAMC 与 MUSAN。v1 公共校准集使用 AliMeeting + ASCEND；v2 公共运营代理使用
AISHELL-4 Test + ASCEND，以 70% 会议、15% code-switch、10% clean、5% 真实非语音间隙覆盖当前
助手/会议用途。`noise`、`accent` 与实体标签仍不得靠来源名称自动推断。

这些数据作为“本项目用途相近的公共代理集”，不是本产品用户真实录音，也可能已进入候选模型训练。
v2 可以形成协议上 formal 的候选筛选证据，但 `evidence_class` 固定为 `public-operational-proxy`，
不能声称是未见数据、目标域证据或生产选型完成。程序化结论保持 `Experimental / No decision`；
本轮未扫描或读取个人录音目录。

## 来源、许可与制品

| 来源 | 固定身份与许可 | 本机制品 | SHA-256 / 完整性 |
|:---|:---|:---|:---|
| AliMeeting Eval | [OpenSLR SLR119](https://www.openslr.org/119/)，`CC BY-SA 4.0` | `Eval_Ali.tar.gz`，3,673,718,355 bytes | `dc47343b2474b5ebcf458927e878155f6ddeb59c85e685b3645c32a1f9578d92`；73 members / 66 files / 0 unsafe path |
| ASCEND | 发布方指向的 [CAiRE/ASCEND](https://huggingface.co/datasets/CAiRE/ASCEND)，revision `b65b9bb87a0412eb94a659660819060825e74b9f`，`CC BY-SA 4.0` | 5 个 parquet，1,223,536,062 bytes | 五个文件 hash 见下表；schema、行数、时长、speaker/session 已实读 |
| HI-MIA-CW | [OpenSLR SLR120](https://www.openslr.org/120/)，`CC BY-SA 4.0` | `data.tgz` 550,623,081 bytes；`resource.tgz` 55,193 bytes | `5de169ac…f83c91a` / `8628c75e…1ee4e4`；16,344 + 2 members / 0 unsafe path |
| AISHELL-4 Test | [OpenSLR SLR111](https://www.openslr.org/111/)，`CC BY-SA 4.0` | `test.tar.gz`，5,241,010,904 bytes | `7e5d306b5f18ab66fcd7e0380c90979b47fd9576bfa8e67e6353bdec7c14a35a`；63 members / 0 unsafe path / 0 links |

ModelScope 优先策略已实测执行：

- `OpenDataLab/ASCEND@master` 只有 `README.md`、`metafile.yaml`、`dataset_infos.json` 三个元数据文件，没有音频和逐字稿，因此回退到发布方指向的 Hugging Face 固定 revision。
- `modelscope/AliMeeting` 只有约 655KB manifest/说明，没有音频；并且卡片标注 Apache 2.0，与上游 OpenSLR 的 `CC BY-SA 4.0` 冲突。本项目一律以上游许可和音频为准。
- HI-MIA-CW 没有可确认的 ModelScope 完整镜像，直接使用 OpenSLR。中国镜像 TLS 证书过期后未绕过校验，改用 OpenSLR 页面列出的 EU HTTPS 镜像。
- AISHELL-4 同样未找到可校验的 ModelScope 完整 test 制品，回退到 OpenSLR 官方 EU HTTPS 镜像；
  中国镜像 TLS 证书过期，未关闭证书校验。

### ASCEND parquet hash

| 文件 | bytes | SHA-256 |
|:---|---:|:---|
| `test-00000-of-00001.parquet` | 105,756,434 | `a4c81d2b5ed6124f052089a695972808c16e0ce0c365ec9773c5d1a8fcf043a7` |
| `train-00000-of-00003.parquet` | 316,735,328 | `3d66ba76f324e0711b779cfb01ee4e772a24a929e1d77a2063cde0506f75976f` |
| `train-00001-of-00003.parquet` | 366,824,932 | `569f84f771c3637ca8535bd35e10e62feed2c240833534711909a9c04f51e589` |
| `train-00002-of-00003.parquet` | 327,687,102 | `aa76b7ef4a74ff111fd1d2573d0b69e6b6f901df6054618d8e5658a7a394523e` |
| `validation-00000-of-00001.parquet` | 106,532,266 | `3bdec53d2abfd3dd4f0d86a6df4e27e60f20660edc9b66055ae0ef8ec05cf7e2` |

## 实测候选池

### AliMeeting Eval

8 个 meeting session、25 个互不重复 speaker；近场为单通道，远场为 8 通道 16kHz WAV。总音频约 4 小时，单场 26.2–37.3 分钟；按 TextGrid 合并后的有效语音约 233 分钟，session overlap 比例约 7.2%–57.4%。

预分配保持完整 session/speaker 隔离：

| Split | Sessions | Speakers | 主要用途 |
|:---|:---|---:|:---|
| Dev | `R8007_M8010` | 4 | 高重叠校准，不进入 blind |
| Proxy Core | `R8001_M8004`、`R8003_M8001`、`R8008_M8013`、`R8009_M8019` | 13 | far-field、meeting 候选 |
| Proxy Reserve | `R8007_M8011`、`R8009_M8018`、`R8009_M8020` | 8 | far-field、meeting 候选 |

远场抽样固定使用同一阵列通道，不对 8 通道做平均混音；单流 CER 只采用无跨 speaker overlap 的完整标注区间。含 overlap 的连续会议保留为 secondary stress set，使用多说话人/时间轴指标，不混入单一文本 CER 主指标。near/far 同一会议必须绑定同一 split，并以 `content_group_id` 防止跨 split 泄漏。

### ASCEND

| Split | Rows | 实测时长 | Speakers | 用途 |
|:---|---:|---:|---:|:---|
| train | 9,869 | 31,589,400ms | 18 | Dev / Public 候选池 |
| validation | 1,130 | 3,323,603ms | 3 | Proxy Reserve code-switch / accent 候选 |
| test | 1,315 | 3,302,971ms | 2 | Proxy Core code-switch / accent 候选 |

三个 split 的 speaker 集合实测互斥。`session_id` 数字在不同 split 中重复，因此冻结 ID 必须使用 `ascend:<split>:<session_id>` 命名空间，禁止按裸整数判断 session 隔离。

### AISHELL-4 Test

归档包含 20 个会议 session，每个 session 提供 8 通道 16kHz FLAC、TextGrid 与 RTTM。v2 从发布方
TextGrid 构造无跨 speaker overlap 的语音片段，并从标注间隙构造真实非语音负样本；归一化会删除
`<sil>` 等发布方控制标记，但不改写词义。Core/Reserve 各分配 10 个完整 AISHELL-4 session，session
与匿名 speaker ID 均不跨 look。发布方 reference 以 `publisher_verified` 进入公共代理 preflight，
不伪装成本地双人标注。

### HI-MIA-CW

16,343 条 16kHz WAV，35 个 speaker，均有逐文件 transcription；内容是“Hi, Mia”中文混淆词，适合作为 non-target speech negative。实测绝大多数文件的 frame 数不能整除 16，无法在不裁剪、不补齐的前提下得到当前 manifest 所要求的整数毫秒时长，因此不进入 `public-proxy-v1-20260825`。它保留为独立的误触发/幻觉专项候选，后续应以 frame 级 negative 协议单独冻结，不进入正文本 CER 分母。

## 已冻结公共代理集

`public-proxy-v1-20260825` 已于 2026-08-25 在项目外目录确定性生成和封存。seed 固定为
`asr-public-proxy-v1-20260825`，只选择 1–20 秒的完整 utterance 或无跨 speaker overlap 的完整
AliMeeting turn；没有补音频、裁剪、跨片段拼接或 8 通道平均混音。

| Split | 样本数 | 精确时长 | 场景配额 | Speaker |
|:---|---:|---:|:---|---:|
| Proxy Core | 1,185 | 3,600,000ms | meeting 30m、code-switch 10m、clean 20m | 14 |
| Proxy Reserve | 859 | 2,700,000ms | meeting 20m、code-switch 8m、clean 17m | 11 |

Core/Reserve 的 `content_group_id` 交集为空；PCM、manifest、references 和 provenance 均位于
`~/.cache/voice-realtime/benchmarks/asr/corpora/` 下，项目仓库不保存模型、音频或逐字稿。公开代理
manifest SHA-256 分别为 Core `5fc2a7a10599140090b0e71a3dfd9b564bd55439d01fa21139dc153b1fb9e357`
和 Reserve `04385bd2dbff9b011b0c0792cad468711a2bf3ac5c7b00867628407df12c1695`；references 在显式评分前
保持 `000` 封存权限。

Proxy Core 三臂串行回放、评分与 cluster bootstrap 已完成，汇总见
[`../public-proxy-v1-20260825/report.md`](../public-proxy-v1-20260825/report.md)。Core references 已按
评分协议打开为 `0600`；Reserve references 仍为 `000`，未运行、未开封。

## 已冻结公共运营代理 v2

`public-operational-proxy-v2-20260825` 使用 AISHELL-4 Test + ASCEND，所有 PCM、manifest、reference、
profile、analysis plan 与结果均位于 `~/.cache/voice-realtime/benchmarks/asr/`，仓库只保存聚合报告。

| Split | 样本数 | 精确时长 | Session / Speaker / Cluster | 场景配额 | Manifest SHA-256 |
|:---|---:|---:|:---:|:---|:---|
| Core | 802 | 3,600,000ms | 14 / 72 / 14 | meeting 42m、code-switch 9m、clean 6m、negative 3m | `21bced1787d7924805d3e2729ab19a4281367abcba3c67316db69902b32cafcb` |
| Reserve | 541 | 2,700,000ms | 14 / 57 / 14 | meeting 31.5m、code-switch 6.75m、clean 4.5m、negative 2.25m | `5c4b26908abfb787442be67d64bcbb6a0342b21605bcf55ff9a32cf12f8e2d32` |

两段的 session、speaker、content group 与 analysis cluster 交集全部为空；1,343 个 PCM 与两个输入
manifest 的 checksum 复核为 1,345/1,345 通过。Core/Reserve reference 在首次模型输出前同时以
`000` 封存，analysis plan SHA-256 为
`43dd4888d5d505a7b1f670b475db8e4985116ba9e921e698678311fc87eb8dda`。Core 三臂按
Qwen → SenseVoice → Fun-ASR 严格串行运行，均为 802/802、0 失败；完成统一开盲评分后 Core reference
重新设为 `000`，Reserve 始终未启封。程序化决定为两个 family 均 futility rejected，详细结果见
[`../public-operational-proxy-v2-20260825/report.md`](../public-operational-proxy-v2-20260825/report.md)。

## 冻结前验收条件

- [x] 所有原始归档/固定 parquet 位于项目外目录。
- [x] 原始制品大小、SHA-256、归档安全与关键 schema 已验证。
- [x] v2 Core/Reserve 的 AISHELL-4 session/speaker 与 ASCEND split speaker 互斥。
- [x] 四个公共候选来源许可均按上游 `CC BY-SA 4.0` 记录。
- [x] 采用固定 seed `asr-public-proxy-v1-20260825` 生成公共代理候选顺序。
- [x] 公共代理配额精确等于 Core 30/10/20 分钟和 Reserve 20/8/17 分钟。
- [x] 公共运营代理 v2 配额精确等于 Core 42/9/6/3 分钟和 Reserve 31.5/6.75/4.5/2.25 分钟。
- [ ] `noise` 具有实测 SNR/混响或明确环境证据，`accent` 经人工审听，`entity` 经冻结词典与人工复核。
- [x] v1/v2 公共代理输入/reference 使用独立 corpus version 和目录；未来目标域 blind 必须使用另一身份，指标不得合并。
- [x] metadata-only `preflight-corpus`、项目外 source/output 边界、显式 `analysis_cluster_id` 与 formal
  `freeze-analysis` 语义绑定工具已实现；操作契约见
  [`../target-domain-preflight-v1/README.md`](../target-domain-preflight-v1/README.md)。
- [x] v2 Core/Reserve reference、manifest、cluster、provenance 与 `analysis-plan.json` 在任何 Core 输出产生前同时封存。
