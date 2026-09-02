---
title: "UI 后端组合根重构实施计划"
status: draft
type: execution_plan
date: 2026-09-01
owners: ["sona-core"]
related_documents:
  - "docs/architecture/系统总体架构与详细设计方案.md"
  - "contracts/meeting-assistant/v1/README.md"
---

# UI Backend Composition Refactor Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task, with a review checkpoint after each commit.

**Goal:** 把 `ui/server.py` 拆成 typed application context、HTTP router、WebSocket router 和 meeting backend lifecycle，同时保留 `create_app` 签名、所有公开路径、lifespan 顺序和 standalone meeting API 测试 seam。

**Architecture:** FastAPI `app.state` 只保存一个经过类型检查的 `UIAppContext`；route factory 通过显式 context/provider 获取依赖，不再逐字段 `getattr`。`server.py` 保留 settings、middleware、lifespan、API installer、static mount 和 app assembly；HTTP/WS protocol 分别位于独立模块；meeting/inner-OS router 用自己的窄 provider dataclass，避免反向依赖 UI concrete runtime。

**Tech Stack:** Python 3.12、FastAPI lifespan/APIRouter、asyncio、pytest、Ruff、mypy。

---

## 执行边界

- 工作目录：`/Users/hrygo/Documents/sona`
- 前置：完成 `2026-09-01-meeting-lifecycle-ports-refactor.md` 和 `2026-09-01-interaction-pipeline-refactor.md`，使用其中的 typed meeting/runtime dependencies。
- 保持入口：`create_app(settings: Settings | None = None, *, initialize_meeting: bool = True) -> FastAPI`。
- 保持 HTTP：`/health`、`/api/services`、`/api/runtime`、`/v1/voices`、`/v1/audio/speech`、meeting v1、inner-OS API。
- 保持 WS：`/ws/subtitles`、`/ws/assistant`、`/ws/assistant/cmd`、`/ws/v1/meetings`、`/ws/v1/meetings/{meeting_id}/inner-os`、`/ws/v1/control`。
- 保持 static catch-all 最后安装；API/WS 必须先注册。
- 保持当前生命周期：`UIRuntime.start` → 可选 meeting backend init → serve → `UIRuntime.stop` → InnerOS close → summary stop → scheduler close → repository close。
- 保持 meeting backend 初始化 fail-soft：异常记录脱敏类型，核心 UI 仍可启动；不得把数据库或 LM Studio 失败伪装成 ready。
- `meeting/api.py` 与 `meeting/inner_os/api.py` 的 standalone installer 仍可测试；不得要求它们构造 `UIRuntime`。
- 不改变 CORS、WebSocket Origin/host policy、SecurityHeaders、SpeechRail Authorization/redaction、HTTP/WS envelope 或 static assets。

## 目标文件

- Create: `src/sona/ui/app_context.py`
- Create: `src/sona/ui/http_routes.py`
- Create: `src/sona/ui/websocket_routes.py`
- Modify: `src/sona/ui/server.py`
- Modify: `src/sona/meeting/api.py`
- Modify: `src/sona/meeting/inner_os/api.py`
- Create: `tests/test_ui_app_context.py`
- Modify: `tests/test_ui_server.py`
- Modify: `tests/test_meeting_api.py`
- Modify: `tests/test_inner_os_api.py`

## Task 1: 固化 app assembly、路由和生命周期

**Files:**

- Create: `tests/test_ui_app_context.py`
- Modify: `tests/test_ui_server.py`
- Modify: `tests/test_meeting_api.py`
- Modify: `tests/test_inner_os_api.py`

- [ ] **Step 1: 增加完整 route set 测试**

测试 `create_app(..., initialize_meeting=False)` 的 HTTP/WS path 集合、static route 最后、CORS/security header、SpeechRail voices/speech proxy、runtime unavailable close code 和 request/origin policy。

- [ ] **Step 2: 增加生命周期顺序测试**

用 fakes 记录正常 startup/shutdown、meeting init 失败、runtime start 失败、部分 meeting init 后失败与重复 close。每个已创建资源最多关闭一次，未创建资源不得关闭。

```python
async def test_context_closes_resources_in_current_order() -> None:
    calls: list[str] = []
    context = fake_context(calls)
    await context.close()
    assert calls == [
        "runtime.stop",
        "inner_os.close",
        "summary.stop",
        "scheduler.close",
        "repository.close",
    ]
```

- [ ] **Step 3: 运行基线**

