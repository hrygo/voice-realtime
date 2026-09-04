---
title: "SpeechRail 流式说话人分离对接手册"
status: active
audience: "sona 工程团队（会议实时字幕与纪要消费方）"
version: "1.0.0"
date: 2026-09-04
---

# 🎙️ SpeechRail 流式说话人分离对接手册

> 本手册说明 SpeechRail `/v1/realtime` 流式说话人分离从「永不生效」到「端到端可用」
> 的变更（SpeechRail ADR-0010，版本 1.6.6 起），以及 sona 侧消费方式。
> SpeechRail 侧协议事实以 [SpeechRail `contracts/realtime-openai.md`](../../../SpeechRail/contracts/realtime-openai.md)
> 为准；本手册对照 sona 现有消费代码（`src/sona/speechrail/`）编写，供 sona 团队验收与联调。

---

## 1. 背景：发生了什么变更

修复前 SpeechRail 流式路径的 `completed` 事件**硬编码空 segments**，导致
`conversation.item.input_audio_transcription.segment` 事件（含 `speaker`）**从不下发**，
sona 会议侧表现为「说话人恒为 `speaker:0`、历史被最新句覆盖」。

SpeechRail 1.6.6（ADR-0010）修复后：

- worker 在 commit 时对已累积音频做一次**词级强制对齐**，`completed` 携带真实
  `segments`；
- WS 层按 segment 粒度下发 `conversation.item.input_audio_transcription.segment`，
  每项含匿名 `speaker`；
- **因此 sona 无需再自行缓冲 PCM 做非流式分人回流**——实时分人与最终纪要分人
  由同一条流式链路提供。

## 2. SpeechRail 侧事实（协议面）

### 2.1 启用方式

分人能力按会话启用，两种方式（`session.update` 一处生效）：

| 方式 | 配置 |
|---|---|
| 模型别名 | `session.update.session.model = "gpt-4o-transcribe-diarize"` |
| 显式配置 | `session.update.session.diarization`（需 SpeechRail diarization profile 就绪） |

sona 现有 `SpeechRailStreamingTranscriber.connect()` 已按 `purpose == "meeting"` 通过
`diarization=True` + `speaker_count_hint` + `diarization_group_id` 启用——**无需改动**。

### 2.2 事件顺序（commit 后）

```text
input_audio_buffer.committed
conversation.item.created
conversation.item.input_audio_transcription.segment   # 0..N 条（启用分人且对齐成功时）
conversation.item.input_audio_transcription.completed  # 终态（streaming 全文）
```

### 2.3 `.segment` 事件字段（与 sona 解码器逐字段对应）

```json
{
  "type": "conversation.item.input_audio_transcription.segment",
  "item_id": "item_sess_abc_input",
  "content_index": 0,
  "id": 0,
  "text": "这个方案我们今天定下来",
  "speaker": "spk_01",
  "start": 0.0,
  "end": 3.1
}
```

sona `transcription_events.py` 解码器约束（SpeechRail 发射格式**天然满足**，无需适配）：

| 字段 | 值/格式 | sona 解码器校验（`_decode_segment` / `_decode_speaker`） |
|---|---|---|
| `text` | 非空字符串（轻量 ITN 规整） | `isinstance(str)` 且 `strip()` 非空 |
| `start` / `end` | **秒**（int/float，`end >= start >= 0`） | 非 bool 数字；解码后 `round(start*1000)` → `start_ms` |
| `speaker` | 匿名 label `spk_01`/`spk_02`/… | 可选；存在则须 `str` 且以 `spk_` 开头、长度 ≤64 |
| `id` | 当前为整数索引（官方为字符串 `seg_0001`） | 未校验（忽略） |
| `item_id` / `content_index` | 字符串 / 0 | 未校验（忽略） |

> ⚠️ **秒制提醒**：sona 解码器按 **秒** 读取 `start`/`end` 并自行 ×1000。若沿用旧的
> 毫秒假设会读到错误边界。当前 sona 代码已正确换算，勿在适配层再乘一次。

## 3. sona 侧对接现状（对应代码）

