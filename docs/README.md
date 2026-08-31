---
title: "Voice Realtime 文档中心"
description: "全本地实时语音交互、会议助手与实时字幕系统的技术文档总览、架构索引、状态生命周期与研发导航矩阵"
status: active
type: guide
category: architecture
version: "v2.1.0"
date: 2026-08-27
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-core"
tags:
  - documentation
  - index
  - architecture
  - sitemap
  - guide
---

# 📚 Voice Realtime 文档中心

> 欢迎来到 **Voice Realtime** 技术文档中心。本项目是一套面向 Apple Silicon 硬件定制的全本地离线、超低延迟实时语音交互（Voice Assistant）、结构化会议助手（Meeting Assistant，含 Sortformer 说话人分离 / PostgreSQL 持久化 / 异步 AI 纪要 / 崩溃恢复 Journal）与实时语音字幕（Live Subtitles）系统。

---

## 🚦 文档生命周期状态对照表（Status Legend）

为了清晰标识每篇文档的权威性与工程效力，本项目所有技术文档均在 YAML Frontmatter 中显式声明 `status`：

| 状态徽标 | 状态代码 (`status`) | 适用场景与定义 | 权威效力 |
|---|---|---|---|
| 🟢 **Active** | `active` | 核心系统架构、接口对接手册、当前生效的专项设计 | **权威基线**：当前系统正在运行和遵循的唯一事实源 |
| 🔵 **Accepted** | `accepted` | 架构决策记录 (ADRs) | **决策定稿**：团队已正式评审并批准采纳的技术决议 |
| 🟣 **Implemented** | `implemented` | 故障排障方案、已完成上线的规格设计与研发执行计划 | **已落地**：方案在代码库中已完全实现并验证通过 |
| 🟡 **Completed** | `completed` | 科学评测报告、联调验证记录、交接清单 | **已完成**：测试/评测/验收动作已结束，结论已归档 |
| ⚪ **Template** | `template` | 联调记录模板、报告模板 | **通用模板**：供后续发布/联调流程复用的标准模板 |
| 📦 **Archived** | `archived` | 历史预检数据集、探索性实验记录、早期演进文档 | **历史归档**：供技术溯源参考，不作为当前执行基线 |
| 🟠 **Draft / Review** | `draft` / `under_review` | 方案初稿、跨团队评审签署中的草案 | **非正式**：尚处于评审讨论阶段，尚未进入主线 |

---

## 🗺️ 文档目录结构分层体系

```text
docs/
├── README.md                              # 🧭 本文档：文档中心总览与索引导航矩阵
│
├── architecture/                          # 🏗️ 系统总体架构与核心子系统设计
│   ├── 系统总体架构与详细设计方案.md       # 系统总体逻辑/物理架构、时序流与模块规范 (v2.1)
│   ├── 全链路语音交互与会议助手-技术方案与实施方案.md # 全链路端到端总体方案、前沿调研与实施路线图
│   ├── 实时语音交互与字幕-方案与最佳实践.md # 实时语音交互/字幕架构与单 PCM owner 仲裁契约
│   └── 声学防回声与全双工交互设计方案.md   # 四层防回声死循环防御体系与外放免提全双工方案
│
├── solutions/                             # 💡 专项技术方案与深度设计
│   ├── 会议模式多说话人精准识别与声纹聚类技术方案.md # 一人多号根因分析、六层防护、CAM++质心池与全局AHC聚类
│   ├── 会议助手实时转录体验优化方案.md     # 确认/修订/暂存状态分层、段落聚合与阅读视图优化
│   └── Fun-ASR与现有ASR后端科学对比测试方案.md # ASR序贯盲测选型方案与决策报告 (v1.3)
│
├── manuals/                               # 📖 开发对接与运行手册
│   ├── Qwen3-ASR-实时语音转文字开发对接手册.md # WebSocket/REST 流式与文件转写对接接口手册
│   ├── 会议助手后端运行与前后端联调.md     # PostgreSQL环境准备、后端启动与前后端联调规范
│   └── Voice-Studio-UI-设计方案.md        # 前端控制台架构设计、组件状态机与交互契约
│
├── operations/                            # 📋 协作交接、联调记录与排障分析
│   ├── 会议助手前后端分离式开发准备方案.md # 契约优先前后端分离路线与开发准备方案
│   ├── 会议助手前后端分离工作交接清单.md   # C0/B1/D1/F1/Q1 五类工作包交接清单与验收基准
│   ├── 前后端接线验证记录-2026-08-26.md    # 2026-08-26 前后端联调接线验证记录
│   ├── 联调记录模板.md                    # 标准前后端联调验收记录模板
│   └── 语音交互打断后推理挂起故障排查与修复方案.md # Barge-in 打断导致 LM Studio 挂起故障排障与修复
│
├── decisions/                             # 📝 架构决策记录 (ADR-001 ~ ADR-010)
│   ├── 0001-single-owner-interaction-runtime.md
│   ├── 0002-lm-studio-stateful-chat-context.md
│   ├── 0003-lm-studio-context-compaction.md
│   ├── 0004-asr-sequential-evaluation.md
│   ├── 0005-server-side-runtime-workload-arbitration.md
│   ├── 0006-contract-first-meeting-assistant-separation.md
│   ├── 0007-bounded-meeting-summary-generation.md
│   ├── 0008-speaker-diarization-and-voiceprint-clustering.md
│   ├── 0009-shared-local-inference-platform.md
│   └── 0010-physical-output-audio-capture.md
│
├── benchmarks/                            # 📊 评测基准与实验资产
│   └── asr/
│       ├── corpus-v12-20260825/source-inventory.md
│       ├── public-operational-proxy-v2-20260825/report.md
│       ├── public-proxy-v1-20260825/report.md
│       ├── stage0-v12-20260825/report.md
│       └── target-domain-preflight-v1/README.md
│
└── superpowers/                           # ⚡ 历史执行计划与规格 (Plans & Specs 归档)
    ├── plans/                             # 研发执行计划 (14 份，含当前草案与历史归档)
    └── specs/                             # 设计规格 (12 份，含当前评审稿与历史归档)
```