```bash
SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run --extra dev pytest \
  tests/test_ui_server.py tests/test_meeting_api.py tests/test_inner_os_api.py \
  -q --no-cov
```

预期：现有测试通过；新 context 测试因模块尚不存在而失败。

## Task 2: 建立单一 typed UIAppContext

**Files:**

- Create: `src/sona/ui/app_context.py`
- Modify: `src/sona/ui/server.py`
- Create: `tests/test_ui_app_context.py`
- Modify: `tests/test_ui_server.py`

- [ ] **Step 1: 定义 context 与唯一 accessor**

```python
@dataclass(slots=True)
class UIAppContext:
    settings: Settings
    meeting_events: MeetingEventBroadcaster
    accepted_control_tasks: set[asyncio.Task[dict[str, object]]]
    runtime: UIRuntime | None = None
    meeting_repository: MeetingRepository | None = None
    meeting_session: MeetingWorkload | None = None
    meeting_summary_service: MeetingSummaryService | None = None
    inference_scheduler: LocalInferenceScheduler | None = None
    inner_os_service: InnerOSQueryService | None = None
    inner_os_exchange_repository: InnerOSExchangeRepository | None = None
    meeting_backend_error: str | None = None

    async def close(self) -> None: ...


def attach_app_context(app: FastAPI, context: UIAppContext) -> None: ...
def get_app_context(app: FastAPI) -> UIAppContext: ...
```

实现中使用 meeting 计划的窄 protocol，避免不必要 concrete type；上述 concrete 名称只用于确实由 composition root 创建的 service。accessor 读取单一 `app.state.sona_context` 并在缺失/类型错误时 fail closed。

- [ ] **Step 2: 迁移 lifespan 与 backend initialization**

`initialize_meeting_backend(context) -> bool` 只更新 context 字段；不再散写多个 `app.state.*`。失败时关闭本次已创建的 summary/repository/scheduler，设置 `meeting_backend_error=type(exc).__name__`，不记录 DSN/token。

- [ ] **Step 3: 更新测试注入**

测试从 `app.state.runtime = fake` 改为 `get_app_context(app).runtime = fake`；不保留两套 runtime 字段作为兼容层。

- [ ] **Step 4: 运行并提交 context**

```bash
uv run --extra dev pytest tests/test_ui_app_context.py tests/test_ui_server.py -q --no-cov
uv run --extra dev ruff check src/sona/ui/app_context.py src/sona/ui/server.py \
  tests/test_ui_app_context.py tests/test_ui_server.py
uv run --extra dev mypy src/sona/ui
git add src/sona/ui/app_context.py src/sona/ui/server.py \
  tests/test_ui_app_context.py tests/test_ui_server.py
git commit -m "refactor: add typed ui app context"
```

## Task 3: 提取 HTTP routes，保留 SpeechRail proxy

**Files:**

- Create: `src/sona/ui/http_routes.py`
- Modify: `src/sona/ui/server.py`
- Modify: `tests/test_ui_server.py`

- [ ] **Step 1: 定义单一 HTTP router factory**

```python
def create_http_router(context: UIAppContext) -> APIRouter:
    """Build health/services/runtime and SpeechRail voices/speech proxy routes."""
```

router 拥有五个现有 path；health/service probes 继续使用 bounded timeout 与脱敏 diagnostics。SpeechRail proxy 继续从 settings 派生 URL、转发必要 Authorization、拒绝任意远程 target、保留 content type/status/error redaction。

- [ ] **Step 2: 从 server 删除 HTTP endpoint closures**

`server.create_app` 只 `include_router(create_http_router(context))`；static mount 仍最后执行。不要把 meeting/inner-OS routes 合并到该 router。

- [ ] **Step 3: 运行并提交 HTTP routes**

```bash
uv run --extra dev pytest tests/test_ui_server.py -q --no-cov
uv run --extra dev ruff check src/sona/ui/http_routes.py src/sona/ui/server.py
uv run --extra dev mypy src/sona/ui
git add src/sona/ui/http_routes.py src/sona/ui/server.py tests/test_ui_server.py
git commit -m "refactor: extract ui http routes"
```

## Task 4: 提取 WebSocket routes 与 control tasks

**Files:**

- Create: `src/sona/ui/websocket_routes.py`
- Modify: `src/sona/ui/server.py`
- Modify: `tests/test_ui_server.py`

- [ ] **Step 1: 定义 WebSocket router factory**

```python
def create_websocket_router(context: UIAppContext) -> APIRouter:
    """Build subtitle, assistant, meeting, inner-OS and v1 control sockets."""
```