| sona 组件 | 职责 | 与本次变更的关系 |
|---|---|---|
| `speechrail/transcription_events.py` | `decode_transcription_event` → `TranscriptionSegment`/`TranscriptionCompleted` 等，含 speaker/时间戳协议校验 | 无需改动；`SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR` 校验已按 `spk_*` 格式对齐 |
| `speechrail/transcriber.py` | `SpeechRailStreamingTranscriber`：消费流式事件，把匿名 `spk_*` 重写进 `group:{id}` 命名空间 | 无需改动；`completed` 无 segment 时已有兜底单 segment（保留本轮转写） |
| `meeting/diarization_overlay`（批路径） | 批量转写的分人归属回流 | 与流式链路并存；流式可用后可作为离线兜底 |

## 4. 消费模式

### 4.1 会议页面实时分人

沿用现有 `SpeechRailStreamingTranscriber.events()` 流：
`TranscriptionSegment`（含 `speaker`）→ `ASRSegment` → 渲染匿名发言人
（`Speaker 1`/`Speaker 2`，可重命名）。**链路打通后即刻生效，无需额外改代码。**

### 4.2 最终纪要分人

同一链路按 commit 累积段事件即为带分人的完整转写。SpeechRail 侧承诺：
**sona 无需再自行缓冲 PCM 做非流式分人回流**。

### 4.3 文本一致性

- `completed.transcript` 为流式解码全文；
- 各 `.segment.text` 来自 commit 时的**独立对齐解码**，与 `transcript` 可能有轻微文本漂移
  （同一音频的两种解码路径）。

**建议**：启用分人时以 `.segment` 渲染；`transcript` 仅作全文/兜底索引，勿按字级强对齐。

## 5. 降级与失败语义（fail-closed）

- commit 时对齐失败 / 未产出分段 → **不发送 `.segment` 事件**，SpeechRail **不伪造
  单一 speaker**；
- sona 现有兜底已覆盖：`completed` 无 segment 时合成单 segment 保留本轮转写
  （`transcriber.py` `_synthesized_segment`，`text` 之外不含 speaker）；
- 分人会话 commit 因对齐多一次重解码，延迟略高于非分人（见 §7），UI 以提交中状态覆盖。

## 6. 已知限制与风险（上线前知悉）

| 项 | 说明 |
|---|---|
| commit 延迟增量 | 启用分人时 commit 增加一次批量对齐重解码（估算 +0.15~0.3s/次）；以 SpeechRail 侧基准实测为准 |
| 段文本漂移 | 段文本与 `completed.transcript` 可能轻微不一致（见 §4.3） |
| label 会话性 | `spk_N` 会话内稳定，断线失效，不跨会话保真；sona 用 `group:{id}` 命名空间承载映射，重连须重建 |
| 真实分人精度 | DER/JER、双人交替、重叠语音效果需 SpeechRail 真实模型基准 + 双音色 smoke 验收确认 |
| 能力声明 | sona `transcriber.py` 现声明 `supports_segment_timestamps=False`，但解码器已消费段时间戳——请 sona 团队核实该声明是否需更新为 `True` |

## 7. 验收清单（sona 对接完成标准）

- [ ] 双音色（`warm` / `bright`）TTS 交替说话，两端收到**不同 `speaker` label**（`spk_01`/`spk_02`）；
- [ ] 单说话人连续说，同一 commit 内 `speaker` 稳定不跳变；
- [ ] 页面实时标签与最终纪要分人一致；
- [ ] 分人会话必见 `.segment`（不再出现「说话人恒为 speaker:0」）；
- [ ] 非分人会话（纯字幕/交互）行为与旧版一致（无 `.segment`、无回归）；
- [ ] 断线重连后新会话 label 从头编号，`group:{id}` 映射重建正常。

## 8. 参考

- SpeechRail 协议事实：[`contracts/realtime-openai.md`](../../../SpeechRail/contracts/realtime-openai.md)
- SpeechRail 决策记录：[`docs/decisions/0010-streaming-diarization-fix.md`](../../../SpeechRail/docs/decisions/0010-streaming-diarization-fix.md)
- sona 现有对接实现：`src/sona/speechrail/transcriber.py`、`transcription_events.py`
- sona 事件解码测试：`tests/asr/test_speechrail_events.py`