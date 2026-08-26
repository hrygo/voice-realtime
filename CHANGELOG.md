# Changelog

## [1.2.0] - 2026-08-26

### Added

- 增加 Qwen3-TTS 四重纵深防御：输入强制终结标点归一化、自回归重复惩罚参数强化（`repetition_penalty=1.25`）、动态字符级 Token 熔断上限与音频 `nan_to_num` 极值清洗和 5ms 线性淡出。
- 增加全链路日志轮转、敏感凭据自动脱敏（`SanitizingFilter`）、排障指引与交互时延度量（TTFT / TTFA）。
- 增加运行时两阶段工作负载仲裁机制，支持 `assistant` / `meeting` / `idle` 模式安全互斥与 PCM 重连快照恢复。
- 增加语音助手默认聆听态（👂）基准流转与状态栏居中防抖动效。

### Changed

- 统一升级项目版本号至 `1.2.0`（包含后端 FastAPI、前端控制台与契约层）。
- 优化 Voice Studio 控制台三大模块响应式布局与设计 Token。

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
- 增加 Voice Studio 助手、会议、字幕和状态栏相关的控制台交互能力。

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
