# Audio Capture IPC v1

该契约定义 `vr-audio-capture.app` 与本机 Python 进程之间的唯一 wire protocol。传输仅允许用户私有 Unix Domain Socket；所有多字节整数均为 big-endian。

## 公共前缀

每条消息以 16 字节前缀开始：

| Offset | Size | Field | v1 value |
|---:|---:|---|---|
| 0 | 4 | magic | ASCII `VRAC` |
| 4 | 2 | header length | JSON `16`；PCM `84` |
| 6 | 1 | protocol major | `1` |
| 7 | 1 | protocol minor | `0` |
| 8 | 1 | message type | JSON `1`；PCM `2` |
| 9 | 1 | prefix flags | `0` |
| 10 | 2 | reserved | `0` |
| 12 | 4 | body length | JSON UTF-8 或 PCM payload 长度 |

未知 major、message type、prefix flag 或 reserved 值必须拒绝。相同 major 的后续 minor 可以增加 header 字段；旧接收方按 `header_length` 跳过未知扩展，不得猜测 body。

## JSON 控制帧

JSON frame 的 `header_length=16`，body 上限 65,536 bytes，顶层必须是 object，并符合 [`control-message.schema.json`](control-message.schema.json)。`request_id` 用于请求/响应关联，最大 64 字符。错误只暴露稳定 `code`、安全 `message` 和 `retryable`，不得包含 token、设备 UID、路径、堆栈或 PCM。

握手顺序为 `hello → hello_ack`。握手成功后允许 `list_devices`、`prepare_capture`、`commit_capture`、`abort_capture`、`stop_capture`。Helper 可异步发送 `event` 和 `health`。

## PCM 帧

PCM frame 的固定 header 为 84 字节。在公共前缀后依次为：

| Size | Field |
|---:|---|
| 16 | capture UUID bytes |
| 16 | source UUID bytes |
| 4 | device generation |
| 8 | sequence |
| 8 | host time ns |
| 4 | sample rate (`16000`) |
| 2 | samples per channel (`512`) |
| 1 | channels (`1`) |
| 1 | sample width (`2`) |
| 4 | frame flags |
| 4 | payload length (`1024`) |

`body_length`、`payload_length` 和实际 body 长度必须一致。frame flags 与 Python `AudioFrameFlag` 对齐：`1=discontinuity`、`2=silence_fill`、`4=end_of_stream`。v1 的非 EOF PCM 固定为 mono signed 16-bit little-endian、512 samples；空 EOF 由后续 minor 扩展，v1 不发送。

## 访问控制与资源上限

- Socket 父目录必须由当前有效 UID 拥有且权限为 `0700`；socket 权限为 `0600`。
- Helper 使用 `getpeereid` 校验客户端有效 UID，并使用常量时间比较校验 256-bit capture token。
- 单连接、单 capture；第二客户端或 capture ID 冲突返回稳定错误并关闭连接。
- JSON 最大 65,536 bytes，header 最大 256 bytes，单帧最大 1 MiB；读取端不得依据未校验长度分配。
- PCM、token 和原始设备 UID 不写日志、fixture、数据库或临时文件。

`fixtures/pcm-header.hex` 只包含合成 metadata header；测试在内存中附加全零样本，不存储音频 payload。
