# Qwen3-ASR 实时语音转文字开发对接手册

> 本手册用于指导外部客户端接入 **Qwen3-ASR 1.7B 语音识别服务**，实现**实时流式音频识别（低延迟字字上屏）**与**录音文件转文字**。

---

## 📌 1. 服务基础信息

| 配置项 | 说明 |
|---|---|
| **服务主机 (Host)** | `172.18.24.62`（局域网） / `localhost`（本机） |
| **服务端口 (Port)** | `8001` |
| **底层模型** | `Qwen3-ASR-1.7B`（阿里通义开源 1.7B 流式端到端语音识别模型） |
| **支持语言** | 中文（`Chinese` / `zh`）、英文（`en`）、多语言自动检测（`auto`） |
| **鉴权方式** | 局域网内免 API Key 鉴权 |

---

## 🎧 2. 音频数据格式规范（必须严格对齐）

对于**实时 WebSocket 流式接口**，客户端发送的音频必须满足以下格式（标准 PCM）：

| 参数 | 要求 | 说明 |
|---|---|---|
| **音频编码** | `PCM 16-bit Signed` (Little-Endian / `s16le`) | 裸 PCM 字节流，无需 WAV 文件头 |
| **采样率** | `16000 Hz` (16kHz) | 若麦克风为 44.1k/48k，需客户端先降采样至 16k |
| **声道数** | `1` (单声道 / Mono) | 双声道需先转为单声道 |
| **分块大小** | 推荐每包 `100ms ~ 500ms` | 100ms 对应 `3,200` 字节；200ms 对应 `6,400` 字节 |

> 💡 **注**：如果是 **REST API 文件上传接口**，支持直接上传常规 `.wav` / `.mp3` / `.m4a` / `.ogg` 音频文件。

---

## 🚀 3. 核心接口接入指南

---

### 接口一：实时流式识别接口（WebSocket API）

适用于麦克风实时拾音、直播字幕、会议实时听写等低延迟流式场景。

#### 1. 连接地址
```text
ws://172.18.24.62:8001/asr?language=Chinese
```
- **Query 参数**：
  - `language`（可选）：指定语言，如 `Chinese`（默认）、`en`、`auto`；
  - `mode`（可选）：`full`（默认，每次推送当前窗口完整文本）、`diff`（仅推送增量更新）。

---

#### 2. 通信流程与协议

```text
客户端                                服务端
  │                                      │
  ├────── 1. 发起 WebSocket 连接 ────────►│
  │◄───── 2. 握手消息 {"type":"config"} ─┤
  │                                      │
  ├────── 3. 连续发送 PCM 二进制音频块 ──►│ (Binary Frame)
  │◄───── 4. 实时推送识别 JSON 结果 ─────┤ (Text Frame)
  │                                      │
  ├────── 5. 发送空字节 b"" (EOF) ────────►│ (发送完毕时通知冲刷)
  │◄───── 6. 返回 {"type":"ready_to_stop"}┤ (转录封存就绪)
  │                                      │
  ├────── 7. 主动关闭连接 ───────────────►│
```

---

#### 3. 服务端返回 JSON 格式定义

**识别中持续返回的增量消息**：
```json
{
  "status": "active_transcription",
  "lines": [
    {
      "speaker": 1,
      "text": "你好，这是一段实时语音转文字测试。",
      "start": "0:00:00",
      "end": "0:00:03"
    },
    {
      "speaker": 1,
      "text": "欢迎使用 Qwen3 语音模型。",
      "start": "0:00:03",
      "end": "0:00:05"
    }
  ]
}
```

**字段说明**：
- `lines`：当前时间窗口内已识别并对齐的句子列表。
- `lines[i].speaker`：说话人 ID（默认 `1`，若启用说话人分离则为 `1`、`2` 等）。
- `lines[i].text`：识别出的文字内容（含标点符号）。
- `lines[i].start` / `lines[i].end`：起始与结束时间戳（格式 `H:MM:SS`）。

---

#### 4. 完整可运行客户端代码示例

##### 🐍 Python 流式示例（直接运行）

