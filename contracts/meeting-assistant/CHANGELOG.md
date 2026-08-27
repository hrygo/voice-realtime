# Meeting Assistant Contract Changelog

## [1.2.0] - 2026-08-27

### Added

- 新增 `/ws/v1/meetings/{meeting_id}/inner-os` 私有 WebSocket 交互通道，支持单连接私密问答与取消操作。
- 新增 `InnerOSQueryCommand`、`InnerOSCancelCommand`、`InnerOSEventEnvelope`、`InnerOSAnswer`、`InnerOSExchange` 模式定义。
- 新增 Inner OS 持久化与查询 REST 端点：`PUT/GET/DELETE /api/v1/meetings/{meeting_id}/inner-os/exchanges/{exchange_id}` 及 `GET /api/v1/meetings/{meeting_id}/inner-os/exchanges`。
- `RuntimeState` 新增只读 `capabilities`（`inner_os_enabled`, `inner_os_analysis_enabled`, `inner_os_channel`）。
- 新增标准 fixtures: `inner-os-completed.json`, `inner-os-insufficient.json`, `inner-os-invalid-focus.json`。

### Compatibility

- 所有新接口、通道、字段与事件均为 additive；原有 `/ws/v1/meetings` 和既有 REST 接口语义保持不变。

## [1.1.0] - 2026-08-26

### Added

- 新增 durable `meeting_title_updated` 事件，使 AI/手动标题更新实时同步到活动页、详情页和历史列表。
- `minutes_state_changed` 可选携带脱敏的 LM Studio 调用统计，不包含 prompt、转录或模型正文。
- 会议纪要内容契约增加条目数、文本长度和证据数上限，阻断异常超长输出。

### Compatibility

- 新事件和可选字段均为 additive；既有 v1 消费端可继续忽略未知事件或字段。

## [1.0.0] - 2026-08-26

### Added

- 明确 `/api/v1`、`/ws/v1/control` 和 `/ws/v1/meetings` 为 v1 canonical 入口。
- 将 `start_subtitles` 纳入控制命令契约。
- 为 9 类会议事件补充独立 envelope/payload schema。
- 增加活动会议 snapshot 和 revision gap fixtures。
- 固化 `meeting_snapshot`、partial、confirmed、revision、resync 和 `replace_from_ms` 语义。
- 后端 producer 保留已知 partial speaker、拦截 revision gap，并提供不依赖模型的契约 mock 回放入口。

### Compatibility

- 保留既有 v1 公共字段和兼容路径；兼容路径不得用于新功能依赖。
- v1 内新增字段必须为 additive；breaking change 进入 v2。