---

## 🧭 按角色快速导航路径

```mermaid
graph TD
    User([开发者 / 贡献者]) --> Role{你的角色 / 任务}
    
    Role -->|系统架构 / 全局审计| Arc[1. 系统总体架构<br/>2. 决策记录 ADRs<br/>3. 工作负载仲裁]
    Role -->|后端研发| Be[1. 会议助手运行手册<br/>2. 架构详细设计<br/>3. 契约规范 contracts/]
    Role -->|前端研发| Fe[1. Voice Studio UI 方案<br/>2. 前后端联调手册<br/>3. 转录体验优化方案]
    Role -->|AI / 算法评测| Algo[1. 声纹聚类方案<br/>2. ASR 科学评测方案<br/>3. 评测报告与基准]
    Role -->|QA / 发布联调| Qa[1. 接线验证记录<br/>2. 联调记录模板<br/>3. 交接清单]

    Arc --> ArcDocs[docs/architecture/ & docs/decisions/]
    Be --> BeDocs[docs/manuals/ & contracts/]
    Fe --> FeDocs[docs/manuals/ & docs/operations/]
    Algo --> AlgoDocs[docs/solutions/ & docs/benchmarks/]
    Qa --> QaDocs[docs/operations/]
```

---

## 📑 全量文档索引矩阵

### 1. 系统总体架构与核心子系统 (`docs/architecture/`)

| 文档名称 | 状态 | 类型 | 版本 | 核心内容与设计要点 |
|---|---|---|---|---|
| [系统总体架构与详细设计方案](architecture/系统总体架构与详细设计方案.md) | 🟢 `active` | `architecture` | `v2.1` | **权威总体架构**：权威拓扑、分层架构、交互/字幕/会议/控制端到端时序与详细设计规范 |
| [全链路语音交互与会议助手-技术方案与实施方案](architecture/全链路语音交互与会议助手-技术方案与实施方案.md) | 🟢 `active` | `architecture` | `v1.0` | **完整技术方案与实施路径**：架构、断句/分人/对账、前沿调研、ROI 与阶段落地 |
| [实时语音交互与字幕-方案与最佳实践](architecture/实时语音交互与字幕-方案与最佳实践.md) | 🟢 `active` | `architecture` | `v2.0` | 语音交互与字幕技术方案、单 PCM owner 仲裁契约及实测验收数据 |
| [声学防回声与全双工交互设计方案](architecture/声学防回声与全双工交互设计方案.md) | 🟢 `active` | `architecture` | `v1.0` | 纯外放免提场景下的物理声学回声抑制、状态共享、文本对账四层防回声死循环体系 |

### 2. 专项技术方案与深度设计 (`docs/solutions/`)

| 文档名称 | 状态 | 类型 | 版本 | 核心内容与设计要点 |
|---|---|---|---|---|
| [会议模式多说话人精准识别与声纹聚类技术方案](solutions/会议模式多说话人精准识别与声纹聚类技术方案.md) | 🟢 `active` | `domain_solution` | `v1.0` | **会议多说话人精准识别与声纹聚类**：一人多号根因分析、六层防护、CAM++质心池与全局 AHC 聚类 |
| [会议助手实时转录体验优化方案](solutions/会议助手实时转录体验优化方案.md) | 🟢 `active` | `domain_solution` | `v1.0` | 实时 ASR 状态分层、段落聚合、断线乱序状态一致性与阅读体验优化 |
| [Fun-ASR与现有ASR后端科学对比测试方案](solutions/Fun-ASR与现有ASR后端科学对比测试方案.md) | 🟡 `completed` | `benchmark_report` | `v1.3` | Qwen3-ASR vs Fun-ASR vs SenseVoiceSmall 序贯盲测与科学选型报告（Core 已触发 futility） |

