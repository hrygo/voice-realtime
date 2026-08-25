# Changelog

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
