# 会议助手后端 V1 验收记录

日期：2026-08-21（Asia/Shanghai）

## 交付范围

- `assistant / meeting / idle` 互斥运行模式；会议中停止 Pipecat、交互 LLM 与 TTS。
- WhisperLiveKit 会议专属采集租约、partial 事件、confirmed 窗口对账、EOF/`ready_to_stop` 冲刷。
- PostgreSQL migration、异步 repository、恢复 journal、崩溃恢复和游标分页。
- LM Studio 原生 `/api/v1/chat` 纪要 worker，模型 `qwen/qwen3.8-27b`、`reasoning:"off"`、
  结构化输出和 evidence segment UUID 校验。
- `/api/v1/*`、`/ws/v1/control`、`/ws/v1/meetings` 与
  `contracts/meeting-assistant/v1/` 前后端分离契约。
- 无音频持久化；会议采集不写普通字幕 SRT。

## 自动验证证据

### Python 与真实 PostgreSQL 临时 schema

```bash
VR_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/
```

结果：`463 passed`；总分支覆盖率 `83.96%`，高于 `80%` 门槛。真实 PostgreSQL 测试为每个
fixture 创建随机 `vr_test_*` / `vr_recovery_*` schema，并在结束时级联删除；结束后查询无残留。

```bash
uv run mypy src/
uv run ruff check src/ tests/
uv lock --check
```

结果：mypy `42 source files` 无问题；Ruff clean；lock 共解析 `181 packages`。

### 前端契约兼容

```bash
cd ui
npm test -- --run
npm run build
npm audit --audit-level=high
```

结果：`11` 个测试文件、`44` 项测试通过；TypeScript 与 Vite 生产构建通过；`0 vulnerabilities`。

### 数据边界与 bootstrap

- 在 meeting 源码和契约中检查不存在 `bytea`、音频 blob、WAV/PCM 数据列。
- PostgreSQL bootstrap 脚本在显式事务内完整执行后 `ROLLBACK`，角色与正式 schema 均无残留。
- 临时 schema 清理查询返回空结果。
- `runtime/sortformer.nemo` 当前不存在；未获授权时未联网下载模型。

## 尚未执行的现场验收

以下不属于自动化实现缺陷，但在本机正式启用前仍需完成：

1. 管理员运行 `psql knowledge -f scripts/bootstrap-meeting-db.sql`，持久创建最小权限角色与正式
   `voice_realtime` schema。
2. 预先放置本地 `runtime/sortformer.nemo`，或显式授权并执行模型下载；默认离线策略会在缺失时
   fail-fast。
3. 同时运行 WhisperLiveKit、LM Studio、麦克风和独立前端，完成真实多人说话、浏览器断线重连、
   EOF 超时与长会议 map/reduce 的现场验收。

本次未创建正式数据库角色/schema，未下载模型，未保存或生成任何会议音频。
