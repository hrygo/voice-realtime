---
title: "ADR-010：本地物理输出音频采用设备绑定的 Core Audio Tap 原生采集"
description: "确立通用物理输出采集、原生 Helper 隔离、双源混音和单 PCM 推理所有者边界"
status: accepted
type: decision_record
category: architecture
date: 2026-08-31
last_updated: 2026-08-31
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-core"
tags:
  - adr
  - core-audio
  - physical-output
  - audio-capture
  - meeting-assistant
scope:
  - "native/vr-audio-capture"
  - "voice_realtime.audio"
  - "voice_realtime.meeting"
  - "voice_realtime.ui"
  - "contracts/meeting-assistant"
related_documents:
  - "docs/superpowers/specs/2026-08-31-physical-output-audio-capture-design.md"
  - "docs/decisions/0005-server-side-runtime-workload-arbitration.md"
  - "docs/architecture/系统总体架构与详细设计方案.md"
---

# ADR-010：本地物理输出音频采用设备绑定的 Core Audio Tap 原生采集

## 状态

Accepted

本文接受的是架构决策；代码实现与产品上线状态以对应技术规格和后续实施计划为准。

## 日期

2026-08-31

## 背景

项目当前只有 PyAudio 麦克风输入。会议助手因此只能通过麦克风声学回采获得腾讯会议等本机应用的播放内容，质量受外放音量、环境噪声、回声和耳机使用方式影响，也无法稳定覆盖用户实际听到的全部远端会议声音。

目标不是适配某一家会议软件，而是采集一个明确本机输出端点上的数字程序混音，使任何路由到该端点且可被 macOS 暴露的应用音频都能进入字幕或会议助手。

该能力同时受到以下既有约束：

- Apple Silicon / macOS、本地离线优先；
- `RuntimeModeCoordinator` 继续保证单一重型 PCM 推理所有者；
- PostgreSQL 不保存音频；
- 浏览器只作为控制面；
- 权限拒绝、设备切换和来源失效不得静默处理。

## 决策

### 1. 采用设备绑定的 Core Audio Tap

在 macOS 14.2+ 上使用 Core Audio process tap 能力，创建绑定目标输出 `deviceUID` 的全局 Tap，并通过私有 Aggregate Device 启动 I/O。Tap 捕获该端点上由 Core Audio 暴露的程序混音，不改变用户的系统输出路由。

设备约束属于隐私边界。无法可靠绑定目标设备时必须拒绝采集，不得降级为捕获所有输出设备。

### 2. 原生能力由独立签名 Helper 承载

新增 `vr-audio-capture.app`：

- 使用稳定 Bundle ID、代码签名、`NSAudioCaptureUsageDescription` 和发布公证；
- 负责系统音频权限、设备枚举、Tap / Aggregate Device 生命周期、格式转换与属性监听；
- Core Audio 实时回调只写预分配 SPSC Ring，转换和 IPC 在工作线程执行；
- 通过用户私有 Unix Domain Socket 向 Python 输出 16 kHz mono s16le PCM；
- 与 Python 主进程隔离崩溃，并由主进程实施有界退避重启。

不由 Python 主进程直接绑定 Core Audio，以避免把 TCC 身份、原生 ABI、实时线程约束和系统级崩溃面带入业务运行时。

### 3. 引入通用音频源路由层

在 Python 音频域新增 `AudioFrame`、`AudioSource`、`MicrophoneSource`、`PhysicalOutputSource`、`AudioSourceRouter` 与 `DualSourceMixer`。

统一帧必须携带来源、角色、sequence、设备 generation 和 monotonic host time。既有 `AudioHub` 继续专用于麦克风，不扩展成同时承载物理输出的通用 Hub。

### 4. 会议支持单源与双源，仍保持一个推理 owner

产品提供：

- microphone-only；
- physical-output-only；
- dual：near-end 麦克风与 far-end 物理输出先对齐、补静音、混音和限幅，再向 WhisperLiveKit / ASR 提交一条 PCM 流。

因此，“单 PCM 所有者”继续约束重型推理链，而不是限制采集源只能有一个。初版双源推荐耳机；外放高质量路径后续以输出流作为 WebRTC AEC 的 far-end reference。

### 5. 采用两阶段采集事务和显式降级

