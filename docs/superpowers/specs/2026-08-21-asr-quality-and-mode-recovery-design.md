# ASR 质量与模式恢复设计

## 目标

在不改变四进程拓扑、不保存音频、保持严格本地运行的前提下完成三项改进：

1. 修复会议捕获结束或中止后普通字幕连接不恢复的问题。
2. 将默认实时字幕模型切换为 `Qwen/Qwen3-ASR-1.7B`，并提供显式的质量与领域上下文配置。
3. 让 TTS 与 STT 共用严格的本地模型解析边界，默认运行时不得访问模型仓库网络。

## 现状与根因

- `SubtitleProxy.begin_capture()` 会停止普通字幕 supervisor；`_close_capture()` 只关闭会议流并把
  状态置为 `stopped`，没有恢复 supervisor。此时 `_running` 仍为 `True`，再次调用 `start()` 也会
  因幂等保护直接返回。
- `SubtitleSettings.model_size` 声明 1.7B，但 `model_dir` 默认指向
  `runtime/qwen3-asr-0.6b`；启动器优先使用 `model_dir`，实际运行的是 0.6B。
- 交互 STT 已有“仓库 ID 先解析成本地快照”的规则，TTS 引擎却直接把仓库 ID 交给
  `mlx-audio`，即使模型已缓存也可能访问 Hugging Face API。

## 架构决策

### 1. 字幕连接恢复属于 SubtitleProxy

会议模式只临时借用字幕传输，不拥有应用级字幕服务生命周期。恢复逻辑放在
`SubtitleProxy` 内部，而不是散落到 `RuntimeModeCoordinator` 或 `UIRuntime`：

- `start()` 负责应用级运行标志。
- `begin_capture()` 暂停普通 supervisor，创建会议专属流。
- `finish_capture()`、`abort_capture()` 和最终化超时都通过统一关闭路径释放会议流。
- 关闭会议流后，只要应用仍处于 running，就重新创建普通 supervisor；应用真正 `stop()` 时不恢复。

这样所有捕获退出路径共享同一不变量：`running && no capture => supervisor exists`。

### 2. 单一本地模型解析边界

新增 `voice_realtime.model_cache.resolve_model_snapshot()`：

- 已存在的本地文件或目录原样返回。
- 仓库 ID 通过 `huggingface_hub.snapshot_download()` 解析。
- 默认 `local_files_only=True`；只有显式 `allow_downloads=True` 才允许下载。
- 若旧缓存只缺 README 等非运行文件，严格离线解析可返回异常携带的现有 snapshot 路径，
  再由模型加载器校验真正需要的权重与配置，绝不因缓存清单不完整而联网。
- 上层只接收本地路径，不再把仓库 ID 直接交给模型运行库。

交互 SenseVoice 与 TTS 引擎复用此函数；字幕启动器继续使用显式 `model_dir`，保持子仓库虚拟环境隔离。

### 3. ASR 质量配置

默认实时字幕模型目录改为 `runtime/qwen3-asr-1.7b`。Qwen3 streaming 使用上游验证过的
windowed 质量配置：

- chunk `2.0s`
- left context `12.0s`
- right context `640ms`
- hold-back words `6`
- stable iterations `2`
- max new tokens `256`
- device `mps`
- audio backend `windowed`
- punctuation split 开启

新增可选 `context` 字段，通过 `--qwen3-streaming-context` 传递转写约束、领域词、人名和缩写。
字段限制长度并去除边界空白，避免无界 prompt。非 `qwen3-streaming` 后端不接收这些专属参数。

### 4. 模型准备

`scripts/download-models.sh` 使用 Qwen 官方 ModelScope 镜像的 `snapshot_download()` 把
1.7B 模型物化到 `runtime/qwen3-asr-1.7b`。TTS 和 SenseVoice 只下载到 Hugging Face
缓存；运行时解析为缓存快照。下载仍是一次性显式安装动作，运行服务默认离线。

## 错误处理

- 1.7B 目录缺失时保持 fail-fast，不静默回退 0.6B 或联网。
- 捕获恢复连接失败时 supervisor 进入既有 BACKOFF 状态，不影响会议数据最终化结果。
- 应用关闭期间不重新创建 supervisor，避免 shutdown 竞态。
- TTS 缓存缺失时抛出原始 Hugging Face 本地快照错误，启动失败并明确暴露模型未准备。

## 验收标准

1. 单元测试先证明当前 `finish_capture`、`abort_capture`、最终化超时后字幕不恢复，再由实现转绿。
2. 字幕启动参数测试证明 1.7B 默认目录、质量参数、上下文与 punctuation split 正确，且其他后端不泄漏参数。
3. 模型解析测试证明本地路径不访问 Hub、默认仓库解析严格离线、显式下载才联网。
4. TTS 加载测试证明传给 `mlx-audio` 的是解析后的本地路径。
5. 全量后端、类型、Lint、前端测试与构建全部通过。
6. 真实 `assistant -> meeting -> idle -> assistant` 后字幕重新为 `connected`；EOF、冲突拒绝和数据隔离保持正常。
