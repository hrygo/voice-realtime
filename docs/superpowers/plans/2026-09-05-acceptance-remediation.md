# 三模式验收缺陷修复 Implementation Plan

> 执行方式：按用户要求，仅使用 `luna_worker` 子代理；主代理协调文件所有权、审查与端到端复验。每个任务遵循 RED → 最小修复 → GREEN，不代替用户提交已有工作区改动。

**Goal:** 修复三模式验收 F01–F07，并验证报告中的准确性、遥测与 EOF 观察项。

**Architecture:** 保持既有运行时所有权与 ASR 中立模型。SpeechRail 适配器负责会话绝对时间，普通字幕会话负责累计展示快照；会议继续使用明确修订区间对账。错误、readiness、静音和说话人显示使用真实状态。

**Tech Stack:** Python 3.12、pytest、PostgreSQL、React、TypeScript、Vitest。

**Spec:** 2026-09-05《Sona 三模式端到端验收报告》F01–F07；用户已批准修复全部已发现问题。

## Global Constraints

- 保留其他任务已有与正在进行的改动；按文件分配写权限，同文件任务串行。
- 无新依赖、无模型下载、无业务数据库迁移或业务数据清理。
- LM Studio 原生端点、回声双防线、单 PCM owner、会后即焚继续保持。
- 真实验收只写独立临时 schema，结束清理；人工硬件与长时样本不足不得伪称通过。
- 普通文本对比度至少 4.5:1；测试命令和持久文档使用可移植的原生命令。

## 原子任务与调度

每项先添加列出的失败断言，运行聚焦测试确认失败，然后修改所有权内实现并重新验证。需要扩大文件范围先向主代理申请。

| ID | 对应问题 | 独占写入范围 | 交付断言/验收条件 | 依赖 |
|---|---|---|---|---|
| T1 | F01 时间回退 | speechrail/transcriber.py、transcription_events.py、对应 ASR 测试 | 后轮0起点根据真实item/VAD定位到会话绝对时间；重连offset正确；不破坏合法修订 | 无 |
| T2 | F02 字幕覆盖 | StandardSubtitleSession、字幕历史聚焦测试 | confirmed A、partial B、confirmed B → full_update和SRT含A+B；重连不重复 | 与T1按契约协调 |
| T3 | F03 错误消失 | assistantStore、AssistantPanel及对应测试、新增小错误组件 | pipeline_error→llm.final/tts.stopped仍保留错误；错误可见；可恢复输入且不隐式重复发送 | 无 |
| T4 | F04 后端readiness | ui/app_context.py、ui/runtime.py及聚焦测试 | 会议装配失败后runtime.storage不是ok；开始失败原因明确；正常装配恢复ok | 避免与外部音源任务冲突 |
| T5 | F04 历史失败伪空 | meetingStore、MeetingPanel/HistorySidebar及对应测试 | HTTP 503显示错误与重试，非暂无会议；初始未探测状态不呈现绿色 | T4接口确认 |
| T6 | F05 静音/所有权文案 | StatusBar、AssistantPanel、SubtitleStream及对应测试 | 静音、电脑音源、idle、播报状态不出现相反收音文案 | T3完成后 |
| T7 | F06 说话人显示 | 会议speaker呈现的最小文件及聚焦测试 | group key不出现在显示名；未知与已识别区分；一人提示语不假称已准确识别 | T5完成后 |
| T8 | F07 对比度 | AssistantPanel.css | 两个报告配色达到4.5:1；保持暗色主题；浏览器实色复测 | 无，同组件仅CSS可独立 |
| T9 | F01/F02 ID隔离及集成防回归 | meeting/asr_mapping.py、独立多轮转录集成测试 | 不同会议/不同时间同文不碰撞；同窗重播幂等；3轮含责任人日期金额，数据库/API/导出一致 | T1、T2 |
| T10 | 遥测观察项 | bridge与遥测展示最小文件 | UI明确端点时延口径；缺失数据不称全链路极速；不伪造声学时间 | T3、T6、T8后 |
| T11 | EOF、准确性观察项 | 先只读协议和真实合成样本 | 录制上游完整帧定位终止语义；复核合成/识别问题，确认是代码缺陷再最小修复 | T1后 |
| T12 | 总体验证 | 主代理测试与验收报告 | 质量门禁、独立审查、真实服务复验；临时数据清理 | 全部实现结束 |

