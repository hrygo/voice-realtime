# Inner OS 深度 UI 优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变会议后端契约和问答业务逻辑的前提下，统一 Inner OS 的展示层、回答卡片层级、Token 和输入区布局。

**Architecture:** 保留 `InnerOSPanel` 负责会话编排，新增纯展示型 `InnerOSAnswerContent` 作为事实/判断/草稿内容层；完整答案卡片只用于实时结果，历史和未保存托盘使用紧凑摘要与可展开内容。样式继续复用全局 Voice Studio Token，并将 Inner OS 的语义别名限定在自身容器范围内。

**Tech Stack:** React 19、TypeScript、Zustand、CSS、Vitest、Vite。

**Spec:** `docs/superpowers/specs/2026-08-27-meeting-inner-os-design.md`

## Global Constraints

- 不改变会议后端接口、WebSocket 契约、会议转录事实源和存储边界。
- 不把临时目标、议程或背景写入持久化 store、localStorage 或数据库。
- 不新增 UI 依赖；复用现有图标组件和全局设计 Token。
- 不重置、覆盖或整理当前工作树中的无关未提交改动。
- 所有新的可见行为先写组件测试，再实现，再运行聚焦测试。

---

### Task 1: 抽离回答内容层并消除嵌套卡片

**Files:**
- Create: `ui/src/features/innerOS/InnerOSAnswerContent.tsx`
- Create: `ui/src/features/innerOS/InnerOSAnswerContent.test.tsx`
- Modify: `ui/src/features/innerOS/InnerOSAnswerCard.tsx`
- Modify: `ui/src/features/innerOS/InnerOSUnsavedTray.tsx`
- Modify: `ui/src/features/innerOS/InnerOSHistoryTab.tsx`
- Modify: `ui/src/features/innerOS/InnerOSAnswerCard.test.tsx`
- Create: `ui/src/features/innerOS/InnerOSAnswerContent.test.tsx`

**Interfaces:**
- `InnerOSAnswerContent` 接收 `answer`、`onSelectEvidence` 和可选的 `compact`，只渲染事实、判断、草稿、限制说明。
- `InnerOSAnswerCard` 继续负责问题头部、保存/追问操作和完整卡片边界。
- 历史与未保存托盘通过摘要行展开 `InnerOSAnswerContent`，不得再渲染完整 `InnerOSAnswerCard`。

- [ ] **Step 1: 写回答内容层失败测试**

  覆盖事实、判断、草稿、限制说明均能展示；证据点击继续传递 `segment_id`；紧凑模式不渲染保存和追问操作。

- [ ] **Step 2: 运行聚焦测试确认 RED**

  Run: `cd ui && npm test -- --run src/features/innerOS/InnerOSAnswerContent.test.tsx`

  Expected: 新测试因组件尚不存在而失败。

- [ ] **Step 3: 实现 `InnerOSAnswerContent` 并让完整卡片复用它**

  将现有答案卡片中的四个 tier 区块、证据映射和草稿复制行为拆分为展示内容组件；复制状态仍由拥有按钮的完整卡片维护。

- [ ] **Step 4: 将历史和未保存托盘改为摘要 + 内容层**

  保留问题、状态、时间、保存、删除和展开按钮；展开区域只渲染 `InnerOSAnswerContent`，不产生第二个卡片头部或第二组保存/追问操作。

- [ ] **Step 5: 运行回答相关测试并提交逻辑切片**

  Run: `cd ui && npm test -- --run src/features/innerOS/InnerOSAnswerContent.test.tsx src/features/innerOS/InnerOSAnswerCard.test.tsx src/features/innerOS/InnerOSPanel.test.tsx`

  Expected: 回答内容、完整卡片、暂存摘要和面板行为全部通过。

### Task 2: 收敛 Token、排版和 Composer 布局

**Files:**
- Create: `ui/src/features/innerOS/InnerOSTokens.css`
- Create: `ui/src/features/innerOS/InnerOSAnswerCard.css`
- Create: `ui/src/features/innerOS/InnerOSArchive.css`
- Modify: `ui/src/features/innerOS/InnerOSPanel.css`
- Modify: `ui/src/features/innerOS/InnerOSPanel.tsx`
- Modify: `ui/src/components/Icons.tsx`
- Modify: `ui/src/index.css`
- Modify: `ui/src/features/innerOS/InnerOSPanel.test.tsx`

