---
title: "ASR 质量与模式恢复实施计划"
description: "恢复普通字幕 supervisor 并切换至 Qwen3-ASR 1.7B 离线配置的执行任务清单"
status: implemented
type: execution_plan
category: subtitles
version: "v1.0.0"
date: 2026-08-21
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "sona-core"
tags:
  - execution-plan
  - qwen3-asr
  - subtitles
---

# ASR Quality and Mode Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 恢复会议后的普通字幕链路，并以严格离线的 Qwen3-ASR 1.7B 质量配置提升本机中文转写准确率。

**Architecture:** `SubtitleProxy` 自己维护应用级 supervisor 与临时会议流之间的生命周期不变量；共享 `model_cache` 只向模型运行库交付本地路径；字幕质量参数由 `SubtitleSettings` 校验并由 launcher 显式投影到 WLK CLI。

**Tech Stack:** Python 3.12、asyncio、Pydantic Settings、WhisperLiveKit、Hugging Face Hub、pytest。

**Spec:** `docs/superpowers/specs/2026-08-21-asr-quality-and-mode-recovery-design.md`

## Global Constraints

- Python 严格保持 `>=3.12,<3.13`。
- 默认禁止模型隐式联网下载；缺少本地模型时 fail-fast。
- 会议不保存音频、不写普通字幕 SRT，PostgreSQL 仍是 confirmed 文本唯一事实源。
- 助手与会议模式继续互斥，麦克风仍由 AudioHub 单源采集。
- 不修改 vendor 子仓库实现；把代码已直接使用、锁文件中已存在的 `huggingface-hub` 声明为
  项目直接依赖，不引入新的第三方包。

---

### Task 1: 恢复会议后的普通字幕 supervisor

**Files:**
- Modify: `src/sona/ui/subtitle_proxy.py`
- Test: `tests/test_subtitle_proxy.py`

**Interfaces:**
- Consumes: `SubtitleProxy._running`、`_supervisor_task`、`_supervise_connection()`。
- Produces: `SubtitleProxy._resume_browser_connection() -> None`，满足 `running && no capture => supervisor exists`。

- [x] **Step 1: 写失败测试**

为正常结束、中止、最终化超时新增状态测试：捕获关闭后 supervisor 会重新创建；应用 `stop()` 期间不会恢复。

- [x] **Step 2: 运行 RED**

Run: `uv run pytest tests/test_subtitle_proxy.py -q --no-cov`

Expected: 新增测试因 `_supervisor_task is None` 或状态保持 `stopped` 失败。

- [x] **Step 3: 写最小实现**

增加 `_resume_browser_connection()`，在 `_close_capture()` 的统一尾部调用；`stop()`/shutdown 先清除
`_running`，统一关闭路径便可依据运行标志抑制恢复，无需为 shutdown 增加第二套关闭分支。

- [x] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/test_subtitle_proxy.py tests/test_meeting_session.py tests/test_runtime_mode.py -q --no-cov`

Expected: PASS。

### Task 2: 收敛严格离线模型解析

**Files:**
- Create: `src/sona/model_cache.py`
- Modify: `src/sona/interaction/pipeline.py`
- Modify: `src/sona/tts_bridge/engine.py`
- Modify: `src/sona/config.py`
- Create: `tests/test_model_cache.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_engine.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `resolve_model_snapshot(model: str, *, default_repo: str | None = None, allow_downloads: bool = False) -> str`。
- Bridge config adds `allow_model_downloads: bool = False`。

- [x] **Step 1: 写模型解析失败测试**

覆盖本地路径直通、默认仓库离线解析、自定义仓库离线解析、显式联网四种状态；TTS 断言 `mlx_audio.load()` 接收本地快照。

- [x] **Step 2: 运行 RED**

Run: `uv run pytest tests/test_model_cache.py tests/test_engine.py::TestLoad tests/test_pipeline.py::TestResolveSttModel tests/test_config.py -q --no-cov`

Expected: 因模块、配置字段和本地解析行为尚不存在而失败。

- [x] **Step 3: 实现共享解析器并迁移调用方**

