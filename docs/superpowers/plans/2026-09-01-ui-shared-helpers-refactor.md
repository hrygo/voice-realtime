---
title: "前端共享 Helper 重构实施计划"
status: draft
type: execution_plan
date: 2026-09-01
owners: ["sona-core"]
related_documents:
  - "docs/architecture/系统总体架构与详细设计方案.md"
---

# UI Shared Helpers Refactor Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task, with a review checkpoint after each commit.

**Goal:** 只收敛前端已经确认完全同构的时间格式化、expanded-block Set 切换和 JSON HTTP error parsing，保留秒/毫秒、长时显示、业务 schema、下载与 UX 差异。

**Architecture:** `shared/duration.ts` 提供单位和显示规则明确的纯函数；`shared/useToggleSet.ts` 只管理 immutable Set state；`services/http.ts` 统一成功/204/稳定 error envelope 解析。组件和业务 API 仍拥有 endpoint、query/body、toast、下载/clipboard 与 presentation。

**Tech Stack:** React 19、TypeScript 5.8、Vite 7、Vitest 3、jsdom。

---

## 执行边界

- 工作目录：`/Users/hrygo/Documents/sona`
- 可独立执行，但不得与其他任务同时修改相同 UI 文件。
- 真实路径是 `ui/src/App.tsx`、`ui/src/features/innerOS/InnerOSEvidenceItem.tsx` 和 `ui/src/components/meeting/*`；不存在 `ui/src/components/App.tsx` 或 `ui/src/components/InnerOSEvidenceItem.tsx`。
- 当前存在三种必须显式保留的时间语义：App 的 seconds→累计分钟 `mm:ss`；Inner OS evidence 的 milliseconds→累计分钟 `mm:ss`；meeting/status 的 seconds→超过一小时显示 `hh:mm:ss`。
- `MeetingHistorySidebar.tsx` 与 `MeetingPanel.tsx` 当前从 `MeetingRecordingView.tsx` 导入 `formatElapsed`；移动 helper 时必须一并更新，避免组件反向成为 utility module。
- `innerOS/api.ts` 与 `meetingApi.ts` 的 `handleResponse` 完全重复，可共享；各自 URL、schema、idempotency、export blob 和业务方法不合并。
- clipboard 已有 `ui/src/utils/clipboard.ts`，且不同组件的 toast/timeout/metrics 不同；本计划不再抽象 clipboard。
- 不改变文案、timer tick、HTTP headers、export filename、toast timing、ARIA、CSS class 或 store shape。

## 目标文件

- Create: `ui/src/shared/duration.ts`
- Create: `ui/src/shared/duration.test.ts`
- Create: `ui/src/shared/useToggleSet.ts`
- Create: `ui/src/shared/useToggleSet.test.tsx`
- Create: `ui/src/services/http.ts`
- Create: `ui/src/services/http.test.ts`
- Modify: `ui/src/App.tsx`
- Modify: `ui/src/App.test.tsx`
- Modify: `ui/src/features/innerOS/InnerOSEvidenceItem.tsx`
- Modify: `ui/src/components/StatusBar.tsx`
- Modify: `ui/src/components/StatusBar.test.ts`
- Modify: `ui/src/components/meeting/MeetingRecordingView.tsx`
- Modify: `ui/src/components/meeting/MeetingTranscriptViewer.tsx`
- Modify: `ui/src/components/meeting/MeetingHistorySidebar.tsx`
- Modify: `ui/src/components/meeting/MeetingPanel.tsx`
- Modify: `ui/src/components/meeting/MeetingComponents.test.tsx`
- Modify: `ui/src/features/innerOS/api.ts`
- Modify: `ui/src/features/innerOS/api.test.ts`
- Modify: `ui/src/services/meetingApi.ts`
- Modify: `ui/src/services/meetingApi.test.ts`

## Task 1: 提取单位明确的 duration functions

**Files:**