所有 handler 直接引用 context/provider；accepted control tasks 存在 context，done callback 必须移除 task 并消费 exception。meeting snapshot helpers 接收 context 参数，不访问散落 app.state。

- [ ] **Step 2: 保留字幕 callback 模式和 single writer**

`/ws/subtitles` 继续向 `SubtitleProxy.add_client(websocket.send_text)` 注册；meeting/control sockets 保留现有 broadcaster queue、snapshot-first、revision 和 single-writer 语义。

- [ ] **Step 3: 运行并提交 WebSocket routes**

```bash
uv run --extra dev pytest tests/test_ui_server.py tests/asr/test_proxy_contract.py tests/test_runtime_mode.py -q --no-cov
uv run --extra dev ruff check src/sona/ui/websocket_routes.py src/sona/ui/server.py
uv run --extra dev mypy src/sona/ui
git add src/sona/ui/websocket_routes.py src/sona/ui/server.py \
  tests/test_ui_server.py tests/asr/test_proxy_contract.py tests/test_runtime_mode.py
git commit -m "refactor: extract ui websocket routes"
```

## Task 5: 给 meeting 与 inner-OS router 注入窄 providers

**Files:**

- Modify: `src/sona/meeting/api.py`
- Modify: `src/sona/meeting/inner_os/api.py`
- Modify: `src/sona/ui/server.py`
- Modify: `tests/test_meeting_api.py`
- Modify: `tests/test_inner_os_api.py`
- Modify: `tests/test_ui_server.py`

- [ ] **Step 1: 定义 meeting API providers**

```python
@dataclass(frozen=True, slots=True)
class MeetingAPIDependencies:
    repository: Callable[[], MeetingAPIRepository | None]
    runtime: Callable[[], MeetingRuntime | None]
    summary_service: Callable[[], SummaryService | None]
    event_publisher: Callable[[str, UUID, Mapping[str, object]], Awaitable[None]]
```

`create_meeting_router(..., dependencies=None)` 和 `install_meeting_api(..., dependencies=None)` 保留现有 explicit repository/runtime/summary 参数；explicit 参数构造成静态 provider，UI server 传 context-backed provider。删除逐字段 `getattr(request.app.state, ...)`。

- [ ] **Step 2: 定义 inner-OS providers**

`InnerOSAPIDependencies` 明确提供 settings、query service、meeting repository 和 exchange repository。standalone test 直接传 fake dependencies；UI server 传 context providers。exchange repository lazy create 时写回 context，而非新 app.state 字段。

- [ ] **Step 3: 运行 API 回归并提交**

```bash
SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run --extra dev pytest \
  tests/test_meeting_api.py tests/test_inner_os_api.py tests/test_ui_server.py -q --no-cov
uv run --extra dev ruff check src/sona/meeting/api.py \
  src/sona/meeting/inner_os/api.py src/sona/ui/server.py \
  tests/test_meeting_api.py tests/test_inner_os_api.py
uv run --extra dev mypy src
git add src/sona/meeting/api.py src/sona/meeting/inner_os/api.py \
  src/sona/ui/server.py tests/test_meeting_api.py tests/test_inner_os_api.py tests/test_ui_server.py
git commit -m "refactor: inject ui api dependencies"
```

## Task 6: 完整门禁

- [ ] **Step 1: 运行 UI/backend 聚焦矩阵**

```bash
SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run --extra dev pytest \
  tests/test_ui_app_context.py tests/test_ui_server.py tests/test_meeting_api.py \
  tests/test_inner_os_api.py tests/test_runtime_mode.py tests/asr/test_proxy_contract.py \
  -q --no-cov
```

- [ ] **Step 2: 运行项目门禁**

```bash
uv run --extra dev ruff check src tests
uv run --extra dev mypy src
git diff --check
```

- [ ] **Step 3: 审查 state 与安全边界**

搜索 source 中 dependency-style `app.state.*`：除单一 context attach/get 和与依赖无关的 FastAPI installer marker 外应为空。确认 CORS/Origin/Auth/redaction/static ordering 与路径集合不变。

## 完成标准

- [ ] `create_app` 签名和 HTTP/WS route set 不变。
- [ ] UI dependency state 只有一个 typed context，没有并行字段事实源。
- [ ] HTTP、WebSocket、meeting、inner-OS router 通过显式 context/provider 获得依赖。
- [ ] lifespan 正常、部分失败和重复 close 测试通过。
- [ ] SpeechRail proxy、安全 header、Origin policy、meeting envelope 和 static mount 行为不变。
