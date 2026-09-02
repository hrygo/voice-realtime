# 🤝 贡献指南 (Contributing to Sona)

感谢你对 **Sona** 开源项目的关注与支持！我们欢迎一切形式的贡献，包括提出 Issue、完善文档、报告 Bug、提出新特性或直接提交 Pull Request (PR)。

为了保证系统的稳定性、架构整洁性与高质量标准，请在参与贡献前阅读本指南。

---

## 🧭 核心架构与不可动摇的设计原则

Sona 坚持 **Clean Architecture（整洁架构）**，并锁定以下六大核心防线。**任何 PR 均不得削弱或绕过这些原则**：

1. **Python 3.12 严格锁定**：
   - 依赖与运行时锁定 `Python >=3.12,<3.13`；统一使用 [`uv`](https://docs.astral.sh/uv/) 与 PEP 621 元数据管理依赖。
2. **纯粹领域与无历史向后兼容包袱**：
   - 领域层（`sona.asr`、`sona.meeting`、`sona.subtitles`）严禁依赖基础设施实现；
   - 严格避免为了历史过渡而引入兼容 Shim 或胶水别名，保持代码库精炼纯洁。
3. **零音频落地与隐私优先 (Local & Privacy-First)**：
   - 数据库与磁盘**绝对不保存原始音频**，默认绑定 Loopback，不向外网发送任何遥测数据；
   - 故障恢复 Journal（`runtime/meetings/recovery/`）目录权限严格设为 `0700`、文件设为 `0600`。
4. **单 PCM 所有权 (Single PCM Owner)**：
   - 麦克风音频流由 `AudioHub` 独占采集；
   - 任意稳定时刻仅允许一个语音任务（`assistant` / `subtitles` / `meeting`）消费 PCM，由 `RuntimeModeCoordinator` 两阶段状态机统一仲裁。
5. **双层防回声死循环防线 (Echo Barrier)**：
   - **L1** `EchoSuppressionProcessor`：外放播报期间物理闭麦或基于快慢自适应包络的打断抑制（`echo_barge_in_gain=2.5`）；
   - **L2** `BotTextRecorder` + `SelfEchoFilter`：转写文本与播报内容相似度 $\ge 0.7$ 或子串覆盖时直接吞帧，切断自我回声死循环。
6. **LM Studio 原生状态链**：
   - 严格使用原生 `/api/v1/chat` + `reasoning: "off"` + `previous_response_id` 状态链，禁止改回 OpenAI 兼容端点或注入 `extra_body`。

---

## 🛠️ 本地开发环境准备

### 1. 克隆仓库与安装依赖

```bash
# 1. 克隆代码仓库
git clone https://github.com/your-username/sona.git
cd sona

# 2. 安装 Python 全量依赖（包含 interaction 与 dev 组）
uv sync --all-extras

# 3. 安装前端依赖
cd ui && npm install && cd ..

# 4. 下载 NLTK punkt_tab 分词数据（Pipecat TTS 断句必需）
bash scripts/install-nltk-data.sh
```

### 2. 准备外部运行依赖

- **PostgreSQL 14+**：执行 `psql knowledge -f scripts/bootstrap-meeting-db.sql` 初始化 `sona` schema 与应用角色；
- **SpeechRail**：启动独立 SpeechRail 服务（监听 `127.0.0.1:8201`）；
- **LM Studio**：启动本地服务器（监听 `127.0.0.1:1234`），加载推荐模型并确认 `reasoning` 可关闭。

---

## 🛡️ 质量门禁（提交 PR 前必须全绿）

本地提交前，**必须依次通过以下五重质量门禁**：

```bash
# 1. 后端单元与集成测试（需配置 SONA_TEST_DATABASE_URL；覆盖率硬性门禁 fail_under=80%）
SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/

# 2. Python 严格静态类型检查 (Strict mode，仅针对 src/)
uv run mypy src/

# 3. Python 代码风格与 Lint 检查
uv run ruff check src/ tests/

# 4. 前端单元与组件测试
cd ui && npm test -- --run

# 5. 前端静态检查与生产构建
cd ui && npm run build
```

---

## 📝 Git 提交规范 (Conventional Commits)

提交信息采用结构化规范，格式如下：

```text
<type>(<scope>): <中文简短描述>

[可选详细说明]
```

### Type 类型枚举

| 类型 | 说明 | 示例 |
|---|---|---|
| `feat` | 新增功能或特性 | `feat(meeting): 支持会议转录多说话人动态重命名` |
| `fix` | 修复缺陷或 Bug | `fix(interaction): 修复打断后自激回声自触发问题` |
| `docs` | 文档与注释更新 | `docs: 完善系统架构设计与快速上手指南` |
| `style` | 代码格式调整（不影响逻辑） | `style: 规范导入排序与全角标点注释` |
| `refactor`| 代码重构（不增加新功能也不修复 Bug） | `refactor(config): 模块化拆分集中配置为独立领域子包` |
| `perf` | 性能优化 | `perf(subtitles): 优化 PCM 快照重放内存占用` |
| `test` | 补充或重构测试用例 | `test(speechrail): 补充流式 ASR 异常断开重连单测` |
| `chore` | 构建、依赖或基础设施变动 | `chore: 升级 uv 依赖锁定至最新稳定版本` |

---

## 🚀 Pull Request (PR) 流程

1. **创建分支**：从最新的 `main` 分支拉取开发分支，分支命名推荐 `feature/<feature-name>` 或 `fix/<issue-name>`；
2. **小步提交**：保持单个 Commit 职责单一，便于 Code Review 与追溯；
3. **本地验证**：确保本地运行 `pytest`、`mypy`、`ruff`、前端测试和构建**全部绿灯**；
4. **提交 PR**：
   - 填写清晰的 PR 描述，说明本次修改的背景、解决方案与实测验证结果；
   - 关联相关 Issue（如有，例如 `Fixes #123`）；
5. **Code Review 与合并**：
   - 维护者将对代码进行严格的架构、性能与安全审查；
   - 审查通过并全量 CI 检查绿灯后，合并入主分支。