- Create: `ui/src/shared/duration.ts`
- Create: `ui/src/shared/duration.test.ts`
- Modify: `ui/src/App.tsx`
- Modify: `ui/src/App.test.tsx`
- Modify: `ui/src/features/innerOS/InnerOSEvidenceItem.tsx`
- Modify: `ui/src/components/StatusBar.tsx`
- Modify: `ui/src/components/StatusBar.test.ts`
- Modify: `ui/src/components/meeting/MeetingRecordingView.tsx`
- Modify: `ui/src/components/meeting/MeetingHistorySidebar.tsx`
- Modify: `ui/src/components/meeting/MeetingPanel.tsx`
- Modify: `ui/src/components/meeting/MeetingComponents.test.tsx`

- [ ] **Step 1: 写边界红灯测试**

覆盖现有有效输入域的 0、59、60、3599、3600、3665 秒和相应毫秒。测试分别锁定累计分钟与自动小时语义，避免一次 DRY 改变一小时后的显示；本结构任务不新增负数/NaN/Infinity 的用户可见规则。

- [ ] **Step 2: 实现三个显式 API，共享内部 primitive**

```typescript
export function formatMinutesSecondsFromSeconds(totalSeconds: number): string;
export function formatMinutesSecondsFromMilliseconds(milliseconds: number): string;
export function formatElapsedClockSeconds(totalSeconds: number): string;
```

前两个输出累计分钟 `mm:ss`；第三个在小时为 0 时输出 `mm:ss`，否则 `hh:mm:ss`。沿用现有 `Math.floor` 和整数 timer 输入，不新增 clamp/throw；不要让 milliseconds 隐式传给 seconds API。

- [ ] **Step 3: 更新全部消费者**

App 使用 seconds→累计分钟；InnerOSEvidenceItem 使用 milliseconds；StatusBar、MeetingRecordingView、MeetingHistorySidebar、MeetingPanel 使用 elapsed clock。删除从 MeetingRecordingView 导出 utility 的做法。

- [ ] **Step 4: 运行并提交 duration**

```bash
npm --prefix ui test -- --run \
  src/shared/duration.test.ts \
  src/App.test.tsx \
  src/components/StatusBar.test.ts \
  src/components/meeting/MeetingComponents.test.tsx
npm --prefix ui run build
git add ui/src/shared/duration.ts ui/src/shared/duration.test.ts \
  ui/src/App.tsx ui/src/App.test.tsx ui/src/features/innerOS/InnerOSEvidenceItem.tsx \
  ui/src/components/StatusBar.tsx ui/src/components/StatusBar.test.ts \
  ui/src/components/meeting/MeetingRecordingView.tsx \
  ui/src/components/meeting/MeetingHistorySidebar.tsx ui/src/components/meeting/MeetingPanel.tsx \
  ui/src/components/meeting/MeetingComponents.test.tsx
git commit -m "refactor: centralize ui duration formatting"
```

## Task 2: 提取 expanded block 的 immutable Set hook

**Files:**

- Create: `ui/src/shared/useToggleSet.ts`
- Create: `ui/src/shared/useToggleSet.test.tsx`
- Modify: `ui/src/components/meeting/MeetingRecordingView.tsx`
- Modify: `ui/src/components/meeting/MeetingTranscriptViewer.tsx`
- Modify: `ui/src/components/meeting/MeetingComponents.test.tsx`

- [ ] **Step 1: 写 hook 红灯测试**

用 React `createRoot` test harness 验证初值 copy、toggle add/remove、clear、每次更新返回新 Set、callback identity 稳定。不要增加新的 testing-library dependency。

- [ ] **Step 2: 实现最小 hook**

```typescript
export interface ToggleSetState<T> {
  readonly values: ReadonlySet<T>;
  readonly toggle: (value: T) => void;
  readonly clear: () => void;
}

export function useToggleSet<T>(initial: Iterable<T> = []): ToggleSetState<T>;
```

内部始终 `new Set(previous)`，不原地 mutate；`initial` 只在首次 render 读取。

- [ ] **Step 3: 只替换 block expansion**

