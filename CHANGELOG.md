# Changelog

## [1.4.0] - 2026-08-28

### Added

- 增加会中 Inner OS 私密伴侣：支持证据上下文快照、严格答案契约、流式查询、取消指令与会后即焚的临时背景。
- 增加用户显式保存的 Inner OS 问答持久化、历史归档与 REST API；未保存的临时背景和模型推理过程不落库。
- 增加共享本地推理调度器，统一协调语音交互、会议纪要与 Inner OS 的 LM Studio 工作负载。
- 增加 Inner OS P0 价值评测数据集、盲评规则、指标聚合与可复现报告。
- 增加 Inner OS 会中侧面板、快捷 Prompt、答案卡片、未保存托盘及历史视图，并扩展 OpenAPI、AsyncAPI 与 JSON Schema 契约。

### Changed

- 统一升级项目版本号至 `1.4.0`（后端 FastAPI、TTS 桥与前端控制台）。
- 统一复用 LM Studio 原生传输层，并保持 `/api/v1/chat` 的响应链、推理开关与输出边界约束。
- 重构会议工作台、侧边栏、顶部操作栏与折叠交互，改善响应式布局、快捷键可达性和全站无障碍对比度。

### Fixed

- 修复 Inner OS 在空转录、路由注册时序和异常契约场景下查询或响应卡片卡住的问题。
- 修复会议中重命名说话人后再次发言时名称被恢复为默认值的问题。
- 修复会议详情双栏挤压、工具栏溢出、折叠导轨 Tooltip 偏移及 `Cmd+K` 焦点冲突。

### Verification

- Python：`1327 passed, 1 warning`，分支覆盖率 `82.89%`（$\ge 80.0\%$）。
- Frontend：`215 passed`（34 test files），TypeScript/Vite 生产构建成功。
- `mypy`（strict，108 源文件全绿）、`ruff`（全通过）。

## [1.3.0] - 2026-08-27

### Added

- 增加 CAM++ 192 维声纹嵌入特征提取（阿里 3D-Speaker ONNX ~27MB，CPU 单段仅 ~12ms，纯内存处理，绝不落盘原始音频）。
- 增加会议实时在线声纹质心池跟踪（`CentroidPool`），相似度 $\ge 0.75$ 时自动映射归并。
- 增加会后全局 AHC 层次凝聚聚类二次修正（`AHCClusterer`），余弦距离 $\le 0.35$ 且受 `max_speakers` 强约束。
- 增加 1:N 已知说话人声纹注册与自动命名匹配器（`VoiceprintProfileMatcher`）。
- 增加 Sortformer 迟滞双门限判定机制（onset=0.50, offset=0.35, silence=0.25）与防静音吸气通道抖动。
- 增加动态参会人数容量先验 `max_speakers`（1~4）全链路贯通（前端 UI 下拉选择器 ➔ 控制协议 ➔ 后端协调器 ➔ WLK Sortformer）。
- 增加 `PostgresMeetingRepository.apply_speaker_remapping` 单事务原子更新段落与合并说话人记录。
- 增加架构决策记录 [ADR-008](docs/decisions/0008-speaker-diarization-and-voiceprint-clustering.md)。

### Changed

- 统一升级项目版本号至 `1.3.0`（包含后端 FastAPI、前端控制台与契约层）。
- 完善 `DiarizationSmoother` 时序平滑器，支持跨 Epoch 相同声道自然平滑（间隙扩至 1000ms）。

### Fixed

- 彻底根除会议模式下“一人多号 / 说话人过度分裂 / 静音漂移”缺陷。
- 修复短片段杂音与多段连续短闪烁翻转（$A-B-A$ 及 $A-B-B-A$ 序列平滑纠偏）。
- 修复跨 Epoch 重连导致说话人自定义名称丢失的问题，支持跨 Epoch 继承与原子重命名同步。

### Verification

- Python：`1269 passed, 1 warning`，分支覆盖率 `83.17%`（$\ge 80.0\%$）。
- Frontend：`166 passed`（20 test files），TypeScript/Vite 生产构建成功。
- `mypy`（strict，91 源文件全绿）、`ruff`（全通过）。

## [1.2.0] - 2026-08-26

### Added

- 增加 Qwen3-TTS 四重纵深防御：输入强制终结标点归一化、自回归重复惩罚参数强化（`repetition_penalty=1.25`）、动态字符级 Token 熔断上限与音频 `nan_to_num` 极值清洗和 5ms 线性淡出。
- 增加全链路日志轮转、敏感凭据自动脱敏（`SanitizingFilter`）、排障指引与交互时延度量（TTFT / TTFA）。
- 增加运行时两阶段工作负载仲裁机制，支持 `assistant` / `meeting` / `idle` 模式安全互斥与 PCM 重连快照恢复。
- 增加语音助手默认聆听态（👂）基准流转与状态栏居中防抖动效。

### Changed

- 统一升级项目版本号至 `1.2.0`（包含后端 FastAPI、前端控制台与契约层）。
- 优化 Sona 控制台三大模块响应式布局与设计 Token。

### Fixed

- 彻底根治短词、叠词（如“好的好的”、“嗯嗯，那咱们随时聊。”）在 Qwen3-TTS 下引发的声学死循环与长蜂鸣问题。
- 修复语音助手前端在多轮交互中对重复输入（如连续回复“没有。”）的误去重丢泡缺陷。
- 修复长会议纪要触顶未闭合 JSON 的输出边界收敛与异常处理。

### Verification

- Python：`1233 passed, 1 warning`，分支覆盖率 `83.11%`（$\ge 80.0\%$）。
- Frontend：`166 passed`（20 test files），TypeScript/Vite 生产构建成功。
- `mypy`（89 源文件全绿）、`ruff`（全通过）。

## [1.1.0] - 2026-08-25

### Added

- 增加 Qwen3-ASR、SenseVoice 和 Fun-ASR 的统一适配与本地运行入口。
- 增加 ASR 公共代理语料冻结、metadata-only 预检、阶段执行器、证据链和序贯决策工具。
- 增加可复现的 ASR benchmark 报告、公共代理 v1/v2 结果与开发语料生成脚本。
- 增加 Sona 助手、会议、字幕和状态栏相关的控制台交互能力。

### Changed

- 保持当前产品后端分工：会议/字幕使用 `Qwen3-ASR-1.7B`，语音交互使用 `SenseVoiceSmall`。
- ASR 评测结果继续标记为 `Experimental / No decision`；公共代理证据不直接触发生产模型切换。

### Fixed

- 加固混合语言自动检测、MPS 超时收敛、资源锁释放和聚类 bootstrap 评估边界。
- 补充 benchmark、ASR 适配层、会议控制台和字幕代理的回归测试。

### Verification

- Python：`1011 passed, 10 skipped`，覆盖率 `80.46%`。
- Frontend：`64 tests` passed，TypeScript/Vite production build passed。
- `mypy`、`ruff`、`uv lock --check` 和 Python package build passed。
