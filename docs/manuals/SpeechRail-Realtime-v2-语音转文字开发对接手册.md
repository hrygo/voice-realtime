---
title: "SpeechRail Realtime v2 语音转文字开发对接手册"
description: "指导客户端通过 SpeechRail Realtime v2 接入本地 ASR 与可选 diarization，并对接 REST 文件转写能力"
status: active
type: manual
category: asr
version: "v2.0.0"
date: 2026-09-01
last_updated: 2026-09-01
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-subtitles"
tags:
  - speechrail
  - realtime-v2
  - websocket
  - rest-api
  - streaming-transcription
  - developer-guide
scope:
  - "voice_realtime.subtitles"
  - "voice_realtime.asr"
related_documents:
  - "docs/architecture/系统总体架构与详细设计方案.md"
  - "docs/architecture/实时语音交互与字幕-方案与最佳实践.md"
  - "SpeechRail repository: contracts/realtime-v2.md (external sibling project)"
---

# SpeechRail Realtime v2 语音转文字开发对接手册

> 本手册是当前 ASR 对接基线。原 `Qwen3-ASR-实时语音转文字开发对接手册.md` 文件名保留为兼容入口，
> 但其中的旧直连地址和二进制 WebSocket 协议已废弃。

## 1. 服务基础信息

| 配置项 | 当前约定 |
|---|---|
| WebSocket | `ws://127.0.0.1:8201/v2/realtime` |
| 健康检查 | `GET http://127.0.0.1:8201/health`；就绪检查使用 `/readyz` |
| ASR model | `speechrail/qwen3-asr-1.7b` |
| 音频 | 16kHz、mono、signed 16-bit PCM（`s16le`） |
| 鉴权 | 可选 `Authorization: Bearer <key>`；key 不得放入 URL |
| 生命周期 | SpeechRail 独立管理模型、profile、worker 与健康状态；客户端只消费协议 |

SpeechRail 当前契约仍需通过实际部署完成后端 worker smoke/e2e 验收；服务返回
`backend_not_ready` 时，客户端应报告依赖未就绪，不得静默切换到本地模型。

## 2. Realtime v2 公共协议

连接后，客户端必须首先发送一次 `session.update`，选择 `transcription` 会话。服务端先返回
`session.created`，之后才能发送音频。每个服务端事件都带有 `type`、`event_id`、`session_id`、
`request_id` 和单调递增的 `sequence`；客户端应校验同一 session 的顺序与身份。

### 2.1 创建转写会话

```json
{
  "type": "session.update",
  "session": {
    "type": "transcription",
    "model": "speechrail/qwen3-asr-1.7b",
    "language": "zh",
    "audio_format": {
      "type": "audio/pcm",
      "rate": 16000,
      "channels": 1,
      "sample_width": 2
    },
    "endpointing": {"mode": "manual"},
    "diarization": {
      "enabled": true,
      "finalize": true,
      "speaker_count_hint": 4,
      "group_id": "application-owned-opaque-group-id"
    }
  }
}
```

`diarization` 为会议等多说话人场景的可选配置。`group_id` 必须是客户端生成的 16–128 字符不透明
标识；它不是会议 ID、账号或真实身份。未配置相应 SpeechRail profile 时，服务应返回
`diarization_not_available`，不得伪造 speaker label。

### 2.2 追加 PCM 与读取事件

实时音频不是 WebSocket 二进制帧，而是 JSON 中的 Base64 字段：

```json
{
  "type": "input_audio_buffer.append",
  "audio": "<base64-s16le-pcm>"
}
```

主要服务端事件如下：

| 事件 | 客户端处理 |
|---|---|
| `transcription.delta` | 同一 `item_id` 的可替换快照；只展示最新 `revision`，不持久化为确认文本 |
| `transcription.completed` | 不可变的已确认片段；包含 `start_ms`、`end_ms`、`text`，启用分人时含 `speaker`/`speakers` |
| `transcription.diarization.completed` | commit 后的一次匿名 label mapping；必须原子应用，不得当作真实身份 |
| `input_audio_buffer.ack` | 可选背压诊断；`accepted_bytes` 不用于断线续传 |
| `error` | 根据 `error.code` 与 `retryable` 决定报告或重试 |