MeetingRecordingView 与 MeetingTranscriptViewer 的 `expandedBlockIds/toggleBlockExpand` 使用 hook。starred segments 含 controlled/uncontrolled、toast 与外部 callback 差异，不在本任务合并。

- [ ] **Step 4: 运行并提交 hook**

```bash
npm --prefix ui test -- --run \
  src/shared/useToggleSet.test.tsx \
  src/components/meeting/MeetingComponents.test.tsx
npm --prefix ui run build
git add ui/src/shared/useToggleSet.ts ui/src/shared/useToggleSet.test.tsx \
  ui/src/components/meeting/MeetingRecordingView.tsx \
  ui/src/components/meeting/MeetingTranscriptViewer.tsx \
  ui/src/components/meeting/MeetingComponents.test.tsx
git commit -m "refactor: share meeting expansion state"
```

## Task 3: 统一 JSON response/error parsing

**Files:**

- Create: `ui/src/services/http.ts`
- Create: `ui/src/services/http.test.ts`
- Modify: `ui/src/features/innerOS/api.ts`
- Modify: `ui/src/features/innerOS/api.test.ts`
- Modify: `ui/src/services/meetingApi.ts`
- Modify: `ui/src/services/meetingApi.test.ts`

- [ ] **Step 1: 写 response matrix 红灯测试**

覆盖 2xx JSON、204、stable `{error:{code,message,request_id,details}}`、FastAPI `{detail:string}`、非 JSON error、空 success body（除 204 外应保持当前 JSON parse failure）。断言仍抛现有 `ApiError`。

- [ ] **Step 2: 实现共享 parser 与 request helper**

```typescript
export async function parseJsonResponse<T>(response: Response): Promise<T>;

export async function requestJson<T>(
  input: RequestInfo | URL,
  init?: RequestInit,
  fetcher: typeof fetch = fetch,
): Promise<T>;
```

`parseJsonResponse` 完整复制当前两份 `handleResponse` 的成功/204/error 语义；`requestJson` 只调用 fetch + parser。`fetcher` 仅用于 deterministic test，不成为全局 mutable client。

- [ ] **Step 3: 更新两个业务 API**

innerOS 与 meeting methods 使用 `requestJson<T>`；保留 encode、query、headers、body 与 idempotency key。`downloadExport` 仍自己 fetch blob，但非 2xx 时调用 `parseJsonResponse` 复用错误映射。

- [ ] **Step 4: 运行并提交 HTTP helper**

```bash
npm --prefix ui test -- --run \
  src/services/http.test.ts \
  src/features/innerOS/api.test.ts \
  src/services/meetingApi.test.ts
npm --prefix ui run build
git add ui/src/services/http.ts ui/src/services/http.test.ts \
  ui/src/features/innerOS/api.ts ui/src/features/innerOS/api.test.ts \
  ui/src/services/meetingApi.ts ui/src/services/meetingApi.test.ts
git commit -m "refactor: share ui json response parsing"
```

## Task 4: 前端完整门禁与重复检查

- [ ] **Step 1: 运行全部前端测试和构建**

```bash
npm --prefix ui test -- --run
npm --prefix ui run build
git diff --check
```

- [ ] **Step 2: 人工核对语义差异**

确认一小时后的 App/meeting 显示、milliseconds evidence、starred behavior、HTTP 204、export blob、error message/request ID/details、toast/clipboard 均与重构前一致。

- [ ] **Step 3: 检查未扩大范围**

不修改 CSS、stores、协议 type、API endpoint、clipboard utility 或 package dependencies；不把不同业务 API 合并为一个 service。

## 完成标准

- [ ] 时间 helper 的单位和小时策略由函数名/测试显式表达。
- [ ] meeting block expansion 使用一个 immutable hook，starred behavior 保持独立。
- [ ] innerOS/meeting API 共用 JSON/error parser，业务 schema 与 export 仍分离。
- [ ] `npm --prefix ui test -- --run` 和 `npm --prefix ui run build` 通过。
- [ ] 没有错误路径、错误文件名或 `cd ui` 导致的后续工作目录问题。