权限、设备、Helper、Tap、格式和 IPC 必须在 meeting row 创建之前完成 prepare；业务记录创建后再 commit PCM。prepare 失败不产生中断会议记录。

运行中来源丢失必须记录 capture event 并在 UI 可见：dual 可以显式降为剩余来源，output-only 不得擅自开启麦克风。默认设备跟随采用两阶段重绑；锁定设备消失时不切换到其他设备。

### 6. 保持最小数据与权限边界

- PCM 只存在于实时回调缓冲、IPC 和有界内存队列，不写数据库、journal、日志或临时文件。
- 原始设备 UID 只保存在用户本机配置，不进入会议数据和遥测。
- Helper 仅在用户显式启用物理输出采集时请求系统权限并保持 Tap。
- 浏览器移除 `getUserMedia`，能量和状态统一由服务端提供。
- 默认排除本产品自身明确可识别的音频渲染进程，且不得因会议运行在浏览器中排除整个浏览器。

### 7. 原生 Tap 失败时不隐式安装虚拟驱动

BlackHole 等虚拟设备只作为显式、可撤销的后备方案，用于旧系统或经验证不兼容的设备。主程序不得自动安装驱动、创建 Multi-Output Device 或修改系统输出路由。

## 备选方案

### 方案 A：Python 直接调用 Core Audio

不采用。虽然可以减少一个进程，但会将 Objective-C / Swift 桥接、TCC 权限身份、实时 callback、签名与设备生命周期耦合到 Python 主进程，故障隔离和发布稳定性不足。

### 方案 B：ScreenCaptureKit 捕获系统音频

不作为主路径。它适合屏幕、窗口和内容捕获，授权与选择模型不能精确表达“只采集指定输出端点”，权限范围也大于纯音频设备采集需求。

### 方案 C：BlackHole 或其他虚拟音频驱动

不作为默认路径。它需要额外安装、重启或系统路由配置，用户必须理解 Multi-Output Device，且还存在升级兼容与许可证评估成本。保留为 P4 后备。

### 方案 D：只依赖麦克风声学回采

不满足目标。它无法在耳机场景获得远端声音，在外放场景又受到房间声学、回声和噪声影响，不能作为“本机输出端点数字音频”的可靠实现。

## 后果

### 正向后果

- 与会议软件解耦，同一实现覆盖腾讯会议、浏览器、媒体播放器及后续应用。
- 不改变用户听音路由，默认体验优于虚拟音频设备。
- 设备作用域、权限状态、丢帧和热切换都可观测、可验收。
- 双源会议输入复用现有单推理 owner、EOF 冲刷、PostgreSQL 和声纹链路。
- 原生崩溃、权限与业务持久化边界被隔离。

### 负向后果

- 仓库新增 Swift / Xcode 构建、签名、公证和跨语言 IPC 的维护成本。
- 输出采集特性最低要求 macOS 14.2；更低版本需要后备方案。
- 双源外放在 AEC 完成前可能产生远端语音重复转写。
- Core Audio 设备、蓝牙、HDMI 和系统版本组合必须进行真实设备矩阵与长稳测试。
- 会议 v2 契约、数据库约束和 UI 状态机需要同步迁移。

### 保留风险

- DRM 或受保护路径可能不提供可捕获 PCM，产品不能承诺绕过系统限制。
- 全零 PCM 既可能是合法静音，也可能是系统 Tap 异常，不能只靠振幅自动修复。
- 某些输出端点可能无法可靠实现 device-scoped tap；此时应 fail closed，并由显式后备方案覆盖。
- 当前 macOS 新版本仍有社区报告的长会话 Tap 异常，需要以至少 2 小时持续运行、完整重建动作和版本矩阵控制风险。

## 实施约束

完整协议、状态机、路线图和验收门禁见[本地物理输出设备音频采集设计](../superpowers/specs/2026-08-31-physical-output-audio-capture-design.md)。任何实施方案不得绕过以下门禁：

1. 未通过设备作用域负向测试，不得发布 physical-output capture。
2. 未完成 preflight 前不得创建 meeting row。
3. 未证明 PCM 无落盘，不得接入生产会议模式。
4. 未保证 single inference owner，不得并行启动双 ASR 代替 Mixer。
5. 未移除浏览器 `getUserMedia`，不得宣称统一音频采集所有权完成。
