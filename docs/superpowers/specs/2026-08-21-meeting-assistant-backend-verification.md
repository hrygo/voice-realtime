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
- PostgreSQL bootstrap 已持久创建 `voice_realtime_app` 和 `voice_realtime` schema；应用角色为
  非 superuser、非 createrole、非 createdb，仅拥有本 schema 的 `USAGE/CREATE`。
- 临时 schema 清理查询返回空结果。
- `runtime/sortformer.nemo` 已从 `nvidia/diar_streaming_sortformer_4spk-v2` 固定 revision
  `5240a64075176943f677d30fa2171c780229f341` 下载；大小 `471367680` bytes，SHA-256
  `b371afce2c4958186469df33d939936b9746c89f38b10a69cfd2c61254e83329`。

## 本机正式部署与现场验收

- PostgreSQL：正式 migration `version=1` 已由 `voice_realtime_app` 应用，六张会议表均存在；该角色
  对 `ag_catalog`、`workstudio` 无 `USAGE/CREATE`，对数据库无 `CREATE`。本机 `.env` 已设置
  socket DSN 与独立 schema，文件由 Git 忽略且不含密码。
- WhisperLiveKit：服务以 `qwen3-streaming`、`--pcm-input`、Sortformer、最多 4 speakers、
  `--retention-seconds 0` 运行。向 WebSocket 发送 1 秒内存静音 PCM 后发送 EOF，收到
  `ready_to_stop`，服务无错误或崩溃，测试前后无音频文件新增。
- LM Studio：`qwen/qwen3.8-27b` 4bit 以 262144 context、parallel 1 加载；原生
  `/api/v1/chat` 接受 `reasoning:"off"`，实测 `reasoning_output_tokens=0`。
- 联合冒烟：使用正式 repository 创建一条合成会议，写入 confirmed segment、封存、排队纪要，
  真实 27B 生成结果通过精确 JSON Schema、evidence UUID 校验并持久化为 completed；测试记录随后
  通过级联删除清理。

现场验收曾发现模型把 `evidence_segment_ids` 输出为 `segments`、把行动项 `task` 输出为
`content`。根因是旧提示未携带实际 schema。修复后，初次生成、repair 和 reduce 均注入由
`MinutesResult.model_json_schema()` 生成的精确契约，并明确禁止别名；同一真实输入已重新通过。
最终审查还发现 worker 的 completed 事件没有携带契约 fixture 要求的完整纪要。repository 现已在
同一完成事务中 `RETURNING` 刚落库的版本，worker 将其直接放入 `minutes` 字段，避免前端停留在
生成中状态。

## 尚未执行的人工验收

以下能力需要真实参会场景，不使用合成静音即可替代，因此保留为上线观察项：

1. 真实多人说话下的中文转录准确率与 Sortformer 说话人区分效果。
2. 独立前端接入后的浏览器断线重连、长会议 map/reduce 与用户操作体验；前端团队需修复新会议
   切换时旧 segments/speakers/minutes/gaps 未清理，以及迟到 transcript 请求覆盖当前会议的竞态。
3. 麦克风设备切换、系统休眠和异常退出后的完整恢复演练。

本次不采集麦克风、不保存或生成任何会议音频。Sortformer 当前在 macOS 上由上游实现选择 CPU，
并非 MPS；这会影响长会议的实时余量，应在真实多人验收中观察。
