# Meeting Assistant v1 Contract

## Status

当前公共契约、后端 producer、独立 mock 和后端质量门禁已落盘；前端 consumer（F1）
以及前后端联合 review/发布门禁仍待接收团队完成。

## Contract versions

- `contract_version: "1"` 表示 major contract version。
- OpenAPI/AsyncAPI `info.version` 使用 semver；当前基线为 `1.0.0`。
- v1 内只允许向后兼容的 additive 变更。
- 删除字段、修改字段类型或改变 revision/恢复语义时，必须进入 v2 并提供迁移说明。

## Public sources

- HTTP：`openapi.json`
- WebSocket：`asyncapi.yaml`
- 资源和事件：`schemas/`
- 可运行样例：`fixtures/`

## Canonical endpoints

- HTTP：`/api/v1`
- Runtime：`GET /api/v1/runtime`
- Control WebSocket：`/ws/v1/control`
- Meeting events WebSocket：`/ws/v1/meetings`

旧的 `/api/runtime`、`/ws/assistant/cmd` 和 `/ws/subtitles` 只作为兼容入口，不能用于新功能的契约依赖。

## Event rules

- `event_id` 用于去重，不用于排序。
- `occurred_at` 使用 UTC ISO 8601，仅用于记录和展示。
- `meeting_snapshot` 是连接、刷新和重连的权威基线。
- `transcript_partial` 是易失展示事件，不计入 confirmed 事实；payload 至少含
  `text`，只有上游明确识别到说话人时才填写 `speaker_key`/`speaker_name`，未知时保持
  `null`，后端也不得把无法解释的 opaque key 回显为展示名称；消费端不得从 key 猜测身份。
- `transcript_reconciled` 由 `transcript_revision` 排序，并按 `replace_from_ms` 替换窗口。
- `meeting_title_updated` 是 durable 事件；AI 或手动标题提交后，消费端必须同步详情、活动会议和历史列表。
- `minutes_state_changed.generation_stats` 只允许包含阶段、耗时和 token 统计，不得包含 prompt、转录或模型正文。
- 后端 producer 发现同一会议的 revision 从 `N` 跳到大于 `N + 1` 时，不广播跳跃事件，
  而是发出 `resync_required(reason=revision_gap, expected_revision=N+1)`；已过期事件直接
  忽略，客户端回源 transcript 重新建立基线。
- 对账窗口保留 `end_ms < replace_from_ms` 的历史，删除并替换 `end_ms >= replace_from_ms`
  的重叠片段；因此刚好结束于边界的旧片段也会被替换。
- 无法解释的 revision gap 或 `resync_required` 必须回源 `GET /api/v1/meetings/{meeting_id}/transcript`。
- 前端不得解析 `speaker_key`，不得跨 `source_epoch` 或跨说话人合并片段。

## Data boundaries

- PostgreSQL 是会议事实源。
- 产品不保存会议音频。
- `raw_output` 等模型内部字段不是前端功能依赖。
- 事件文本是用户内容，消费端必须按不可信数据处理。

## Fixture validation

Python 端使用 `jsonschema.Draft202012Validator` 校验 envelope 和事件 schema；前端使用同一批 JSON fixtures 做 consumer tests。新增或修改事件时，必须同时更新 schema、fixture、AsyncAPI/OpenAPI（如适用）和 `CHANGELOG.md`。

## Change workflow

1. 提交契约变更说明并判断 additive/breaking。
2. 更新 schema、fixtures 和变更日志。
3. 运行后端 producer contract tests。
4. 运行前端 consumer contract tests。
5. 前后端 reviewer 共同确认后发布 tag/artifact。