**Interfaces:**
- 所有 Inner OS 颜色、字号、间距、圆角和阴影通过容器作用域内的语义 Token 使用。
- Composer 保持现有提交逻辑，但输入框和发送按钮共享明确的布局基线与最小尺寸。

- [ ] **Step 1: 写布局和冗余可见性测试**

  断言面板默认不显示 `v1`，侧边折叠按钮仍带 `⌘K` accessible name，输入区包含语义标签和发送按钮；回答区域不出现嵌套完整卡片。

- [ ] **Step 2: 运行聚焦测试确认当前差异**

  Run: `cd ui && npm test -- --run src/features/innerOS/InnerOSPanel.test.tsx`

  Expected: 新增的 `v1` 隐藏或结构断言至少有一项失败，证明测试捕获目标行为。

- [ ] **Step 3: 限定语义 Token 作用域并统一排版层级**

  将 Inner OS 变量挂载到 `.inner-os-panel`、`.inner-os-unsaved-tray` 和 `.inner-os-history-tab` 作用域；保留全局 Token 为唯一颜色来源，组件规则不再新增直接色值。统一标题、正文、辅助文字和徽标四档字号。

- [ ] **Step 4: 重排 Composer 组合布局**

  使用 `grid-template-columns: minmax(0, 1fr) 48px` 约束输入框与发送按钮，按钮高度与输入区域对齐；窄屏改为上下堆叠，快捷键提示只在输入区焦点或生成中显示。

- [ ] **Step 5: 运行测试与生产构建**

  Run: `cd ui && npm test -- --run src/features/innerOS/InnerOSPanel.test.tsx`

  Run: `cd ui && npm run build`

  Expected: 面板行为和 TypeScript/Vite 构建通过。

### Task 3: 完成交互可访问性与样式清理

**Files:**
- Modify: `ui/src/features/innerOS/InnerOSPanel.tsx`
- Modify: `ui/src/features/innerOS/InnerOSQuickPills.tsx`
- Modify: `ui/src/features/innerOS/InnerOSEphemeralContext.tsx`
- Modify: `ui/src/features/innerOS/InnerOSHistoryTab.tsx`
- Modify: `ui/src/features/innerOS/InnerOSUnsavedTray.tsx`
- Modify: `ui/src/features/innerOS/InnerOSPanel.css`
- Modify: `ui/src/features/innerOS/InnerOSPanel.test.tsx`

**Interfaces:**
- 状态变化使用 `role="status"` / `role="alert"` / `aria-live` 表达，不增加常驻长文案。
- 快捷键只作为对应控件的 tooltip 或聚焦提示，不再作为顶部独立按钮。

- [ ] **Step 1: 写键盘和状态反馈测试**

  覆盖侧边按钮、临时上下文、重点筛选、取消、重试和输入区的可访问名称、展开状态、禁用状态及状态播报。

- [ ] **Step 2: 补齐状态语义和焦点样式**

  为连接状态和生成状态提供稳定的 live region；确保所有图标按钮有 accessible name，焦点环不依赖 hover。

- [ ] **Step 3: 删除可见 V1 与重复提示**

  版本信息不再进入普通用户视图；安全说明只在异常时显示；输入快捷键改为上下文提示；不删除快捷问题、意图选择和重点筛选。

- [ ] **Step 4: 清理遗留选择器并运行聚焦测试**

  删除不再被使用的嵌套卡片和旧布局规则，确认 `rg` 不再找到对应孤儿 class；运行 Inner OS 全部测试。

  Run: `cd ui && npm test -- --run src/features/innerOS`

### Task 4: 全量验证与变更审查

**Files:**
- Verify: `ui/src/features/innerOS/`
- Verify: `ui/src/components/meeting/`

- [ ] **Step 1: 运行前端全量测试**

  Run: `cd ui && npm test -- --run`

  Expected: 所有测试通过；既有 jsdom canvas warning 若仍出现，单独记录为测试环境噪声，不扩大结论。

- [ ] **Step 2: 运行生产构建和差异检查**

  Run: `cd ui && npm run build`

  Run: `git diff --check`

  Expected: 构建成功且没有空白/冲突标记问题。

- [ ] **Step 3: 做五轴审查**

  按正确性、可读性、架构、安全、性能检查变更；确认没有改动会议后端契约、没有引入依赖、没有把临时上下文写入持久化层。

- [ ] **Step 4: 输出变更清单和未触碰范围**

  明确列出实际修改的 Inner OS 文件、验证命令、仍无法由 jsdom 证明的真实浏览器布局风险，以及当前工作树中保留的无关改动。