### 3. 开发对接与运行手册 (`docs/manuals/`)

| 文档名称 | 状态 | 类型 | 版本 | 核心内容与设计要点 |
|---|---|---|---|---|
| [Qwen3-ASR 实时语音转文字开发对接手册](manuals/Qwen3-ASR-实时语音转文字开发对接手册.md) | 🟢 `active` | `manual` | `v1.0` | **Qwen3-ASR 开发对接手册**：WebSocket / REST 音频流式与文件转写对接指南 |
| [会议助手后端运行与前后端联调手册](manuals/会议助手后端运行与前后端联调.md) | 🟢 `active` | `manual` | `v1.0` | 会议助手运行手册、PostgreSQL 数据库准备、接口定义与前后端联调规范 |
| [Voice Studio UI 设计方案](manuals/Voice-Studio-UI-设计方案.md) | 🟢 `active` | `guide` | `v1.0` | 前端控制台架构设计、单源麦克风控制面、组件状态机与交互契约 |
| [Voice Studio 会议助手『内心 OS』前端 UI/UX 设计方案](manuals/Voice-Studio-会议助手-内心OS-UI-UX-设计方案.md) | 🟢 `active` | `specification` | `v1.0` | **内心 OS 专属设计方案**：私密副驾驶信息架构、事实/判断/草稿三层卡片、证据定位与状态机 |

### 4. 协作交接、联调记录与排障 (`docs/operations/`)

| 文档名称 | 状态 | 类型 | 版本 | 核心内容与设计要点 |
|---|---|---|---|---|
| [会议助手前后端分离式开发准备方案](operations/会议助手前后端分离式开发准备方案.md) | 🟡 `completed` | `technical_spec` | `v1.0` | 契约优先前后端分离路线设计、接口版本化与团队开发边界定义 |
| [会议助手前后端分离工作交接清单](operations/会议助手前后端分离工作交接清单.md) | 🟡 `completed` | `guide` | `v1.0` | C0/B1/D1/F1/Q1 五类工作包交接资料、执行基线、验收物与交接清单 |
| [前后端接线验证记录 (2026-08-26)](operations/前后端接线验证记录-2026-08-26.md) | 🟡 `completed` | `test_record` | `v1.0` | 会议助手前后端分离接线联调验证记录、测试结果矩阵与验收结论 |
| [会议助手前后端分离联调记录模板](operations/联调记录模板.md) | ⚪ `template` | `template` | `v1.0` | 每次契约/后端/前端版本发布前执行联调验收的标准记录模板 |
| [语音交互打断后推理挂起故障排查与修复方案](operations/语音交互打断后推理挂起故障排查与修复方案.md) | 🟣 `implemented` | `postmortem` | `v1.0` | Barge-in 打断导致 LM Studio 挂起死锁故障的根因分析与状态机修复 |

### 5. 架构决策记录 (`docs/decisions/`)

| ADR 编号 | 决策标题 | 状态 | 日期 | 核心决策要点 |
|---|---|---|---|---|
| [ADR-001](decisions/0001-single-owner-interaction-runtime.md) | 交互管道采用单一所有者运行时 | 🔵 `accepted` | 2026-08-20 | `vr-ui` 为交互管道唯一所有者，`vr-interact` 为互斥 headless 替代入口 |
| [ADR-002](decisions/0002-lm-studio-stateful-chat-context.md) | LM Studio 交互上下文采用原生有状态会话链 | 🔵 `accepted` | 2026-08-21 | 废弃 OpenAI 兼容端点，改用原生 `/api/v1/chat` + `reasoning: "off"` |
| [ADR-003](decisions/0003-lm-studio-context-compaction.md) | LM Studio 长会话采用结构化记忆预热与原子换链 | 🔵 `accepted` | 2026-08-21 | 基于输入 token 与 TTFT 动态监控，结构化摘要预热新链并原子换链 |
| [ADR-004](decisions/0004-asr-sequential-evaluation.md) | ASR 选型采用两阶段序贯盲测与 Finalist-Only 验收 | 🔵 `accepted` | 2026-08-24 | 确立 Stage 0 门禁 + Core/Reserve 序贯盲测，防止偏倚与算力浪费 |
| [ADR-005](decisions/0005-server-side-runtime-workload-arbitration.md) | 服务端状态机统一仲裁语音推理工作负载 | 🔵 `accepted` | 2026-08-25 | `RuntimeModeCoordinator` 四模式状态机与单 PCM 所有者仲裁 |
| [ADR-006](decisions/0006-contract-first-meeting-assistant-separation.md) | 以契约优先支持会议助手前后端团队分离 | 🔵 `accepted` | 2026-08-26 | 单仓架构下以 `contracts/` 目录为唯一事实源，分离生产与消费 |
| [ADR-007](decisions/0007-bounded-meeting-summary-generation.md) | AI 会议纪要采用有界分段生成与服务端事件收敛 | 🔵 `accepted` | 2026-08-26 | 建立多层超时、字符熔断与 `output_limit` 边界，防止无限生成 |
| [ADR-008](decisions/0008-speaker-diarization-and-voiceprint-clustering.md) | 会议模式多说话人精准识别与声纹聚类 | 🔵 `accepted` | 2026-08-27 | 迟滞双门限、参会人数先验、时序平滑与 CAM++ 声纹质心及 AHC 聚类 |
| [ADR-009](decisions/0009-shared-local-inference-platform.md) | LM Studio 原生协议与本地推理准入采用跨业务公共层 | 🔵 `accepted` | 2026-08-27 | 统一 SSE 语义、配置所有权、优先级调度和 Inner OS 边界 |
| [ADR-010](decisions/0010-physical-output-audio-capture.md) | 本地物理输出音频采用设备绑定的 Core Audio Tap 原生采集 | 🔵 `accepted` | 2026-08-31 | 原生 Helper、设备级作用域、双源混音与单 PCM 推理所有者 |