### 测试循环示例

```python
# 多轮结果必须投影到会议绝对时间，不覆盖第一轮。
assert second.start_ms >= first.end_ms
assert [segment.text for segment in persisted] == [first.text, second.text]
```

```typescript
// 收尾事件不是错误恢复信号。
const failed = reduceAssistantEvent(initial, { type: "system", state: "pipeline_error", message: "服务不可用" });
const settled = reduceAssistantEvent(failed, { type: "llm", state: "final", turn_id: 0, text: "" });
expect(settled.phase).toBe("degraded");
```

聚焦检查：`uv run pytest <具体测试文件> --no-cov`、`npm test -- --run <具体测试文件>`。

最终门禁：`SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/`、`uv run mypy src/`、`uv run ruff check src/ tests/`、前端 `npm test -- --run` 与 `npm run build`。PostgreSQL测试只用测试自动创建并清理的临时schema。

## 任务状态

- [x] T1 时间归一化（44 项聚焦测试与真实协议八轮回放通过；EOF另由T11处理）
- [x] T2 字幕累计（历史、修订、SRT、真实重连offset聚焦复验通过）
- [x] T3 助手失败可见（33 项聚焦测试复验通过）
- [x] T4 后端readiness（runtime 与 server 聚焦测试复验通过）
- [x] T5 历史加载错误（49 项聚焦测试复验通过）
- [x] T6 静音状态（68项组件聚焦测试复验通过）
- [x] T7 说话人标签（REST/WS默认名与识别分组计数，真实会议界面复验）
- [x] T8 对比度（浏览器实色浅色6.15:1、6.92:1；暗色5.71:1、6.96:1）
- [x] T9 ID隔离（真实PG两会议各两轮且重播不重复；整体多轮业务链由T12复验）
- [x] T10 遥测口径（24项聚焦测试复验通过）
- [x] T11 协议/语音观察项复核（EOF回执屏障及48项聚焦测试复验通过；声学准确率不作泛化结论）
- [x] T12 独立审查与端到端复验（真实服务、合成PCM、独立临时schema；证据已归档，环境已清理）

## 主代理复验补充（2026-09-05）

- 已用真实协议八轮回放验证 T1：分段时间有序，关键责任人、日期与预算完整。
- EOF 复现：停止后到达的旧 completed 触发提前结束，第二个 EOF completed 留在队列。T11 增加有序结束确认，不使用固定延时。
- 真实 PostgreSQL 临时 schema 复现 ID 碰撞：两场会议同文、不同 group，结果条数 [1, 0]，期望 [1, 1]。临时 schema 已清理；T9 修复 identity。
- T2 重连实路径已补传连续 offset，并覆盖发送失败、取消与队列丢弃的缺口上报。

### T12 交叉审查追加闭环

- [x] 当前工作区与实际owner混用、静音阶段胶囊遗漏：父组件测试及浏览器复验。
- [x] 字幕静音/电脑来源待采集仍为active：DOM回归及浏览器静音listening类为0。
- [x] 中文/emoji会议标题导出500：编码下载名，真实四格式HTTP200、关键内容齐全。
- [x] 重叠修订并列段丢失：排他槽位回归通过。
- [x] summary启动失败发布半初始化依赖：失败后重试成功测试通过。
- [x] 重连发送失败/取消/清队列的PCM缺口上报：4项独立回归及完整994项后端测试通过。

最终门禁快照：后端994项通过、覆盖率84.52%；前端274项通过；mypy 106个source文件无错误；ruff及生产构建通过。2026-09-05 11:20（Asia/Shanghai）完成最终真实服务复验：字幕文本/SRT完整、会议8段与纪要完成、四格式导出200、助手正文与TTS事件正常；旧测试会议未被后续会议覆盖。临时schema、验收服务与页面已清理，原有SpeechRail/LM Studio仍在运行。

差异检查：本轮范围通过；全局检查中的 `tests/test_logging.py:198` 文件末尾空行为开始前已有，已按字节核对并保留。
