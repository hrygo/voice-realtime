---
title: "架构整治验收与质量门禁规范"
description: "Voice Realtime 架构整治后的功能与性能验收测试规范"
status: implemented
type: test_record
category: architecture
version: "v1.0.0"
date: 2026-08-21
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "voice-realtime-core"
tags:
  - verification
  - quality-gate
  - architecture-remediation
---

# Voice Realtime 架构整治验收记录

## 结论

2026-08-21，方案 A 的自研代码范围已完成：`vr-ui` 成为默认且唯一的交互所有者，
`vr-interact` 复用同一 `InteractionSession` 并通过稳定文件锁互斥。字幕、交互、TTS、
LLM、控制面、前端状态、安全边界、配置与文档均已接线并通过自动化与本机运行级验证。

## 自动化门禁

| 门禁 | 结果 |
|---|---|
| `uv run pytest tests/ -W error::pytest.PytestUnraisableExceptionWarning` | 271 passed；分支覆盖率 84.55%，门槛 80% |
| `uv run mypy src/` | 29 个源文件 clean |
| `uv run ruff check src/ tests/` | clean |
| `npm test -- --run` | 5 个文件、13 项测试通过 |
| `npm run build` | TypeScript 与 Vite production build 通过 |
| `npm audit --audit-level=high` | 0 vulnerabilities |

仅有两条第三方弃用警告：Python 3.13 将移除 `audioop`，以及 Pipecat 2.0 将移除旧的
`SpeechTimeoutUserTurnStopStrategy.reset` 覆写点；当前项目严格锁定 Python 3.12，且两者均非
项目代码产生的未等待协程或资源泄漏。

## 需求证据矩阵

| 范围 | 已验证行为 | 主要证据 |
|---|---|---|
| 单一所有者 | UI/headless 共用会话；双开被拒绝；停止先 `runner.end()`，超时才取消；重启保留 persona/duplex | `tests/test_interaction_session.py`；真实双进程冲突与 UI 重启 |
| 音频 | 16kHz/mono/int16/512 frames；打开失败上抛；每 sink 有界；慢 sink 隔离；真实静音清队列 | `tests/test_audio_hub.py`、`tests/test_audio_injector.py`；真机麦克风打开 |
| 回声与打断 | EchoState 单写者；新播报代次重置包络；短应答不过滤；耳机模式触发/重锁门限稳定 | `tests/test_pipeline.py` 的合成 PCM 与帧序列测试 |
| 字幕 PCM | WLK 始终以 `--pcm-input` 启动；本地目录优先；默认离线缺失立即失败 | `tests/test_subtitles.py`；18001 临时端口真实启动参数 |
| 字幕可靠性 | 全量 confirmed+partial 快照；签名去重；断线退避；慢客户端隔离；原子 SRT 与归档 | `tests/test_subtitle_proxy.py`、`tests/test_events_tracker.py`；真实 WS config 与 PCM 上行 |
| TTS | 请求 voice 生效；热切换默认音色不被 Pipecat 的占位音色覆盖；MLX 串行、有界、可取消；PCM 首块错误可见；WAV 单次聚合 | `tests/test_engine.py`、`tests/test_server.py`；18765 真实模型生成 88,320 bytes PCM |
| LM Studio | 原生 `/api/v1/chat`；`reasoning:"off"`；SSE 错误/非法 delta/空输出校验；客户端显式关闭 | `tests/test_reasoning.py`；1234 真实响应 `reasoning_output_tokens=0`、TTFT 0.238s |
| 控制与状态 | 初始完整 state；命令 `request_id` 关联；成功后才更新；真实 mute；重启与完整状态确认 | `tests/test_control.py`、`tests/test_ui_server.py`、前端 socket/store 测试；18100 真实 WS 命令 |
| 安全 | host 与依赖 URL 仅 loopback；恶意 Origin 拒绝；CSP/nosniff/no-referrer/DENY；严格 schema 不泄漏内部异常 | `tests/test_config.py`、`tests/test_ui_server.py`；真实恶意 Origin 返回 403 |
| 健康与指标 | `/api/runtime` 返回组件真状态；外部服务探活含目标模型；每轮时间戳重置；缺失阶段为 null；首个真实音频闭合 TTS TTFB | `tests/test_ui_server.py`、`tests/test_assistant_bridge.py`；18100 真实 API |
| 前端 | 控制/事件连接可取消重连；服务端状态权威；助手相位与 SRT 时间正确；production build 可发布 | 13 项 Vitest；TypeScript/Vite build |
| 元数据与文档 | Python 3.12 元数据一致；默认 pytest 执行覆盖率门禁；四运行单元拓扑和替代入口一致 | `pyproject.toml`、README、AGENTS、三份架构/方案文档 |

## 本机运行级结果

- LM Studio 已加载 `qwen/qwen3.6-35b-a3b`；原生聊天返回“好”，推理 token 为 0。
- TTS 桥使用真实 4.2GB 本地模型完成健康检查、音色切换与 PCM 合成，并可干净退出。
- WhisperLiveKit 使用本地 Qwen3-ASR 目录及 `--pcm-input` 启动，客户端收到 config 事件并发送
  20,480 bytes PCM；日志无 FFmpeg PCM 写入错误、Traceback 或 ERROR。
- `vr-ui` 使用真实麦克风启动；`/api/runtime`、控制握手、静音/取消静音、重启均成功；
  同时启动 `vr-interact` 被所有权锁拒绝；恶意 Origin 握手被拒绝；关闭过程干净。
- 真实重启额外发现并修复了两项集成缺陷：离线 STT 仍尝试联网，以及 Pipecat 修改观察器列表后
  污染下一次重启。复测确认无 HuggingFace HTTP 请求，且重启使用干净观察器集合。

## 外部边界与剩余风险

- 当前 WhisperLiveKit 内置的 `qwen3-asr-causal` 在加载 `runtime/qwen3-asr-0.6b` 时提示上游
  tokenizer 正则需要 `fix_mistral_regex=True`，并提示 `temperature` 生成参数可能被忽略。
  加载点位于被项目 `.gitignore` 排除的 vendor 依赖内部，当前 launcher 没有稳定参数入口。
  本轮不引入 `sitecustomize` 或 monkey patch；升级/修复 vendor 后应重新做中文转写准确率回归。
- 外放回声与耳机插话已由确定性 PCM/帧测试覆盖状态、阈值和重锁逻辑；不同房间、音量与设备的
  声学效果仍需在最终使用环境校准，这属于环境 QA，不再是未接线功能。