### 6. 评测基准与实验资产 (`docs/benchmarks/asr/`)

| 文档路径 | 状态 | 类型 | 说明 |
|---|---|---|---|
| [Stage 0 v1.2 可行性门禁报告](benchmarks/asr/stage0-v12-20260825/report.md) | 🟡 `completed` | `benchmark_report` | Qwen3-ASR、SenseVoiceSmall 与 Fun-ASR-Nano 的 Stage 0 可行性门禁 |
| [Public Operational Proxy v2 盲测评测报告](benchmarks/asr/public-operational-proxy-v2-20260825/report.md) | 🟡 `completed` | `benchmark_report` | 基于 AISHELL-4 与 ASCEND 的序贯盲测实验报告与 futility 结论 |
| [ASR 语料库 v1.2/v1.3 资产清单与源溯源](benchmarks/asr/corpus-v12-20260825/source-inventory.md) | 🟡 `completed` | `benchmark_report` | 评测语料清单、切片来源、许可协议与 SHA-256 哈希台账 |
| [Public Proxy v1 预评测报告](benchmarks/asr/public-proxy-v1-20260825/report.md) | 📦 `archived` | `benchmark_report` | 公共代理语料初测与流程校验报告（历史归档） |
| [目标域预检协议与规范](benchmarks/asr/target-domain-preflight-v1/README.md) | 📦 `archived` | `benchmark_report` | 目标域 ASR 录音盲测预检协议与数据脱敏规范（历史归档） |

### 7. 历史计划与设计规格归档 (`docs/superpowers/`)

| 目录 | 数量 | 状态 | 说明 |
|---|---|---|---|
| [superpowers/plans/](superpowers/plans/) | 14 份执行计划 | 🟠 `draft` / 🟣 `implemented` | 当前研发计划与历史功能迭代任务清单；P0 见[音频源基础设施实施计划](superpowers/plans/2026-08-31-audio-source-foundation.md)，当前进入[P1 物理输出 Helper 实施计划](superpowers/plans/2026-08-31-physical-output-helper.md) |
| [superpowers/specs/](superpowers/specs/) | 12 份设计规格 | 🟠 `under_review` / 🟣 `implemented` | 当前评审规格与历史技术整改设计及验证标准；新增[本地物理输出设备音频采集设计](superpowers/specs/2026-08-31-physical-output-audio-capture-design.md) |

---

## 📋 文档撰写与元数据最佳实践规范

新增或修改文档时，请务必在文档第一行声明规范的 YAML Frontmatter：

```yaml
---
title: "文档中文标题"
description: "文档一句话核心功能与摘要说明"
status: active | draft | under_review | accepted | implemented | completed | template | archived
type: architecture | domain_solution | technical_spec | manual | guide | decision_record | benchmark_report | test_record | postmortem | template | execution_plan
category: architecture | meeting | interaction | subtitles | asr | tts | frontend | quality_assurance
version: "1.0.0"        # 语义化版本（若适用）
date: 2026-08-27        # 创建日期
last_updated: 2026-08-27# 最后维护日期
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-core"
tags:
  - voice-realtime
  - keyword1
scope:
  - "voice_realtime.module"
related_documents:
  - "docs/architecture/系统总体架构与详细设计方案.md"
contracts:              # 若涉及前后端或外部通信协议
  - "contracts/meeting-assistant/v1/"
---
```
