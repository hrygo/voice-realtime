---
title: "ADR-008：会议模式多说话人精准识别与声纹聚类"
description: "采用迟滞双门限、参会人数先验、时序平滑滤波与 CAM++ 声纹质心池及 AHC 全局聚类，根除一人多号与声道抖动"
status: accepted
type: decision_record
category: meeting
version: "v1.0.0"
date: 2026-08-27
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-meeting"
tags:
  - adr
  - speaker-diarization
  - voiceprint
  - cam++
  - ahc-clustering
  - sortformer
scope:
  - "voice_realtime.meeting"
  - "voice_realtime.asr"
  - "ui"
related_documents:
  - "docs/solutions/会议模式多说话人精准识别与声纹聚类技术方案.md"
  - "docs/architecture/系统总体架构与详细设计方案.md"
contracts:
  - "contracts/meeting-assistant/v1/"
---

# ADR-008：会议模式多说话人精准识别与声纹聚类

## 状态

Accepted

## 日期

2026-08-27

## 背景

在会议录制与实时转录场景中，原系统仅依赖前端流式 Sortformer 模型进行帧级在线说话人分离（Diarization）。在实测与用户反馈中暴露出显著的**“一人多号”**与**说话人身份过度分裂**问题：

1. **静音与呼吸声通道漂移**：Sortformer 输出帧级说话人概率，原实现简单采用 `argmax`。在发言人微顿、吸气或背景底噪帧中，由于各通道置信度极低，`argmax` 会在随机通道间快速振荡，造成单人连续发言被切分成多个说话人。
2. **缺乏参会人数容量先验**：Sortformer 固定分配 4 通道。在 1 人演讲或 2 人 1v1 访谈场景下，模型仍可能误分配到 Speaker 3 或 4，造成无意义的说话人膨胀。
3. **断线重连身份割裂**：网络闪断或 ASR 重启产生新的 `source_epoch` 时，旧说话人标记（如 `epoch0:s0`）与新说话人标记（如 `epoch1:s0`）被视为完全独立的物理身份，自定义姓名无法跨 Epoch 继承。
4. **缺乏全局声学特征比对**：流式帧级判定缺乏长时声纹（Voiceprint）约束，无法在整场会议维度将同一人的多次发言进行闭环合并。

同时，系统必须恪守**隐私底线**：PostgreSQL 是会议文本唯一事实源，**严禁将原始会议音频写入持久化磁盘**。

## 决策

采用“流式实时平滑（第一遍） + 声学质心在线跟踪 + 会后 AHC 全局聚类二次修正（第二遍）”的多层防御体系：

### 1. Sortformer 迟滞双门限状态机（L1）
在 `SortformerDiarizationOnline` 中引入迟滞双门限与静音保护：
- `onset_threshold = 0.50`：新声道置信度必须达到 0.50 且显著高于当前声道才触发通道切换；
- `offset_threshold = 0.35`：当前活跃声道置信度维持在 0.35 以上时保持不切；
- `silence_threshold = 0.25`：全通道置信度低于 0.25（静音/弱能量）时锁定上一有效声道，严禁随机漂移。

### 2. 参会人数容量先验 `max_speakers` 全链路贯通（L2）
支持在会议启动时指定 `max_speakers`（1~4 人），并沿“前端 UI ➔ 控制协议 ➔ UIRuntime ➔ MeetingSession ➔ SubtitleProxy ➔ WLK ➔ Sortformer”全链路动态下发：
- 严格限制 Sortformer 仅在第 $1 \sim M$ 个到达声道中激活；
- 单人录音（`max_speakers=1`）强制单声道，彻底阻断多说话人产生；双人访谈（`max_speakers=2`）上限锁定为 2 人。

### 3. 增强型时序平滑器与跨 Epoch 继承（L3/L4）
- 在 `DiarizationSmoother` 中集成短片段滤波（$\le 350\text{ms}$ 杂音过滤）、$A-B-A$（$\le 500\text{ms}$）与 $A-B-B-A$（$\le 600\text{ms}$）短闪烁纠偏；
- 同一说话人相邻段落合并自然间隙扩至 $1000\text{ms}$；
- 同一会议内跨 Epoch 重连时，新 `speaker_key` 自动继承历史已命名的 `display_name`，且 `rename_speaker` 原子同步全量跨 Epoch 记录。

### 4. CAM++ 声纹特征嵌入与在线质心池（L5）
- 集成阿里 3D-Speaker CAM++ 192 维 ONNX 模型（~27MB，纯 CPU 单段提取仅需 ~12ms）；
- **内存生命周期隔离**：使用 `AudioMemoryBuffer` 在内存中维护滚动 PCM 缓冲，**会议进行中与结束后均不向磁盘写任何音频文件**，会后随会话销毁释放；
- `CentroidPool` 动态维护会话内各声道的单位化质心，当新声道与已有质心余弦相似度 $\ge 0.75$ 时自动映射归并。

### 5. 会后全局 AHC 聚类二次修正与原子重映射（L6）
- 会议录制结束（EOF 冲刷完成）后、触发 AI 纪要前，`AHCClusterer` 对全量确认段落执行层次凝聚聚类（余弦距离阈值 $\le 0.35$，且受 `max_speakers` 强约束）；
- `PostgresMeetingRepository.apply_speaker_remapping` 在一个数据库事务内原子更新 `transcript_segments`、合并 `meeting_speakers` 并继承自定义名称。

### 6. 1:N 说话人声纹库注册与自动命名（L7）
- `VoiceprintProfileMatcher` 支持注册已知说话人声纹特征；
- 质心池与已知声纹库进行余弦比对（阈值 $\ge 0.72$），命中时自动赋予对应真实姓名，无需人工手动重命名。

## 影响

1. **识别精度与体验**：
   - 彻底消除了静音吸气引起的声道频繁跳变；
   - 单人/双人会议不再出现误切第 3、4 说话人的现象；
   - 会后 AI 纪要接收到的转录文本具备高度连贯、准确的说话人归属。
2. **性能与资源**：
   - CAM++ ONNX 仅在 CPU 上轻量运行（~12ms / 段），零显存占用，完全不影响实时语音 ASR 与 LLM 交互；
   - 纯内存音频切片在会议结束后立即释放，完全符合隐私与存储隔离规范。
3. **架构与向后兼容**：
   - 若模型未就绪或禁用声纹聚类，系统无缝回退至纯时序平滑模式；
   - 控制协议 `start_meeting` 的 `max_speakers` 字段为可选参数，缺省默认兼容 4 人标准会议。
