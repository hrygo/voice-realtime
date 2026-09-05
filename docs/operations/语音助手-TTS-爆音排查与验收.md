# 语音助手 TTS 爆音排查与验收手册

> 本手册用于排查与验收 Sona 语音交互长播报场景下的 CoreAudio overload 与爆音问题。

---

## 1. 背景与根因

在语音助手长播报（尤其是并发本地推理）场景下，如果直接将 SpeechRail 的 24 kHz PCM 交付给 PyAudio 且未指定缓冲帧大小，macOS `coreaudiod` 会在重采样与设备调度中出现安全时限超期（`HALS_OverloadMessage: Overload`），导致声音撕裂、爆音与咔哒声。

Sona 通过接入稳定本机输出适配器（`StableLocalAudioTransport`），实现：
1. 自动探测输出设备原生采样率（内置扬声器通常为 48,000 Hz）；
2. 显式设定 40 ms 输出缓冲（48 kHz 下对应 1,920 frames）；
3. 由 Pipecat 在流经输出前完成 24 kHz → 原生采样率的高质量重采样。

---

## 2. 诊断与排查命令

### 2.1 macOS CoreAudio Overload 日志计数

验收时可在播放前后获取精确时间戳，统计 `coreaudiod` 报告的 Overload 次数：

```bash
TTS_ACCEPT_START="$(date '+%Y-%m-%d %H:%M:%S')"
# 在 Sona UI 连续播放 60–90 秒验收文本。
TTS_ACCEPT_END="$(date '+%Y-%m-%d %H:%M:%S')"
/usr/bin/log show \
  --start "$TTS_ACCEPT_START" \
  --end "$TTS_ACCEPT_END" \
  --style compact \
  --predicate 'process == "coreaudiod" AND eventMessage CONTAINS[c] "HALS_OverloadMessage: Overload"' \
  | rg -c 'HALS_OverloadMessage: Overload'
```

> [!NOTE]
> 当测试期间未发生任何 Overload 时，`rg -c` 会匹配到 0 行并返回退出码 1。这属于正常行为，**验收依据为最终计数为 `0`**（或直接无输出）。

### 2.2 查看服务日志与输出配置

查看 `sona-ui` 启动后解析到的音频输出参数：

```bash
rg -n 'audio-output: device=.*rate=.*buffer=.*frames' runtime/logs/ui.log | tail -n 1
```

正常输出示例（内置扬声器）：
```text
audio-output: device='MacBook Pro Speakers' index=1 rate=48000 buffer=1920 frames (40ms)
```

### 2.3 应用队列与性能指标检查

请求服务健康与指标端点：

```bash
curl -s http://127.0.0.1:8100/api/services | jq .
```

重点监控指标项：
- `audio_hub.pipecat.dropped_chunks`: 应保持为 `0`
- `interaction.dropped_chunks`: 应保持为 `0`
- `tts.source_chunk_gaps_over_200ms`: 应保持为 `0`

---

## 3. 验收规范与流程

### 3.1 三轮固定长文本测试

准备一段 60–90 秒标准中文长文本（涵盖短句、逗号句号停顿、数字与英文缩写）：

1. **第一轮（冷启动）**：服务启动后首次播报，验证音频输出管道冷启动平稳性；
2. **第二轮（热播报）**：紧接着进行连续第二轮播报，验证长期调度稳定性；
3. **第三轮（混合特征）**：测试包含长停顿与多数字/英文字符的文本，验证标点断句间隙的无爆音表现。

每轮播报前记录 `TTS_ACCEPT_START`，结束后记录 `TTS_ACCEPT_END` 并执行 CoreAudio 检索。

### 3.2 自然结束与打断验收

- **自然结束（3 次）**：TTS 播报自然放完，检查尾字是否有截断或爆音，确认 `echo-state` 及时由 `speaking` 恢复为 `idle`；
- **真人插话打断（3 次）**：播报中途发声插话，验证输出流被立刻取消，无残留破音，下一轮识别与回答立即正常响应。

### 3.3 验收硬指标基准

```text
CoreAudio overload count                         0
audio_hub.pipecat.dropped_chunks                 0
interaction.dropped_chunks                       0
tts.source_chunk_gaps_over_200ms                 0
可闻爆音/噼啪/失速/变调/尾字截断                 0
首包与总播报时长相对基线恶化                     <= 10%
```

---

## 4. 日志隐私与安全边界

- 严禁在日志中打印 TTS 合成文本明文；
- 严禁打印原始 PCM 音频数据；
- 严禁输出 SpeechRail/LM Studio API Key 明文；
- 不记录设备硬件 UID。

---

## 5. 故障回退方案

若在特殊外接声卡或硬件环境下发现兼容性问题，可通过配置显式回退至上游 `LocalAudioTransport`：

在 `.env` 或环境变量中配置：

```text
SONA_INTERACTION_AUDIO_OUTPUT_STABLE_ENABLED=false
```

生效配置重启服务：

```bash
scripts/sona-ctl.sh restart -d
scripts/sona-ctl.sh status
```

回退后，Sona 将恢复旧有由 PortAudio/CoreAudio 协商流缓冲的运行模式。