解析器只负责本地路径/快照；pipeline 保留 `_resolve_stt_model()` 兼容入口并委托共享函数；
`TTSEngine.load()` 先解析本地路径再调用 `mlx_audio.tts.utils.load()`。

- [x] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/test_model_cache.py tests/test_engine.py tests/test_pipeline.py tests/test_config.py -q --no-cov`

Expected: PASS。

### Task 3: 启用 Qwen3-ASR 1.7B 质量配置

**Files:**
- Modify: `src/sona/config.py`
- Modify: `src/sona/subtitles/launcher.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_subtitles.py`

**Interfaces:**
- `SubtitleSettings` adds bounded Qwen3 streaming quality fields and `context`。
- `build_server_argv()` only emits Qwen3-specific flags for `backend == "qwen3-streaming"`。

- [x] **Step 1: 写失败测试**

断言默认目录为 `runtime/qwen3-asr-1.7b`；质量参数、context 和 punctuation split 被投影；
funasr 后端不包含 Qwen3 专属参数；非法范围和过长 context 被拒绝。

- [x] **Step 2: 运行 RED**

Run: `uv run pytest tests/test_subtitles.py::TestBuildServerArgv tests/test_config.py -q --no-cov`

Expected: 默认目录和缺失字段断言失败。

- [x] **Step 3: 实现配置与 CLI 投影**

使用 Pydantic 数值范围约束；context 为空时不发送；punctuation split 只在 diarization 开启时发送。

- [x] **Step 4: 运行 GREEN**

Run: `uv run pytest tests/test_subtitles.py tests/test_config.py -q --no-cov`

Expected: PASS。

### Task 4: 更新模型准备与运行文档

**Files:**
- Modify: `scripts/download-models.sh`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/实时语音交互与字幕-方案与最佳实践.md`
- Modify: `docs/架构图与流程图.md`
- Modify: `docs/会议助手后端运行与前后端联调.md`

**Interfaces:**
- 模型安装脚本通过 Qwen 官方 ModelScope 镜像把 `Qwen/Qwen3-ASR-1.7B` 写入
  `runtime/qwen3-asr-1.7b`。
- 文档统一以 1.7B、MPS windowed、严格离线和会议后字幕自动恢复为准。

- [x] **Step 1: 修改脚本与文档**

删除 0.6B 作为默认主链路的说明，保留其作为可选轻量候选的历史语义；命令保持原生、可移植。

- [x] **Step 2: 执行静态检查**

Run: `rg -n "runtime/qwen3-asr-0.6b|qfuxa/qwen3-asr-0.6b-streaming" README.md AGENTS.md scripts src docs/实时语音交互与字幕-方案与最佳实践.md docs/架构图与流程图.md docs/会议助手后端运行与前后端联调.md`

Expected: 不再存在把 0.6B 描述为当前默认路径的匹配。

### Task 5: 准备模型并完成验收

**Files:**
- Runtime artifact: `runtime/qwen3-asr-1.7b/`（gitignored）

**Interfaces:**
- `vr-subtitles` 从默认本地目录加载官方 Qwen3-ASR 1.7B。

- [x] **Step 1: 下载官方模型到默认目录**

Run: `uv run python -c "from modelscope import snapshot_download; print(snapshot_download('Qwen/Qwen3-ASR-1.7B', local_dir='runtime/qwen3-asr-1.7b', max_workers=8))"`

- [x] **Step 2: 验证模型完整性**

确认 `config.json`、tokenizer 文件和全部 safetensors shard 存在，且启动参数使用默认目录。

- [x] **Step 3: 执行全量门禁**

Run:

```bash
SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/
uv run mypy src/
uv run ruff check src/ tests/
cd ui && npm test -- --run
cd ui && npm run build
```

Expected: 全部退出码为 0，分支覆盖率不低于 80%。

- [x] **Step 4: 执行真实闭环**

依次启动 `vr-subtitles`、`vr-bridge`、临时 schema 下的 `sona-ui`；验证健康检查、EOF、
`assistant -> meeting -> idle -> assistant`、冲突拒绝、字幕重新 connected、数据库无音频载荷。