所有时间戳相对当前 SpeechRail session 首个已接受 PCM 字节。应用如果有自己的 source epoch 或
窗口偏移，必须在 adapter 层转换；不能把不同 WebSocket 连接的 timestamp 直接混用。

### 2.3 flush、commit、cancel

- `input_audio_buffer.flush`：强制确认当前非空 item，session 仍可继续追加。
- `input_audio_buffer.commit`：停止接收新音频，冲刷剩余结果，正常终态为 `session.completed`。
- `session.cancel`：丢弃未确认输入和 partial，终态为 `session.cancelled`。
- 断线不保证 terminal event。Realtime v2 不提供透明恢复；必须新建连接/session，并由应用记录
  source epoch 与可能的 transcription gap。

会议结束必须等待 `transcription.diarization.completed`（如启用分人）及 `session.completed`，再封存
confirmed 转录和 speaker remap；超时应标记 `finalization_timeout`，不能假设空 PCM 二进制包等同 EOF。

## 3. Python 对接骨架

以下示例展示协议顺序；生产代码还应加入超时、sequence 校验、背压和取消回收：

```python
import asyncio
import base64
import json

import websockets

WS_URL = "ws://127.0.0.1:8201/v2/realtime"


async def transcribe(pcm_chunks: list[bytes], api_key: str | None = None) -> None:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    async with websockets.connect(WS_URL, additional_headers=headers) as ws:
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "transcription",
                "model": "speechrail/qwen3-asr-1.7b",
                "language": "zh",
                "audio_format": {
                    "type": "audio/pcm",
                    "rate": 16000,
                    "channels": 1,
                    "sample_width": 2,
                },
                "endpointing": {"mode": "manual"},
            },
        }))
        created = json.loads(await ws.recv())
        if created["type"] != "session.created":
            raise RuntimeError(created)

        for chunk in pcm_chunks:
            if not chunk or len(chunk) % 2:
                raise ValueError("PCM must be non-empty int16")
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode("ascii"),
            }))

        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        async for raw in ws:
            event = json.loads(raw)
            if event["type"] == "transcription.completed":
                print(event["segments"])
            elif event["type"] == "error":
                raise RuntimeError(event["error"])
            elif event["type"] == "session.completed":
                break


if __name__ == "__main__":
    asyncio.run(transcribe([]))
```

`voice-realtime` 内部使用 `SpeechRailV2Transport` 统一校验 envelope、session、request 和 sequence；
优先复用对应 adapter，而不是在业务模块中重复实现协议解析。

## 4. REST 文件转写（非实时）

录音文件整段转写可使用 SpeechRail 的 REST 接口（具体 multipart 字段以 SpeechRail 当前 OpenAPI 为准）：

```bash
curl --fail --silent http://127.0.0.1:8201/v1/audio/transcriptions \
  -F "file=@meeting_record.wav" \
  -F "model=speechrail/qwen3-asr-1.7b" \
  -F "language=zh" \
  -F "response_format=json"
```

文件转写不是 `voice-realtime` 当前字幕/会议实时主链路；实时场景必须使用 `/v2/realtime`。

## 5. 排查清单

1. `backend_not_ready`：检查 SpeechRail worker、model snapshot 和 profile 就绪状态；不要联网隐式下载。
2. `invalid_event_order` / `sequence` 错误：确认首个客户端事件是 `session.update`，并且只消费当前 session 的递增事件。
3. 没有文字：确认是 JSON + Base64 PCM、16kHz mono int16，且在结束时发送 `commit`。
4. 没有 speaker：确认请求了 diarization、`group_id` 合法且 SpeechRail 已配置对应 profile。
5. 断线丢字：这是不可恢复 session；为新连接创建新 source epoch，并在应用层记录 gap/对账边界。