```python
import asyncio
import json
import wave
import websockets

# 服务端地址
WS_URL = "ws://172.18.24.62:8001/asr?language=Chinese"


async def stream_audio_file(file_path: str):
    """读取本地 16k 单声道 wav 文件并流式发送到 ASR 服务。"""
    with wave.open(file_path, "rb") as wf:
        sample_rate = wf.getframerate()
        channels = wf.getnchannels()
        assert sample_rate == 16000, f"音频必须为 16kHz (当前: {sample_rate})"
        assert channels == 1, f"音频必须为单声道 (当前: {channels})"

        async with websockets.connect(WS_URL) as ws:
            # 1. 接收握手消息
            handshake = await ws.recv()
            print(">> 服务已连接:", handshake)

            # 2. 启动异步后台接收任务
            async def receive_handler():
                try:
                    async for message in ws:
                        data = json.loads(message)
                        # 结束信号
                        if data.get("type") == "ready_to_stop":
                            print("\n>> 识别完全结束 (ready_to_stop)")
                            break
                        # 解析并输出转录文本
                        if "lines" in data:
                            for line in data["lines"]:
                                speaker = line.get("speaker", 1)
                                text = line.get("text", "")
                                start = line.get("start", "")
                                end = line.get("end", "")
                                print(f"[{start} ~ {end}] Speaker {speaker}: {text}")
                except websockets.exceptions.ConnectionClosed:
                    pass

            recv_task = asyncio.create_task(receive_handler())

            # 3. 模拟实时流，每 100ms 发送一块数据 (1600 个采样点 = 3200 字节)
            chunk_samples = 1600
            while True:
                pcm_data = wf.readframes(chunk_samples)
                if not pcm_data:
                    break
                await ws.send(pcm_data)
                await asyncio.sleep(0.09)  # 控制发送间隔在 100ms 左右

            # 4. 发送空字节 (EOF)，通知服务端音频已结束
            await ws.send(b"")

            # 等待服务端吐完最后一段字并退出
            await recv_task


if __name__ == "__main__":
    # 安装依赖: pip install websockets
    asyncio.run(stream_audio_file("test_16k_mono.wav"))
```

---

##### 🌐 JavaScript / 浏览器 / Node.js 示例

```javascript
const ws = new WebSocket("ws://172.18.24.62:8001/asr?language=Chinese");
ws.binaryType = "arraybuffer";

ws.onopen = () => {
  console.log("WebSocket 连接成功");
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  
  if (data.type === "config") {
    console.log("握手就绪:", data);
    return;
  }
  
  if (data.type === "ready_to_stop") {
    console.log("识别完成");
    return;
  }
  
  // 打印最新转写结果
  if (data.lines) {
    data.lines.forEach((line) => {
      console.log(`[${line.start}-${line.end}] 说话人${line.speaker}: ${line.text}`);
    });
  }
};

// 1. 发送实时音频 PCM 片段 (ArrayBuffer / Uint8Array)
function sendAudioChunk(int16ArrayBuffer) {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(int16ArrayBuffer);
  }
}

// 2. 录音结束时发送空包冲刷
function stopStreaming() {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(new Uint8Array(0)); // EOF
  }
}
```

---

### 接口二：音频文件转录接口（REST API / OpenAI 兼容）

如果客户端需求是**录完一段音频后整段上传识别**，使用此 HTTP 接口最简单。

#### 1. 接口定义
- **URL**：`POST http://172.18.24.62:8001/v1/audio/transcriptions`
- **Content-Type**：`multipart/form-data`
- **请求字段**：
  - `file`（必填）：音频文件（支持 `.wav`, `.mp3`, `.m4a` 等）；
  - `model`（可选）：填 `Qwen3-ASR-1.7B`；
  - `language`（可选）：`Chinese` / `zh` / `en`；
  - `response_format`（可选）：`json`（默认）、`text`、`verbose_json`。

---

#### 2. 调用示例

##### cURL
```bash
curl -X POST http://172.18.24.62:8001/v1/audio/transcriptions \
  -F "file=@meeting_record.wav" \
  -F "model=Qwen3-ASR-1.7B" \
  -F "language=Chinese" \
  -F "response_format=json"
```

##### 响应 JSON
```json
{
  "text": "这是音频文件转录出来的全部文本内容。",
  "usage": {
    "type": "duration",
    "seconds": 5
  }
}
```

---

##### 官方 OpenAI Python SDK 接入
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://172.18.24.62:8001/v1",
    api_key="none",  # 本地服务无需鉴权
)

with open("meeting_record.wav", "rb") as f:
    transcript = client.audio.transcriptions.create(
        model="Qwen3-ASR-1.7B",
        file=f,
        language="zh",
    )

print("识别结果:", transcript.text)
```

---

### 接口三：服务健康检查（探活）

- **URL**：`GET http://172.18.24.62:8001/health`
- **响应示例**：
  ```json
  {
    "status": "ok",
    "backend": "qwen3-streaming",
    "ready": true
  }
  ```

---

## ❓ 常见问题排查 (FAQ)

### 1. 为什么发送了音频但是没有任何文字返回？
- **检查采样率**：流式 WebSocket 严格要求 **16000Hz 单声道 16-bit PCM**。若传入了 44.1k/48k 音频，服务端无法正确解码特征，会导致静音或乱码；
- **检查分包大小**：不要一次性发送几兆数据，建议以 100ms ~ 300ms（3.2KB ~ 9.6KB）为一包连续发送；
- **检查音频末尾**：音频发送完毕后，务必发送一次空字节 `ws.send(b"")` 以触发模型冲刷最后的尾句。

### 2. 可以在浏览器网页中直接录音测试吗？
可以，局域网电脑直接打开浏览器访问：
👉 `http://172.18.24.62:8001/`  
*(注：现代浏览器在非 HTTPS 下调用麦克风有限制，若浏览器提示无法获取麦克风，请在 Chrome 地址栏访问 `chrome://flags/#unsafely-treat-insecure-origin-as-secure`，添加 `http://172.18.24.62:8001` 为信任源即可)*。
