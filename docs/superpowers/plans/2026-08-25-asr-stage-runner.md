---
title: "ASR Stage 2–5 执行器实施计划"
description: "实现 Stage 2-5 统一 Runner 与自动决策生成的执行任务清单"
status: implemented
type: execution_plan
category: asr
version: "v1.0.0"
date: 2026-08-25
last_updated: 2026-08-27
author: "Voice Realtime Core Team"
owners:
  - "sona-core"
tags:
  - execution-plan
  - asr
  - stage-runner
---

# ASR Stage 2–5 统一执行器实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个可注入真实运行时、默认串行、可验证输入与证据链的 Stage 2–5 统一执行器核心，并用合成 executor 证明 Screen→Confirm、Stage 3/5 会话复用、故障注入、失败保留和 Promote 边界。

**Architecture:** `run_stage()` 独占生命周期、资源锁、输入游标、状态机和制品封存；`StageExecutor` 只适配具体系统，`StagePolicy` 只用纯函数解释 observation。正式决策在 run 封存后由 `verify_stage_decision()` 重新打开源文件、重算 hash 并构造，synthetic/experimental 证据永远不能 Promote。

**Tech Stack:** Python 3.12、asyncio、Pydantic v2、POSIX `flock`、SHA-256、原子文件写入、pytest、uv、mypy strict、ruff。

**Spec:** `docs/superpowers/specs/2026-08-25-asr-stage-runner-design.md`

## Global Constraints

- Python 严格锁定 3.12；不增加依赖。
- 模型、PCM、reference、逐字稿、运行与决策制品全部位于 Git 工作树外。
- runner 只读取显式传入路径，不扫描语料目录，不读取 reference，不隐式下载、迁移或删除模型。
- formal 运行拒绝 synthetic executor；CLI 不提供 synthetic fallback。
- Stage 2/4 Screen→Confirm 必须同 executor、同 session、同身份、连续 cursor，`start_count == 1`。
- 会议 candidate 固定为一次 `stage=5, covered_stages=(3, 5)` 的连续 `3_600_000 ms` 物理运行。
- Stage 5 固定 `3 disconnect + 1 asr_crash + 1 finalization_delay`，Promote 只统计已应用且恢复成功的故障。
- `exclusive_resource_lock()` 从任何输出目录创建、模型加载或服务启动前持有，直到 executor 清理和 `ArtifactIndex` 封存完成。
- 模型、服务、实验、后端/前端全量测试严格串行；只读审查可以并行，但不同 worker 不得同时修改同一文件。
- 目录权限 `0700`，文件权限 `0600`；拒绝 symlink、路径穿越、覆盖和 run resume。
- 保留双层回声防线、会议零音频持久化、EOF 冲刷和生产默认后端；本计划不实现生产运行时切换。
- 每个任务严格 red → green → targeted regression → commit；提交格式使用中文 Conventional Commit。
- 每个 commit 前必须重新完成下方 Mandatory Per-Commit Gate；定点测试不能替代完整门禁。

## Mandatory Per-Commit Gate

每个 Task 的 commit step 前严格顺序运行，任何一项失败都停止提交并修复；不得与模型、服务、正式实验
或另一个 gate 并行：

```bash
SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/
uv run mypy src/
uv run ruff check src/ tests/
cd ui && npm test -- --run
cd ui && npm run build
```

Expected: 后端测试全部通过且 branch coverage ≥80%；mypy/ruff 无错误；前端 11 个 test files、62 个 tests
或更多全部通过；production build 成功。每次 gate 后回到项目根目录再执行 `git add/commit`。

## File Structure

| 路径 | 责任 |
|:---|:---|
| `src/sona/benchmarks/asr/stage_contracts.py` | Stage 输入、lineage、状态、gate、selection 与最终报告的不可变 schema |
| `src/sona/benchmarks/asr/stage_inputs.py` | 显式输入 manifest 加载、项目外路径解析、字节/hash/时长验证和确定帧/action 流 |
| `src/sona/benchmarks/asr/stage_artifacts.py` | 私有 run 目录、原子 snapshot、JSONL/CSV、失败保留和 `ArtifactIndex` 最终封存 |
| `src/sona/benchmarks/asr/stage_executors.py` | `StageExecutor` Protocol、capabilities、observations 与显式 registry |
| `src/sona/benchmarks/asr/stage_evaluators.py` | Screen/Confirm、Stage 3 checkpoint 与 Stage 5 纯函数 policy |
| `src/sona/benchmarks/asr/stage_runner.py` | 唯一资源锁 owner、状态机、schedule/cursor、executor 生命周期、故障编排 |
| `src/sona/benchmarks/asr/stage_decision.py` | 打开并验证封存制品、上游报告、finalist selection，生成 `StageDecisionReport` |
| `src/sona/benchmarks/resource_lock.py` | 现有 flock 以及清理失败后的项目外 resource quarantine |
| `src/sona/benchmarks/asr/cli.py` | `run-stage` 与 `decide-stage` 边界，不重复持锁 |
| `tests/benchmarks/asr_stage_fakes.py` | 仅测试使用的 deterministic synthetic executor/policy 工具 |
| `tests/benchmarks/test_asr_stage_*.py` | 对应模块的单元与合成集成测试 |
| `docs/Fun-ASR与现有ASR后端科学对比测试方案.md` | 新 CLI、状态、制品和 synthetic 非正式边界 |

---

### Task 1: 扩展 Stage 执行与证据契约

**Files:**
- Modify: `src/sona/benchmarks/asr/stage_contracts.py`
- Modify: `tests/benchmarks/test_asr_stage_contracts.py`

**Interfaces:**
- Consumes: 现有 `ScheduleManifest`、`FaultPlan`、`StageRunManifest`、`ArtifactIndex`、`StageDecisionReport`。
- Produces: `StageModelManifest`、`StageInputManifest`、`PCMInputBinding`、`InteractionScriptBinding`、`StageRunState`、`StageEligibilityEvidence`、`StageGateEvidenceBundle`、`FinalistSelectionEvidence`，以及扩展后的 `StageRunManifest.covered_stages/evidence_tier/executor_id`。

- [ ] **Step 1: 写 lineage、输入判别联合与 fault 终点语义的失败测试**

```python
def _hash(character: str) -> str:
    return character * 64


def _stage_manifest(
    *,
    stage: StageNumber,
    covered_stages: tuple[StageNumber, ...],
    evidence_tier: EvidenceTier,
) -> StageRunManifest:
    return StageRunManifest(
        run_id=f"stage{stage}-meeting-fun",
        stage=stage,
        covered_stages=covered_stages,
        family_id="meeting",
        arm="finalist",
        candidate_id="fun",
        evidence_tier=evidence_tier,
        executor_id="meeting-test",
        git_commit="1" * 40,
        model_sha256=_hash("a"),
        profile_sha256=_hash("b"),
        runtime_config_sha256=_hash("c"),
        schedule_sha256=_hash("d"),
        fault_plan_sha256=_hash("e") if stage == 5 else None,
        started_at=datetime(2026, 8, 25, tzinfo=UTC),
        status="planned",
    )


def _fault_plan(*, delay_cursor_ms: int, delay_duration_ms: int) -> FaultPlan:
    return FaultPlan(
        stage=5,
        duration_ms=3_600_000,
        events=(
            FaultEvent(event_id="d1", cursor_ms=600_000, kind="disconnect"),
            FaultEvent(event_id="d2", cursor_ms=1_200_000, kind="disconnect"),
            FaultEvent(event_id="crash", cursor_ms=1_800_000, kind="asr_crash"),
            FaultEvent(event_id="d3", cursor_ms=2_400_000, kind="disconnect"),
            FaultEvent(
                event_id="delay",
                cursor_ms=delay_cursor_ms,
                kind="finalization_delay",
                duration_ms=delay_duration_ms,
            ),
        ),
    )


def test_meeting_stage5_is_the_only_multi_stage_lineage() -> None:
    manifest = _stage_manifest(stage=5, covered_stages=(3, 5), evidence_tier="formal")
    assert manifest.covered_stages == (3, 5)
    with pytest.raises(ValidationError, match="covered_stages"):
        _stage_manifest(stage=4, covered_stages=(3, 4), evidence_tier="formal")


def test_stage_input_manifest_requires_one_binding_per_segment() -> None:
    manifest = StageInputManifest(
        schedule_sha256=_hash("a"),
        bindings=(
            PCMInputBinding(
                segment_id="screen-001",
                relative_path="pcm/screen-001.pcm",
                input_sha256=_hash("b"),
                size_bytes=32_000,
                duration_ms=1_000,
                sample_rate_hz=16_000,
                channels=1,
                sample_format="s16le",
            ),
        ),
    )
    assert manifest.bindings[0].kind == "pcm"


def test_model_manifest_rejects_duplicate_relative_paths() -> None:
    model_file = StageModelFile(
        relative_path="weights/model.bin",
        sha256=_hash("f"),
        size_bytes=8,
    )
    with pytest.raises(ValidationError, match="model file paths must be unique"):
        StageModelManifest(
            model_id="test/model",
            model_revision="immutable-revision",
            files=(model_file, model_file),
        )


def test_finalization_delay_occurs_at_eof_and_uses_wall_duration() -> None:
    plan = _fault_plan(delay_cursor_ms=3_600_000, delay_duration_ms=5_000)
    assert plan.events[-1].kind == "finalization_delay"
    with pytest.raises(ValidationError, match="finalization_delay"):
        _fault_plan(delay_cursor_ms=3_599_999, delay_duration_ms=5_000)
```

- [ ] **Step 2: 运行契约测试并确认因新类型/字段不存在而失败**

Run: `uv run pytest tests/benchmarks/test_asr_stage_contracts.py -q`

Expected: FAIL，导入 `StageInputManifest` 或构造 `covered_stages` 失败。

- [ ] **Step 3: 增加输入、lineage、状态和 gate schema**

```python
StageNumber = Literal[2, 3, 4, 5]
EvidenceTier = Literal["formal", "experimental"]
StageStatus = Literal["planned", "running", "completed", "failed", "deferred"]
StagePhase = Literal["planned", "preflight", "screen", "confirm", "reliability", "finalizing", "terminal"]
SchedulePurpose = Literal["screen", "confirm", "system", "interaction", "reliability"]


class PCMInputBinding(_FrozenModel):
    kind: Literal["pcm"] = "pcm"
    segment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    relative_path: str
    input_sha256: str
    size_bytes: int = Field(gt=0)
    duration_ms: int = Field(gt=0)
    sample_rate_hz: Literal[16_000] = 16_000
    channels: Literal[1] = 1
    sample_format: Literal["s16le"] = "s16le"


class StageModelFile(_FrozenModel):
    relative_path: str
    sha256: str
    size_bytes: int = Field(gt=0)


class StageModelManifest(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    model_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    files: tuple[StageModelFile, ...] = Field(min_length=1)


class InteractionAssetBinding(_FrozenModel):
    asset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    relative_path: str
    input_sha256: str
    size_bytes: int = Field(gt=0)
    duration_ms: int = Field(gt=0)
    sample_rate_hz: Literal[16_000] = 16_000
    channels: Literal[1] = 1
    sample_format: Literal["s16le"] = "s16le"


class InteractionScriptBinding(_FrozenModel):
    kind: Literal["interaction_script"] = "interaction_script"
    segment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    relative_path: str
    input_sha256: str
    size_bytes: int = Field(gt=0)
    duration_ms: int = Field(gt=0)
    assets: tuple[InteractionAssetBinding, ...] = Field(min_length=1)


StageInputBinding = Annotated[
    PCMInputBinding | InteractionScriptBinding,
    Field(discriminator="kind"),
]


class StageInputManifest(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    schedule_sha256: str
    bindings: tuple[StageInputBinding, ...] = Field(min_length=1)


class StageRunState(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    status: StageStatus
    phase: StagePhase
    cursor_ms: int = Field(ge=0)
    start_count: int = Field(ge=0)
    session_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stop_reason: str | None = None
    failure_code: str | None = None
```

给所有相对路径复用 `PurePosixPath` 校验；给所有 SHA-256 复用 `_validate_hex()`。`StageModelManifest`
验证 file path 唯一；`StageInputManifest` 验证 segment/asset ID 与路径唯一。`StageRunState` 验证
timezone-aware 时间和 terminal 字段一致。

- [ ] **Step 4: 扩展 run manifest、fault plan 与 decision source schema**

```python
class StageRunManifest(_FrozenModel):
    covered_stages: tuple[StageNumber, ...]
    evidence_tier: EvidenceTier
    executor_id: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _lineage(self) -> Self:
        allowed = (self.stage,) if self.stage != 5 else ((5,), (3, 5))
        if self.stage == 5:
            if self.covered_stages not in allowed:
                raise ValueError("covered_stages must be (5,) or meeting (3, 5)")
        elif self.covered_stages != (self.stage,):
            raise ValueError("covered_stages must contain only the physical stage")
        return self


class StageGateEvidenceBundle(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    stage: StageNumber
    family_id: str
    candidate_id: str
    gates: dict[PromotionGate, GateStatus]
    source_artifact_sha256s: dict[PromotionGate, tuple[str, ...]]


class StageEligibilityEvidence(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    target_stage: StageNumber
    family_id: str
    candidate_id: str
    eligible: bool
    reason: Literal["advanced", "stage1_not_advanced", "upstream_incomplete", "not_unique_finalist"]
    upstream_report_sha256s: dict[UpstreamStage, str]


class FinalistSelectionEvidence(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    family_id: str
    selected_candidate_id: str
    eligible_candidate_ids: tuple[str, ...]
    upstream_report_sha256s: dict[UpstreamStage, str]
```

`StageEligibilityEvidence` 验证 `eligible=True` 只允许 reason=`advanced`，false 禁止 advanced，且 upstream
map 非空、hash 合法。`StageGateEvidenceBundle` 验证 `gates` 恰好等于 `PROMOTION_HARD_GATES`，source map 使用同一 key set，
每个 source tuple 非空且 hash 合法。`FinalistSelectionEvidence` 验证 eligible 非空、唯一、包含 selected，
并绑定恰好 Stage 1–4 四个 report hash；eligible 是否恰好一个由 Task 7 verifier 判断，避免调用方手填
`unique_finalist=True`。

调整 `FaultPlan`：所有 event cursor 唯一递增；disconnect/crash cursor `< 3_600_000`；唯一 finalization delay cursor `== 3_600_000` 且 `duration_ms > 0`；`duration_ms` 是 wall-clock，不再参与音频 cursor 越界计算。更新所有现有 `StageRunManifest` fixture，显式传入 `covered_stages`、`evidence_tier`、`executor_id`。

- [ ] **Step 5: 运行契约测试**

Run: `uv run pytest tests/benchmarks/test_asr_stage_contracts.py -q`

Expected: PASS。

- [ ] **Step 6: 运行定点类型与 lint**

Run: `uv run mypy src/sona/benchmarks/asr/stage_contracts.py`

Expected: `Success: no issues found`。

Run: `uv run ruff check src/sona/benchmarks/asr/stage_contracts.py tests/benchmarks/test_asr_stage_contracts.py`

Expected: `All checks passed!`。

- [ ] **Step 7: 运行 Mandatory Per-Commit Gate 并提交契约变更**

```bash
git add src/sona/benchmarks/asr/stage_contracts.py tests/benchmarks/test_asr_stage_contracts.py
git commit -m "feat(asr): 扩展阶段执行契约"
```

---

### Task 2: 验证并解析冻结 Stage 输入

**Files:**
- Create: `src/sona/benchmarks/asr/stage_inputs.py`
- Create: `tests/benchmarks/test_asr_stage_inputs.py`

**Interfaces:**
- Consumes: `ScheduleManifest`、`StageInputManifest`、`PCMInputBinding`、`InteractionScriptBinding`。
- Produces: `ResolvedPCMInput`、`ResolvedInteractionInput`、`ResolvedStageInput`、`load_stage_input_manifest()`、`resolve_stage_inputs()`、`verify_resolved_input()`。

- [ ] **Step 1: 写项目外路径、字节、时长、canonical JSON 和 symlink 的失败测试**

```python
def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _pcm_fixture(
    root: Path,
    *,
    pcm: bytes,
    duration_ms: int,
) -> tuple[ScheduleManifest, str, StageInputManifest, Path]:
    input_root = root / "external-input"
    input_root.mkdir(parents=True)
    pcm_path = input_root / "sample.pcm"
    pcm_path.write_bytes(pcm)
    input_hash = _sha256_bytes(pcm)
    schedule = ScheduleManifest(
        stage=2,
        family_id="meeting",
        segments=(
            ScheduleSegment(
                segment_id="screen-001",
                purpose="screen",
                input_sha256=input_hash,
                duration_ms=duration_ms,
                repetition=1,
            ),
        ),
    )
    schedule_hash = "a" * 64
    manifest = StageInputManifest(
        schedule_sha256=schedule_hash,
        bindings=(
            PCMInputBinding(
                segment_id="screen-001",
                relative_path="sample.pcm",
                input_sha256=input_hash,
                size_bytes=len(pcm),
                duration_ms=duration_ms,
            ),
        ),
    )
    return schedule, schedule_hash, manifest, input_root


def test_resolve_pcm_rechecks_hash_size_and_duration(tmp_path: Path) -> None:
    pcm = b"\x00\x00" * 16_000
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule, schedule_hash, manifest, input_root = _pcm_fixture(
        tmp_path, pcm=pcm, duration_ms=1_000
    )
    resolved = resolve_stage_inputs(
        schedule=schedule,
        schedule_sha256=schedule_hash,
        manifest=manifest,
        input_root=input_root,
        repository_root=repo,
        evidence_tier="formal",
    )
    assert resolved[0].duration_ms == 1_000
    (input_root / "sample.pcm").write_bytes(pcm + b"\x00\x00")
    with pytest.raises(StageInputError, match="changed after resolution"):
        verify_resolved_input(resolved[0])


def test_formal_input_rejects_repository_and_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule, schedule_hash, manifest, input_root = _pcm_fixture(
        repo, pcm=b"\0\0" * 16_000, duration_ms=1_000
    )
    with pytest.raises(StageInputError, match="outside the repository"):
        resolve_stage_inputs(
            schedule, schedule_hash, manifest, input_root, repo, "formal"
        )


def test_interaction_script_must_equal_canonical_json_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    script_path, schedule, schedule_hash, manifest, input_root = _interaction_fixture(tmp_path)
    script_path.write_text('{ "actions": [] }\n', encoding="utf-8")
    with pytest.raises(StageInputError, match="canonical JSON"):
        resolve_stage_inputs(
            schedule, schedule_hash, manifest, input_root, repo, "formal"
        )
```

同一 test file 增加以下 fixture；所有 hash 都从实际字节计算：

```python
def _interaction_fixture(
    root: Path,
) -> tuple[Path, ScheduleManifest, str, StageInputManifest, Path]:
    input_root = root / "external-interaction"
    input_root.mkdir(parents=True)
    pcm = b"\x00\x00" * 16_000
    asset_path = input_root / "utterance-1.pcm"
    asset_path.write_bytes(pcm)
    payload = {
        "actions": [
            {
                "at_cursor_ms": 0,
                "asset_id": "utterance-1",
                "duration_ms": 1_000,
                "kind": "feed_pcm",
            }
        ]
    }
    script_bytes = canonical_json_bytes(payload)
    script_path = input_root / "turn-001.json"
    script_path.write_bytes(script_bytes)
    script_hash = _sha256_bytes(script_bytes)
    schedule = ScheduleManifest(
        stage=4,
        family_id="interaction",
        segments=(
            ScheduleSegment(
                segment_id="screen-turn-001",
                purpose="screen",
                input_sha256=script_hash,
                duration_ms=1_000,
                repetition=1,
            ),
        ),
    )
    schedule_hash = "c" * 64
    manifest = StageInputManifest(
        schedule_sha256=schedule_hash,
        bindings=(
            InteractionScriptBinding(
                segment_id="screen-turn-001",
                relative_path="turn-001.json",
                input_sha256=script_hash,
                size_bytes=len(script_bytes),
                duration_ms=1_000,
                assets=(
                    InteractionAssetBinding(
                        asset_id="utterance-1",
                        relative_path="utterance-1.pcm",
                        input_sha256=_sha256_bytes(pcm),
                        size_bytes=len(pcm),
                        duration_ms=1_000,
                    ),
                ),
            ),
        ),
    )
    return script_path, schedule, schedule_hash, manifest, input_root
```

- [ ] **Step 2: 运行新测试并确认模块缺失**

Run: `uv run pytest tests/benchmarks/test_asr_stage_inputs.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'sona.benchmarks.asr.stage_inputs'`。

- [ ] **Step 3: 实现显式 loader、外部路径和 PCM 校验**

```python
@dataclass(frozen=True)
class ResolvedPCMInput:
    segment_id: str
    sha256: str
    size_bytes: int
    duration_ms: int
    frame_bytes: int
    _path: Path = field(repr=False)

    def iter_frames(
        self,
        *,
        start_offset_ms: int = 0,
        end_offset_ms: int | None = None,
    ) -> Iterator[bytes]:
        final_offset_ms = self.duration_ms if end_offset_ms is None else end_offset_ms
        if not 0 <= start_offset_ms <= final_offset_ms <= self.duration_ms:
            raise StageInputError("PCM slice is outside the resolved input")
        with self._path.open("rb") as stream:
            stream.seek(start_offset_ms * 32)
            remaining = (final_offset_ms - start_offset_ms) * 32
            while remaining > 0 and (frame := stream.read(min(self.frame_bytes, remaining))):
                remaining -= len(frame)
                yield frame


@dataclass(frozen=True)
class ResolvedInteractionInput:
    segment_id: str
    sha256: str
    size_bytes: int
    duration_ms: int
    actions: tuple[InteractionAction, ...]
    assets: Mapping[str, ResolvedPCMInput]
    _path: Path = field(repr=False)


ResolvedStageInput = ResolvedPCMInput | ResolvedInteractionInput


def _pcm_duration_ms(size_bytes: int) -> int:
    bytes_per_second = 16_000 * 1 * 2
    if size_bytes % 32 != 0:
        raise StageInputError("PCM size is not aligned to one millisecond")
    return size_bytes * 1_000 // bytes_per_second


def _resolve_regular_file(root: Path, relative_path: str) -> Path:
    candidate = root / relative_path
    if candidate.is_symlink():
        raise StageInputError("stage input must not be a symlink")
    resolved = candidate.resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise StageInputError("stage input escapes the declared root")
    if not stat.S_ISREG(resolved.lstat().st_mode):
        raise StageInputError("stage input must be a regular file")
    return resolved


def _resolve_pcm(binding: PCMInputBinding, root: Path) -> ResolvedPCMInput:
    path = _resolve_regular_file(root, binding.relative_path)
    size = path.stat().st_size
    digest = sha256_file(path)
    duration = _pcm_duration_ms(size)
    if (size, digest, duration) != (
        binding.size_bytes,
        binding.input_sha256,
        binding.duration_ms,
    ):
        raise StageInputError("PCM bytes do not match the frozen binding")
    return ResolvedPCMInput(
        segment_id=binding.segment_id,
        sha256=digest,
        size_bytes=size,
        duration_ms=duration,
        frame_bytes=640,
        _path=path,
    )


def resolve_stage_inputs(
    schedule: ScheduleManifest,
    schedule_sha256: str,
    manifest: StageInputManifest,
    input_root: Path,
    repository_root: Path,
    evidence_tier: EvidenceTier,
) -> tuple[ResolvedStageInput, ...]:
    if manifest.schedule_sha256 != schedule_sha256:
        raise StageInputError("stage input manifest schedule SHA-256 mismatch")
    root = input_root.resolve(strict=True)
    repo = repository_root.resolve(strict=True)
    if evidence_tier == "formal" and (root == repo or repo in root.parents):
        raise StageInputError("formal stage input root must be outside the repository")
    bindings = {binding.segment_id: binding for binding in manifest.bindings}
    expected_ids = tuple(segment.segment_id for segment in schedule.segments)
    if set(bindings) != set(expected_ids):
        raise StageInputError("stage input bindings must exactly match schedule segments")
    resolved: list[ResolvedStageInput] = []
    for segment in schedule.segments:
        binding = bindings[segment.segment_id]
        item = (
            _resolve_pcm(binding, root)
            if isinstance(binding, PCMInputBinding)
            else _resolve_interaction(binding, root)
        )
        if item.sha256 != segment.input_sha256 or item.duration_ms != segment.duration_ms:
            raise StageInputError("resolved input does not match frozen schedule")
        resolved.append(item)
    return tuple(resolved)
```

在函数体中逐项完成：schedule hash 绑定、segment 一一对应、`resolve(strict=True)` 后的 root containment、formal 项目外边界、`lstat()` regular-file/no-symlink、实际 size/hash/duration 与 binding/schedule 比较。20 ms PCM `frame_bytes=640`；Stage 3 可由 caller 指定同样的 canonical frame，禁止整段一次性加载。

- [ ] **Step 4: 实现 interaction canonical action 解析**

```python
InteractionActionKind = Literal["feed_pcm", "wait", "expect_tts", "barge_in"]


class InteractionAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: InteractionActionKind
    at_cursor_ms: int = Field(ge=0)
    asset_id: str | None = None
    duration_ms: int = Field(ge=0)


class InteractionScriptPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    actions: tuple[InteractionAction, ...] = Field(min_length=1)


def canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _resolve_interaction(
    binding: InteractionScriptBinding,
    root: Path,
) -> ResolvedInteractionInput:
    path = _resolve_regular_file(root, binding.relative_path)
    raw = path.read_bytes()
    payload = InteractionScriptPayload.model_validate_json(raw)
    if raw != canonical_json_bytes(payload.model_dump(mode="json")):
        raise StageInputError("interaction script must use canonical JSON")
    actions = payload.actions
    if tuple(action.at_cursor_ms for action in actions) != tuple(
        sorted(action.at_cursor_ms for action in actions)
    ):
        raise StageInputError("interaction action cursors must be monotonic")
    assets = {
        asset.asset_id: _resolve_pcm_asset(asset, root)
        for asset in binding.assets
    }
    referenced = {
        action.asset_id
        for action in actions
        if action.kind in {"feed_pcm", "barge_in"}
    }
    if None in referenced or not referenced.issubset(assets):
        raise StageInputError("interaction action references an unknown PCM asset")
    digest = hashlib.sha256(raw).hexdigest()
    if (len(raw), digest) != (binding.size_bytes, binding.input_sha256):
        raise StageInputError("interaction script bytes do not match the frozen binding")
    return ResolvedInteractionInput(
        segment_id=binding.segment_id,
        sha256=digest,
        size_bytes=len(raw),
        duration_ms=binding.duration_ms,
        actions=actions,
        assets=assets,
        _path=path,
    )
```

`_resolve_pcm_asset()` 与 `_resolve_pcm()` 使用同一 size/hash/duration/regular-file 检查，但把 asset ID 映射为
`ResolvedPCMInput.segment_id`。`verify_resolved_input()` 对 PCM 重新运行 size/hash/duration，对 interaction
重新验证 script canonical bytes 与所有 asset；任一变化抛出 `StageInputError("changed after resolution")`。

- [ ] **Step 5: 运行输入测试与契约回归**

Run: `uv run pytest tests/benchmarks/test_asr_stage_inputs.py tests/benchmarks/test_asr_stage_contracts.py -q`

Expected: PASS。

- [ ] **Step 6: 运行定点类型与 lint**

Run: `uv run mypy src/sona/benchmarks/asr/stage_inputs.py`

Expected: `Success: no issues found`。

Run: `uv run ruff check src/sona/benchmarks/asr/stage_inputs.py tests/benchmarks/test_asr_stage_inputs.py`

Expected: `All checks passed!`。

- [ ] **Step 7: 运行 Mandatory Per-Commit Gate 并提交输入解析器**

```bash
git add src/sona/benchmarks/asr/stage_inputs.py tests/benchmarks/test_asr_stage_inputs.py
git commit -m "feat(asr): 验证阶段冻结输入"
```

---

### Task 3: 原子写入并封存 Stage 制品

**Files:**
- Create: `src/sona/benchmarks/asr/stage_artifacts.py`
- Create: `tests/benchmarks/test_asr_stage_artifacts.py`

**Interfaces:**
- Consumes: `StageRunManifest`、`StageRunState`、`ArtifactIdentity`、`ArtifactIndex`。
- Produces: `StageArtifactWriter.create()`、`replace_manifest()`、`replace_state()`、`append_event()`、`append_failure()`、`append_fault()`、`append_resource()`、`write_metrics()`、`write_summary()`、`seal()`。

- [ ] **Step 1: 写权限、不可覆盖、原子 snapshot、symlink 和 seal 的失败测试**

```python
def _manifest(*, status: StageStatus) -> StageRunManifest:
    return StageRunManifest(
        run_id="run-001",
        stage=2,
        covered_stages=(2,),
        family_id="meeting",
        arm="baseline",
        candidate_id="qwen",
        evidence_tier="experimental",
        executor_id="test-synthetic",
        git_commit="1" * 40,
        model_sha256="a" * 64,
        profile_sha256="b" * 64,
        runtime_config_sha256="c" * 64,
        schedule_sha256="d" * 64,
        started_at=datetime(2026, 8, 25, tzinfo=UTC),
        status=status,
    )


def _state(*, status: StageStatus) -> StageRunState:
    terminal = status in {"completed", "failed", "deferred"}
    return StageRunState(
        run_id="run-001",
        status=status,
        phase="terminal" if terminal else "screen",
        cursor_ms=1_000 if terminal else 0,
        start_count=1,
        session_id="synthetic-session-1",
        started_at=datetime(2026, 8, 25, tzinfo=UTC),
        finished_at=datetime(2026, 8, 25, 0, 1, tzinfo=UTC) if terminal else None,
        stop_reason="schedule_complete" if status == "completed" else None,
    )


def _ready_writer(tmp_path: Path) -> StageArtifactWriter:
    writer = StageArtifactWriter.create(tmp_path, "run-001")
    writer.replace_manifest(_manifest(status="completed"))
    writer.replace_state(_state(status="completed"))
    writer.append_event({"event_kind": "state", "status": "completed"})
    writer.write_metrics({"wall_elapsed_ms": 1_000})
    writer.write_summary({"stop_reason": "schedule_complete"})
    writer.ensure_empty_streams()
    return writer


def test_writer_seals_private_required_artifacts(tmp_path: Path) -> None:
    writer = StageArtifactWriter.create(tmp_path, "run-001")
    writer.replace_manifest(_manifest(status="running"))
    writer.replace_state(_state(status="running"))
    writer.append_event({"event_kind": "state", "status": "running"})
    writer.write_metrics({"wall_elapsed_ms": 1_000})
    writer.write_summary({"stop_reason": "schedule_complete"})
    writer.ensure_empty_streams()
    writer.replace_manifest(_manifest(status="completed"))
    writer.replace_state(_state(status="completed"))
    index = writer.seal()
    assert (tmp_path / "run-001" / "artifact-index.json").stat().st_mode & 0o777 == 0o600
    assert {item.path for item in index.artifacts} >= {
        "state.json", "events.jsonl", "metrics.json", "resources.csv",
        "fault-execution.jsonl", "failures.jsonl", "summary.json",
    }


def test_seal_rejects_symlink(tmp_path: Path) -> None:
    writer = _ready_writer(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    (writer.run_dir / "metrics.json").unlink()
    (writer.run_dir / "metrics.json").symlink_to(outside)
    with pytest.raises(StageArtifactError, match="regular file"):
        writer.seal()


def test_sealed_writer_rejects_mutation(tmp_path: Path) -> None:
    writer = _ready_writer(tmp_path)
    writer.seal()
    with pytest.raises(StageArtifactSealedError):
        writer.append_event({"event_kind": "late"})


def test_writer_refuses_existing_run_directory(tmp_path: Path) -> None:
    StageArtifactWriter.create(tmp_path, "run-001")
    with pytest.raises(FileExistsError):
        StageArtifactWriter.create(tmp_path, "run-001")
```

- [ ] **Step 2: 运行测试并确认模块缺失**

Run: `uv run pytest tests/benchmarks/test_asr_stage_artifacts.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 实现私有 run 目录和 snapshot/stream writer**

```python
REQUIRED_ARTIFACTS = (
    "state.json",
    "events.jsonl",
    "metrics.json",
    "resources.csv",
    "fault-execution.jsonl",
    "failures.jsonl",
    "summary.json",
)


@dataclass
class StageArtifactWriter:
    run_dir: Path
    _sealed: bool = False

    @classmethod
    def create(cls, output_root: Path, run_id: str) -> Self:
        run_dir = output_root / run_id
        run_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        if run_dir.is_symlink():
            raise StageArtifactError("run directory must not be a symlink")
        return cls(run_dir=run_dir)
```

snapshot 使用同目录 `mkstemp`、`fchmod(0600)`、flush、`fsync`、`os.replace`、父目录 `fsync`；只允许 manifest/state 在 seal 前替换。JSONL/CSV 以 `os.open(..., O_APPEND|O_CREAT|O_CLOEXEC, 0o600)` 写入并在 checkpoint/terminal `fsync`。错误 payload 先通过 `sanitize_artifact_value()` 去除用户名、绝对根路径、URL query 和超长值。

- [ ] **Step 4: 实现最终 hash seal**

```python
def seal(self) -> ArtifactIndex:
    self._require_open()
    manifest_path = self.run_dir / "manifest.json"
    identities: list[ArtifactIdentity] = []
    for path in sorted(self.run_dir.rglob("*"), key=lambda item: item.as_posix()):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise StageArtifactError("stage artifact must not be a symlink")
        if stat.S_ISDIR(mode):
            if mode & 0o777 != 0o700:
                raise StageArtifactError("stage artifact directory must use mode 0700")
            continue
        if not stat.S_ISREG(mode):
            raise StageArtifactError("stage artifact must be a regular file")
        if path.name not in {"manifest.json", "artifact-index.json"}:
            identities.append(self._identity_for(path))
    artifacts = tuple(identities)
    required = set(REQUIRED_ARTIFACTS)
    if not required.issubset({item.path for item in artifacts}):
        raise StageArtifactError("required stage artifacts are incomplete")
    index = ArtifactIndex(
        run_manifest_sha256=sha256_file(manifest_path),
        artifacts=artifacts,
    )
    self._write_new_json("artifact-index.json", index.model_dump(mode="json"))
    self._sealed = True
    return index
```

`rglob()` 结果在调用 `is_file()` 前先用 `lstat()` 拒绝任何 symlink；允许 `checkpoints/` 等私有子目录，
并要求每层目录 mode `0700`。`_identity_for()` 检查 regular file/mode `0600`，以相对 run root 的 POSIX
路径重新计算 size/hash。seal 后所有 writer 方法抛出 `StageArtifactSealedError`。捕获异常路径可以封存
partial 文件；writer 自身损坏时不伪造 index。

- [ ] **Step 5: 运行 artifact 测试与 replay 原子写回归**

Run: `uv run pytest tests/benchmarks/test_asr_stage_artifacts.py tests/benchmarks/test_asr_replay.py -q`

Expected: PASS。

- [ ] **Step 6: 运行定点类型与 lint**

Run: `uv run mypy src/sona/benchmarks/asr/stage_artifacts.py`

Expected: `Success: no issues found`。

Run: `uv run ruff check src/sona/benchmarks/asr/stage_artifacts.py tests/benchmarks/test_asr_stage_artifacts.py`

Expected: `All checks passed!`。

- [ ] **Step 7: 运行 Mandatory Per-Commit Gate 并提交 artifact writer**

```bash
git add src/sona/benchmarks/asr/stage_artifacts.py tests/benchmarks/test_asr_stage_artifacts.py
git commit -m "feat(asr): 封存阶段运行制品"
```

---

### Task 4: 定义执行器、observation 与测试 registry

**Files:**
- Create: `src/sona/benchmarks/asr/stage_executors.py`
- Create: `tests/benchmarks/asr_stage_fakes.py`
- Create: `tests/benchmarks/test_asr_stage_executors.py`

**Interfaces:**
- Consumes: `StageNumber`、`FaultEvent`、`ScheduleSegment`、`ResolvedStageInput`。
- Produces: `StageExecutor`、`StageExecutorCapabilities`、`ValidatedRuntimeInputs`、`StageExecutionContext`、`SessionIdentity`、五种 observation、`StageExecutorRegistry`；测试产生 `SyntheticStageExecutor`。

- [ ] **Step 1: 写 registry、formal/synthetic 能力和幂等 close 的失败测试**

```python
def test_registry_rejects_duplicate_and_unknown_executor() -> None:
    registry = StageExecutorRegistry()
    registry.register("test-synthetic", lambda: SyntheticStageExecutor())
    with pytest.raises(ValueError, match="already registered"):
        registry.register("test-synthetic", lambda: SyntheticStageExecutor())
    with pytest.raises(UnknownStageExecutorError, match="UNKNOWN_STAGE_EXECUTOR"):
        registry.create("missing")


@pytest.mark.asyncio
async def test_synthetic_close_is_idempotent_and_reports_release() -> None:
    executor = SyntheticStageExecutor()
    first = await executor.close()
    second = await executor.close()
    assert first.released and second.released
    assert executor.close_count == 2
```

- [ ] **Step 2: 运行测试并确认模块缺失**

Run: `uv run pytest tests/benchmarks/test_asr_stage_executors.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 实现协议和不可变 observations**

```python
@dataclass(frozen=True)
class StageExecutorCapabilities:
    supported_stages: frozenset[StageNumber]
    supported_inputs: frozenset[Literal["pcm", "interaction_script"]]
    supports_continuation: bool
    supported_faults: frozenset[FaultKind]
    is_synthetic: bool


@dataclass(frozen=True)
class CursorRange:
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class ValidatedRuntimeInputs:
    model_root: Path = field(repr=False)
    model_manifest: StageModelManifest
    profile: Mapping[str, object] = field(repr=False)
    runtime_config: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True)
class StageExecutionContext:
    run_id: str
    stage: StageNumber
    covered_stages: tuple[StageNumber, ...]
    family_id: str
    candidate_id: str
    evidence_tier: EvidenceTier
    identity_sha256s: Mapping[str, str]
    runtime_inputs: ValidatedRuntimeInputs = field(repr=False)


@dataclass(frozen=True)
class SessionIdentity:
    session_id: str
    process_ids: tuple[int, ...] = ()
    source_epoch: int = 0


@dataclass(frozen=True)
class SegmentObservation:
    segment_id: str
    repetition_index: int
    slice_index: int
    cursor: CursorRange
    session_id: str
    source_epoch: int
    metrics: Mapping[str, float | int | bool | str]


FaultOutcome = Literal["applied", "recovered", "failed", "unknown"]


@dataclass(frozen=True)
class FaultObservation:
    event_id: str
    kind: FaultKind
    planned_cursor_ms: int
    actual_cursor_ms: int
    outcome: FaultOutcome
    session_id_before: str
    session_id_after: str
    source_epoch_before: int
    source_epoch_after: int


@dataclass(frozen=True)
class RuntimeObservation:
    monotonic_ms: int
    rss_bytes: int
    file_descriptors: int
    background_tasks: int
    queue_depth: int


@dataclass(frozen=True)
class FinalObservation:
    eof_sent: bool
    terminal_received: bool
    finalization_latency_ms: int
    metrics: Mapping[str, float | int | bool | str]


@dataclass(frozen=True)
class CloseObservation:
    released: bool
    remaining_process_ids: tuple[int, ...] = ()
    remaining_ports: tuple[int, ...] = ()
    remaining_tasks: int = 0
    remaining_connections: int = 0


class StageExecutor(Protocol):
    executor_id: str
    capabilities: StageExecutorCapabilities

    async def prepare(self, context: StageExecutionContext) -> None: ...
    async def start(self, context: StageExecutionContext) -> SessionIdentity: ...
    async def feed_segment(
        self,
        segment: ScheduleSegment,
        resolved_input: ResolvedStageInput,
        cursor_range: CursorRange,
    ) -> SegmentObservation: ...
    async def inject_fault(self, event: FaultEvent) -> FaultObservation: ...
    async def snapshot(self) -> RuntimeObservation: ...
    async def finalize(
        self,
        finalization_fault: FaultEvent | None,
    ) -> FinalObservation: ...
    async def close(self) -> CloseObservation: ...
```

Observation 只含 opaque ID、cursor、monotonic 时间、计数、布尔状态和结构化 metrics；不含任意路径、
reference 或秘密。`StageExecutionContext.runtime_inputs` 只驻留内存，不允许 dataclass 序列化或写入错误；
所有 writer sink 由 runner 独占。

- [ ] **Step 4: 实现显式 registry 与测试 synthetic executor**

```python
class StageExecutorRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], StageExecutor]] = {}

    def register(self, executor_id: str, factory: Callable[[], StageExecutor]) -> None:
        if executor_id in self._factories:
            raise ValueError(f"stage executor already registered: {executor_id}")
        self._factories[executor_id] = factory

    def create(self, executor_id: str) -> StageExecutor:
        try:
            executor = self._factories[executor_id]()
        except KeyError as exc:
            raise UnknownStageExecutorError(executor_id) from exc
        if executor.executor_id != executor_id:
            raise ValueError("stage executor factory identity mismatch")
        return executor
```

`SyntheticStageExecutor` 只放在 `tests/benchmarks/asr_stage_fakes.py`；支持预设 observation、记录方法调用顺序、start/session/count/cursor、故障状态和指定方法抛错。生产 registry 不导入测试 helper。

- [ ] **Step 5: 运行 executor 测试**

Run: `uv run pytest tests/benchmarks/test_asr_stage_executors.py -q`

Expected: PASS。

- [ ] **Step 6: 运行定点类型与 lint**

Run: `uv run mypy src/sona/benchmarks/asr/stage_executors.py`

Expected: `Success: no issues found`。

Run: `uv run ruff check src/sona/benchmarks/asr/stage_executors.py tests/benchmarks/asr_stage_fakes.py tests/benchmarks/test_asr_stage_executors.py`

Expected: `All checks passed!`。

- [ ] **Step 7: 运行 Mandatory Per-Commit Gate 并提交执行器接口**

```bash
git add src/sona/benchmarks/asr/stage_executors.py tests/benchmarks/asr_stage_fakes.py tests/benchmarks/test_asr_stage_executors.py
git commit -m "feat(asr): 定义阶段执行器边界"
```

---

### Task 5: 实现资源 quarantine 与核心 Screen/Confirm 状态机

**Files:**
- Modify: `src/sona/benchmarks/resource_lock.py`
- Create: `src/sona/benchmarks/asr/stage_evaluators.py`
- Create: `src/sona/benchmarks/asr/stage_runner.py`
- Modify: `tests/benchmarks/asr_stage_fakes.py`
- Modify: `tests/benchmarks/test_resource_lock.py`
- Create: `tests/benchmarks/test_asr_stage_runner.py`

**Interfaces:**
- Consumes: Tasks 1–4 的 contracts、resolved inputs、writer、executor。
- Produces: `StageRunRequest`、`StageRunResult`、`StagePolicy`、`ScreenDecision`、`validate_status_transition()`、`run_stage()`、`ResourceQuarantinedError`、quarantine write/check/clear；测试 helper `StageFixture/build_stage_fixture()` 在 `asr_stage_fakes.py` 内创建完整项目外 JSON/PCM fixture。

- [ ] **Step 1: 写锁竞争不构造 executor、quarantine 和状态转移失败测试**

```python
@pytest.mark.asyncio
async def test_lock_contention_creates_no_executor_or_output(tmp_path: Path) -> None:
    fixture = build_stage_fixture(tmp_path, lock_path=tmp_path / "host.lock")
    calls: list[str] = []

    def factory() -> StageExecutor:
        calls.append("created")
        return SyntheticStageExecutor()

    with exclusive_resource_lock(fixture.request.lock_path, run_id="owner"):
        with pytest.raises(ResourceBusyError):
            await run_stage(fixture.request, executor_factory=factory, policy=fixture.policy)
    assert calls == []
    assert not (fixture.request.output_root / fixture.request.run_id).exists()


@pytest.mark.asyncio
async def test_formal_deferred_run_never_constructs_executor(tmp_path: Path) -> None:
    fixture = build_stage_fixture(tmp_path)
    upstream = fixture.request.output_root.parent / "stage1-report.json"
    _write_private_json(upstream, {"family_id": "meeting", "candidate_id": "qwen"})
    eligibility_path = fixture.request.output_root.parent / "eligibility.json"
    _write_private_json(
        eligibility_path,
        StageEligibilityEvidence(
            target_stage=2,
            family_id="meeting",
            candidate_id="qwen",
            eligible=False,
            reason="stage1_not_advanced",
            upstream_report_sha256s={"stage1": sha256_file(upstream)},
        ).model_dump(mode="json"),
    )
    request = dataclasses.replace(
        fixture.request,
        evidence_tier="formal",
        executor_id="meeting-real-test",
        eligibility_path=eligibility_path,
        upstream_report_paths={"stage1": upstream},
    )
    calls: list[str] = []

    def factory() -> StageExecutor:
        calls.append("created")
        return SyntheticStageExecutor()

    result = await run_stage(request, executor_factory=factory, policy=fixture.policy)
    assert result.status == "deferred"
    assert calls == []


def test_quarantine_requires_explicit_clean_audit(tmp_path: Path) -> None:
    marker = tmp_path / "resource-quarantine.json"
    write_resource_quarantine(
        marker,
        run_id="run-001",
        executor_id="meeting-test",
        observation=CloseObservation(released=False, remaining_process_ids=(123,)),
    )
    with pytest.raises(ResourceQuarantinedError, match="RESOURCE_QUARANTINED"):
        require_no_resource_quarantine(marker)
    clear_resource_quarantine(
        marker,
        ResourceReleaseAudit(
            released=True,
            remaining_process_ids=(),
            remaining_ports=(),
            remaining_tasks=0,
            remaining_connections=0,
        ),
    )
    assert not marker.exists()


def test_stage_status_rejects_skips_and_terminal_reentry() -> None:
    with pytest.raises(StageStateError, match="planned.*completed"):
        validate_status_transition("planned", "completed")
    validate_status_transition("planned", "running")
    validate_status_transition("running", "completed")
    with pytest.raises(StageStateError, match="completed.*running"):
        validate_status_transition("completed", "running")
```

- [ ] **Step 2: 写 Screen continuation 与 Screen-Fail 失败测试**

```python
@pytest.fixture
def stage_fixture(tmp_path: Path) -> StageFixture:
    return build_stage_fixture(tmp_path)


@pytest.mark.asyncio
async def test_screen_pass_continues_same_session_without_restart(stage_fixture: StageFixture) -> None:
    executor = SyntheticStageExecutor()
    result = await run_stage(stage_fixture.request, lambda: executor, stage_fixture.policy)
    assert result.status == "completed"
    assert executor.start_count == 1
    assert executor.session_ids == ("synthetic-session-1",)
    assert executor.fed_segment_ids == ("screen-001", "confirm-001")
    assert executor.cursor_ranges == ((0, 1_000), (1_000, 2_000))


@pytest.mark.asyncio
async def test_screen_fail_does_not_consume_confirm(stage_fixture: StageFixture) -> None:
    executor = SyntheticStageExecutor()
    policy = TestStagePolicy(pass_screen=False)
    result = await run_stage(stage_fixture.request, lambda: executor, policy)
    assert result.status == "completed"
    assert result.stop_reason == "screen_fail"
    assert executor.fed_segment_ids == ("screen-001",)
```

在 `asr_stage_fakes.py` 增加以下完整 fixture 接口；它用 sparse zero PCM 避免 60 分钟测试占用实际磁盘，
但仍让生产 resolver 读取并 hash 完整逻辑字节：

```python
@dataclass(frozen=True)
class StageFixture:
    request: StageRunRequest
    policy: StagePolicy


@dataclass(frozen=True)
class TestStagePolicy:
    pass_screen: bool = True

    def phase_for(self, segment: ScheduleSegment) -> StagePhase:
        return cast(StagePhase, segment.purpose)

    def evaluate_screen(
        self, observations: tuple[SegmentObservation, ...]
    ) -> ScreenDecision:
        del observations
        return ScreenDecision.PASS if self.pass_screen else ScreenDecision.FAIL


def _write_private_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


DEFAULT_SEGMENTS: tuple[tuple[str, SchedulePurpose, int], ...] = (
    ("screen-001", "screen", 1_000),
    ("confirm-001", "confirm", 1_000),
)


def build_stage_fixture(
    tmp_path: Path,
    *,
    lock_path: Path | None = None,
    stage: StageNumber = 2,
    covered_stages: tuple[StageNumber, ...] = (2,),
    segment_specs: tuple[tuple[str, SchedulePurpose, int], ...] = DEFAULT_SEGMENTS,
    fault_plan: FaultPlan | None = None,
) -> StageFixture:
    repo = tmp_path / "repo"
    external = tmp_path / "external"
    input_root = external / "inputs"
    output_root = external / "runs"
    repo.mkdir()
    input_root.mkdir(parents=True)
    output_root.mkdir()
    segments: list[ScheduleSegment] = []
    bindings: list[PCMInputBinding] = []
    for segment_id, purpose, duration_ms in segment_specs:
        path = input_root / f"{segment_id}.pcm"
        with path.open("wb") as stream:
            stream.truncate(duration_ms * 32)
        digest = sha256_file(path)
        segments.append(
            ScheduleSegment(
                segment_id=segment_id,
                purpose=purpose,
                input_sha256=digest,
                duration_ms=duration_ms,
                repetition=1,
            )
        )
        bindings.append(
            PCMInputBinding(
                segment_id=segment_id,
                relative_path=path.name,
                input_sha256=digest,
                size_bytes=duration_ms * 32,
                duration_ms=duration_ms,
            )
        )
    schedule = ScheduleManifest(stage=stage, family_id="meeting", segments=tuple(segments))
    schedule_path = external / "schedule.json"
    _write_private_json(schedule_path, schedule.model_dump(mode="json"))
    schedule_hash = sha256_file(schedule_path)
    input_manifest_path = external / "inputs.json"
    _write_private_json(
        input_manifest_path,
        StageInputManifest(
            schedule_sha256=schedule_hash,
            bindings=tuple(bindings),
        ).model_dump(mode="json"),
    )
    model_root = external / "model"
    model_root.mkdir()
    model_file = model_root / "weights.bin"
    model_file.write_bytes(b"test-model")
    model_manifest_path = external / "model-manifest.json"
    _write_private_json(
        model_manifest_path,
        StageModelManifest(
            model_id="test/model",
            model_revision="test-revision",
            files=(
                StageModelFile(
                    relative_path="weights.bin",
                    sha256=sha256_file(model_file),
                    size_bytes=model_file.stat().st_size,
                ),
            ),
        ).model_dump(mode="json"),
    )
    identity_paths = {
        name: external / f"{name}.json"
        for name in ("profile", "runtime-config")
    }
    for name, path in identity_paths.items():
        _write_private_json(path, {"identity": name})
    fault_plan_path = external / "fault-plan.json" if fault_plan is not None else None
    if fault_plan_path is not None:
        _write_private_json(fault_plan_path, fault_plan.model_dump(mode="json"))
    request = StageRunRequest(
        run_id=f"stage{stage}-meeting-test",
        stage=stage,
        covered_stages=covered_stages,
        family_id="meeting",
        arm="finalist" if stage == 5 else "baseline",
        candidate_id="qwen",
        evidence_tier="experimental",
        executor_id="test-synthetic",
        model_manifest_path=model_manifest_path,
        model_root=model_root,
        profile_path=identity_paths["profile"],
        runtime_config_path=identity_paths["runtime-config"],
        schedule_path=schedule_path,
        input_manifest_path=input_manifest_path,
        input_root=input_root,
        output_root=output_root,
        repository_root=repo,
        fault_plan_path=fault_plan_path,
        lock_path=lock_path or external / "host.lock",
        lock_timeout_secs=0.0,
    )
    return StageFixture(request=request, policy=TestStagePolicy())
```

- [ ] **Step 3: 运行测试并确认 API 缺失**

Run: `uv run pytest tests/benchmarks/test_resource_lock.py tests/benchmarks/test_asr_stage_runner.py -q`

Expected: FAIL，缺少 quarantine 与 `run_stage()`。

- [ ] **Step 4: 在现有 lock 模块增加项目外 quarantine**

```python
class ResourceQuarantinedError(RuntimeError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"RESOURCE_QUARANTINED: cleanup audit required: {path}")


@dataclass(frozen=True)
class ResourceReleaseAudit:
    released: bool
    remaining_process_ids: tuple[int, ...]
    remaining_ports: tuple[int, ...]
    remaining_tasks: int
    remaining_connections: int


def require_no_resource_quarantine(path: Path) -> None:
    if path.exists():
        raise ResourceQuarantinedError(path)


def resource_quarantine_path(lock_path: Path | None = None) -> Path:
    resolved_lock = lock_path or default_resource_lock_path()
    return resolved_lock.with_name(f"{resolved_lock.name}.quarantine.json")


def clear_resource_quarantine(path: Path, audit: ResourceReleaseAudit) -> None:
    if not (
        audit.released
        and not audit.remaining_process_ids
        and not audit.remaining_ports
        and audit.remaining_tasks == 0
        and audit.remaining_connections == 0
    ):
        raise ValueError("resource quarantine requires a clean release audit")
    path.unlink()
```

marker 使用原子 `0600` JSON；只记录 run ID、时间、opaque executor ID、剩余自有 PID/port/count，不记录命令参数或环境。`clear` 先验证 marker 是 regular file、mode 0600，再要求 audit 全部为零。

- [ ] **Step 5: 实现 Stage policy 接口和核心 `run_stage()`**

```python
@pydantic_dataclass(config=ConfigDict(extra="forbid", frozen=True))
class StageRunRequest:
    run_id: str
    stage: StageNumber
    covered_stages: tuple[StageNumber, ...]
    family_id: str
    arm: Literal["baseline", "finalist"]
    candidate_id: str
    evidence_tier: EvidenceTier
    executor_id: str
    model_manifest_path: Path
    model_root: Path
    profile_path: Path
    runtime_config_path: Path
    schedule_path: Path
    input_manifest_path: Path
    input_root: Path
    output_root: Path
    repository_root: Path
    eligibility_path: Path | None = None
    upstream_report_paths: Mapping[UpstreamStage, Path] = field(default_factory=dict)
    fault_plan_path: Path | None = None
    lock_path: Path | None = None
    lock_timeout_secs: float = 0.0

    @property
    def quarantine_path(self) -> Path:
        return resource_quarantine_path(self.lock_path)


@dataclass(frozen=True)
class StageRunResult:
    run_id: str
    status: StageStatus
    covered_stages: tuple[StageNumber, ...]
    stop_reason: str
    manifest_sha256: str | None
    artifact_index_sha256: str | None
    executed_fault_counts: Mapping[FaultKind, int]
    stage3_checkpoint_sha256: str | None = None


class ScreenDecision(StrEnum):
    PASS = "Screen-Pass"
    FAIL = "Screen-Fail"


ALLOWED_STATUS_TRANSITIONS: Mapping[StageStatus, frozenset[StageStatus]] = {
    "planned": frozenset({"running", "deferred"}),
    "running": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "deferred": frozenset(),
}


def validate_status_transition(current: StageStatus, target: StageStatus) -> None:
    if target not in ALLOWED_STATUS_TRANSITIONS[current]:
        raise StageStateError(f"illegal stage status transition: {current} -> {target}")


class StagePolicy(Protocol):
    def phase_for(self, segment: ScheduleSegment) -> StagePhase: ...
    def evaluate_screen(self, observations: tuple[SegmentObservation, ...]) -> ScreenDecision: ...
```

在同一步实现 `load_stage_run_request()`、model/profile/runtime/schedule/input/fault/eligibility loader，以及
`validate_stage_request()`。每个 loader 使用 strict schema、项目外 `resolve(strict=True)`、regular/no-symlink
检查和实际 SHA-256；函数返回包含 `ValidatedRuntimeInputs` 与 resolved inputs 的内部
`ValidatedStageRunRequest`，不把绝对路径写入 model dump。

- [ ] **Step 6: 实现 happy-path `run_stage()` 和 Screen 状态机**

```python


async def run_stage(
    request: StageRunRequest,
    executor_factory: Callable[[], StageExecutor],
    policy: StagePolicy,
) -> StageRunResult:
    with exclusive_resource_lock(
        request.lock_path,
        timeout_secs=request.lock_timeout_secs,
        run_id=request.run_id,
    ):
        require_no_resource_quarantine(request.quarantine_path)
        validated = validate_stage_request(request)
        eligibility = validate_stage_eligibility(validated)
        if not eligibility.eligible:
            writer = StageArtifactWriter.create(validated.output_root, request.run_id)
            return record_deferred_run(validated, eligibility, writer)
        executor = executor_factory()
        validate_executor_capabilities(validated, executor.capabilities)
        writer = StageArtifactWriter.create(validated.output_root, request.run_id)
        return await _run_locked(validated, executor, policy, writer)
```

`validate_stage_request()` 在创建 writer/executor 前完成 external root、lineage，以及
`StageModelManifest` 中每个实际 model file 的 regular/no-symlink/size/hash 校验；executor factory
只能构造轻量 adapter，模型/服务启动必须留在 `prepare/start`。`validate_executor_capabilities()` 在创建 run
目录前检查 stage、input、continuation、fault 与 formal/synthetic。`_run_locked()` 固定调用
`prepare → start once → feed ordered repetitions → screen evaluate → finalize → close → terminal snapshots → seal`；
任何 regular exception 都先尝试 close，再封存 failed；Screen-Fail 是 completed decision，不是
infrastructure failed。每次 repetition 调用 `verify_resolved_input()` 并记录 cursor range。
formal `validate_stage_eligibility()` 加载 bundle、重算全部 upstream report hash 并核对 stage/family/
candidate；experimental 缺省返回 eligible/advanced。deferred 路径写 planned/deferred state、summary、空
streams 和 artifact index，`start_count=0`，不调用 factory/prepare/start。

- [ ] **Step 7: 实现清理失败与不可捕获失败语义**

如果 `CloseObservation.released=False`，写 quarantine、run status=`failed`、failure code=`resource_cleanup_incomplete`，封存能写出的 partial artifacts并返回非成功；如果 writer 自身失败，保留无 index 的 partial run 并重新抛出 `StageArtifactError`。任何已有 run dir、terminal 回退或 resume 请求直接拒绝。

- [ ] **Step 8: 运行 runner、lock、input、artifact 回归**

Run: `uv run pytest tests/benchmarks/test_asr_stage_runner.py tests/benchmarks/test_resource_lock.py tests/benchmarks/test_asr_stage_inputs.py tests/benchmarks/test_asr_stage_artifacts.py -q`

Expected: PASS。

- [ ] **Step 9: 运行定点类型与 lint**

Run: `uv run mypy src/sona/benchmarks/asr/stage_runner.py src/sona/benchmarks/asr/stage_evaluators.py src/sona/benchmarks/resource_lock.py`

Expected: `Success: no issues found`。

Run: `uv run ruff check src/sona/benchmarks/asr/stage_runner.py src/sona/benchmarks/asr/stage_evaluators.py src/sona/benchmarks/resource_lock.py tests/benchmarks/test_asr_stage_runner.py tests/benchmarks/test_resource_lock.py`

Expected: `All checks passed!`。

- [ ] **Step 10: 运行 Mandatory Per-Commit Gate 并提交核心状态机**

```bash
git add src/sona/benchmarks/resource_lock.py src/sona/benchmarks/asr/stage_evaluators.py src/sona/benchmarks/asr/stage_runner.py tests/benchmarks/asr_stage_fakes.py tests/benchmarks/test_resource_lock.py tests/benchmarks/test_asr_stage_runner.py
git commit -m "feat(asr): 实现阶段运行状态机"
```

---

### Task 6: 编排 Stage 5 故障和 Stage 3/5 共用会话

**Files:**
- Modify: `src/sona/benchmarks/asr/stage_evaluators.py`
- Modify: `src/sona/benchmarks/asr/stage_runner.py`
- Modify: `tests/benchmarks/asr_stage_fakes.py`
- Modify: `tests/benchmarks/test_asr_stage_runner.py`

**Interfaces:**
- Consumes: `run_stage()`、`FaultPlan`、`FaultObservation`、`StagePolicy`。
- Produces: `FaultScheduler`、`MeetingStagePolicy`、`InteractionStagePolicy`、Stage 3 checkpoint/metrics slice 与固定故障执行日志；测试 helper `build_stage5_fixture()`。

- [ ] **Step 1: 写精确 cursor、一次注入、结果计数和 finalization delay 失败测试**

```python
@pytest.mark.asyncio
async def test_stage5_injects_each_fault_once_at_exact_cursor(tmp_path: Path) -> None:
    stage5_fixture = build_stage5_fixture(tmp_path)
    executor = SyntheticStageExecutor(fault_outcomes="recovered")
    result = await run_stage(stage5_fixture.request, lambda: executor, stage5_fixture.policy)
    assert result.status == "completed"
    assert executor.injected_faults == (
        ("d1", 600_000), ("d2", 1_200_000), ("crash", 1_800_000),
        ("d3", 2_400_000), ("delay", 3_600_000),
    )
    assert executor.finalize_order == ("eof", "delay", "terminal")


@pytest.mark.asyncio
async def test_unknown_fault_cannot_count_as_executed(tmp_path: Path) -> None:
    stage5_fixture = build_stage5_fixture(tmp_path)
    executor = SyntheticStageExecutor(fault_overrides={"crash": "unknown"})
    result = await run_stage(stage5_fixture.request, lambda: executor, stage5_fixture.policy)
    assert result.stop_reason == "fault_not_recovered"
    assert result.executed_fault_counts["asr_crash"] == 0
```

- [ ] **Step 2: 写一次会话产生 Stage 3 checkpoint 和 Stage 5 证据的失败测试**

```python
@pytest.mark.asyncio
async def test_meeting_candidate_reuses_one_session_for_stage3_and_stage5(
    tmp_path: Path,
) -> None:
    meeting_stage5_fixture = build_stage5_fixture(tmp_path)
    executor = SyntheticStageExecutor()
    result = await run_stage(meeting_stage5_fixture.request, lambda: executor, meeting_stage5_fixture.policy)
    assert executor.start_count == 1
    assert result.covered_stages == (3, 5)
    assert result.stage3_checkpoint_sha256 is not None
    summary = json.loads(
        (
            meeting_stage5_fixture.request.output_root
            / meeting_stage5_fixture.request.run_id
            / "summary.json"
        ).read_text(encoding="utf-8")
    )
    assert summary["stage3_window"] == {"start_ms": 0, "end_ms": 1_800_000}
    assert summary["stage5_window"] == {"start_ms": 1_800_000, "end_ms": 3_600_000}
```

在 `asr_stage_fakes.py` 增加精确 Stage 5 fixture：

```python
def build_stage5_fixture(tmp_path: Path) -> StageFixture:
    fault_plan = FaultPlan(
        stage=5,
        duration_ms=3_600_000,
        events=(
            FaultEvent(event_id="d1", cursor_ms=600_000, kind="disconnect"),
            FaultEvent(event_id="d2", cursor_ms=1_200_000, kind="disconnect"),
            FaultEvent(event_id="crash", cursor_ms=1_800_000, kind="asr_crash"),
            FaultEvent(event_id="d3", cursor_ms=2_400_000, kind="disconnect"),
            FaultEvent(
                event_id="delay",
                cursor_ms=3_600_000,
                kind="finalization_delay",
                duration_ms=5_000,
            ),
        ),
    )
    base = build_stage_fixture(
        tmp_path,
        stage=5,
        covered_stages=(3, 5),
        segment_specs=(
            ("preflight", "system", 300_000),
            ("stage3-main", "system", 1_500_000),
            ("stage5-reliability", "reliability", 1_800_000),
        ),
        fault_plan=fault_plan,
    )
    return StageFixture(request=base.request, policy=MeetingStagePolicy())
```

- [ ] **Step 3: 运行 runner 测试并确认故障/lineage 行为失败**

Run: `uv run pytest tests/benchmarks/test_asr_stage_runner.py -q`

Expected: FAIL，缺少 `FaultScheduler`、checkpoint 或精确 EOF fault 顺序。

- [ ] **Step 4: 实现 canonical cursor fault scheduler**

```python
@dataclass
class FaultScheduler:
    pending: deque[FaultEvent]
    attempted_ids: set[str] = field(default_factory=set)

    def next_range(self, cursor: int, segment_end_ms: int) -> CursorRange:
        next_fault = self.pending[0] if self.pending else None
        end_ms = (
            segment_end_ms
            if next_fault is None
            else min(segment_end_ms, next_fault.cursor_ms)
        )
        return CursorRange(start_ms=cursor, end_ms=end_ms)
```

runner feed/fault 严格串行；到 cursor 时写 `planned → attempt_started → applied → recovered|failed|unknown`；
只有 recovered 进入 executed count。三个断线和 crash 在音频中途调用 `inject_fault()`；唯一
finalization delay 在 cursor 3_600_000 作为 `finalize(finalization_fault)` 参数传入，由 executor 记录
“EOF → delay → terminal”，wall duration 不推进 cursor。executor 对每个 slice 用
`ResolvedPCMInput.iter_frames(start_offset_ms=..., end_offset_ms=...)`，offset 由全局 cursor 减去当前
segment 起点得到；observation 记录 repetition/slice index，runner 合并为逻辑 segment 指标。
若 `next_range()` 返回零长度，runner 必须先注入并弹出该 fault，再计算下一 range，禁止把空 slice 传给
executor 或停在同一 cursor 忙循环。

- [ ] **Step 5: 实现 meeting composite policy 与 checkpoint**

`MeetingStagePolicy` 固定区间 `0–300_000 preflight`、`0–1_800_000 Stage 3`、`1_800_000–3_600_000 Stage 5`。30 分钟处 flush 并写 `checkpoints/stage3.json` 和 `metrics-stage3.json`，但不写 decision。物理 run terminal seal 后，两个文件进入同一 artifact index。preflight/Stage 3 有证据硬失败时 completed+Reject source；基础设施异常时 failed+No decision source。

- [ ] **Step 6: 运行 Stage 5/runner 回归**

Run: `uv run pytest tests/benchmarks/test_asr_stage_runner.py tests/benchmarks/test_asr_stage_contracts.py tests/benchmarks/test_asr_stage_artifacts.py -q`

Expected: PASS。

- [ ] **Step 7: 运行定点类型与 lint**

Run: `uv run mypy src/sona/benchmarks/asr/stage_runner.py src/sona/benchmarks/asr/stage_evaluators.py`

Expected: `Success: no issues found`。

Run: `uv run ruff check src/sona/benchmarks/asr/stage_runner.py src/sona/benchmarks/asr/stage_evaluators.py tests/benchmarks/test_asr_stage_runner.py`

Expected: `All checks passed!`。

- [ ] **Step 8: 运行 Mandatory Per-Commit Gate 并提交故障与复用会话**

```bash
git add src/sona/benchmarks/asr/stage_runner.py src/sona/benchmarks/asr/stage_evaluators.py tests/benchmarks/asr_stage_fakes.py tests/benchmarks/test_asr_stage_runner.py
git commit -m "feat(asr): 编排阶段故障与复用会话"
```

---

### Task 7: 从真实源制品构造 Stage 决策

**Files:**
- Create: `src/sona/benchmarks/asr/stage_decision.py`
- Create: `tests/benchmarks/test_asr_stage_decision.py`
- Modify: `src/sona/benchmarks/asr/stage_contracts.py`
- Modify: `tests/benchmarks/asr_stage_fakes.py`
- Modify: `tests/benchmarks/test_asr_stage_contracts.py`

**Interfaces:**
- Consumes: sealed run directory、`ArtifactIndex`、`StageGateEvidenceBundle`、Stage 1–4 reports、`FinalistSelectionEvidence`。
- Produces: `StageDecisionRequest`、`verify_stage_decision()`、`write_stage_decision_report()`；调用者不能传 duration、fault counts、gate map 或 `unique_finalist`；测试 helper `DecisionFixture/build_decision_fixture()`。

- [ ] **Step 1: 写篡改 artifact、synthetic、手填 finalist 和 fault mismatch 的失败测试**

```python
def test_decision_rejects_tampered_metrics(tmp_path: Path) -> None:
    sealed_stage5 = build_decision_fixture(tmp_path)
    sealed_stage5.metrics_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(StageEvidenceError, match="artifact hash mismatch"):
        verify_stage_decision(sealed_stage5.request)


def test_experimental_run_cannot_promote(tmp_path: Path) -> None:
    sealed_stage5 = build_decision_fixture(tmp_path, evidence_tier="experimental")
    with pytest.raises(StageEvidenceError, match="formal evidence"):
        verify_stage_decision(sealed_stage5.request)


def test_selection_is_derived_from_report_not_boolean(tmp_path: Path) -> None:
    sealed_stage5 = build_decision_fixture(tmp_path)
    sealed_stage5.write_selection(eligible=("fun", "qwen"), selected="fun")
    with pytest.raises(StageEvidenceError, match="unique finalist"):
        verify_stage_decision(sealed_stage5.request)
```

- [ ] **Step 2: 写 Promote 正路径与 Stage 3 slice 测试**

```python
def test_promote_is_built_from_sealed_sources(tmp_path: Path) -> None:
    sealed_stage5 = build_decision_fixture(tmp_path)
    report = verify_stage_decision(sealed_stage5.request)
    assert report.status == "Promote"
    assert report.actual_duration_ms == 3_600_000
    assert report.executed_fault_counts == {
        "disconnect": 3,
        "asr_crash": 1,
        "finalization_delay": 1,
    }
    assert report.unique_finalist


def test_stage3_report_uses_checkpoint_metrics_slice(tmp_path: Path) -> None:
    sealed_meeting_stage5 = build_decision_fixture(tmp_path)
    request = dataclasses.replace(sealed_meeting_stage5.request, stage=3)
    report = verify_stage_decision(request)
    assert report.metrics_sha256 == sha256_file(sealed_meeting_stage5.stage3_metrics_path)
```

在 `asr_stage_fakes.py` 增加：

```python
@dataclass(frozen=True)
class DecisionFixture:
    request: StageDecisionRequest
    metrics_path: Path
    stage3_metrics_path: Path
    selection_path: Path
    upstream_report_sha256s: Mapping[UpstreamStage, str]

    def write_selection(
        self,
        *,
        eligible: tuple[str, ...],
        selected: str,
    ) -> None:
        payload = FinalistSelectionEvidence(
            family_id="meeting",
            selected_candidate_id=selected,
            eligible_candidate_ids=eligible,
            upstream_report_sha256s=dict(self.upstream_report_sha256s),
        )
        self.selection_path.write_text(
            payload.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        self.selection_path.chmod(0o600)
```

`build_decision_fixture(tmp_path, evidence_tier="formal")` 必须用 `StageArtifactWriter` 建立一个
`stage=5, covered_stages=(3,5), executor_id="meeting-real-test"` 的 completed run，并精确写入：

- `metrics.json`：`canonical_audio_duration_ms=3_600_000`、`monotonic_wall_elapsed_ms=3_605_000`；
- `metrics-stage3.json`：window `0..1_800_000`；
- `fault-execution.jsonl`：d1/d2/d3 disconnect、crash、delay 各唯一且全部 `applied/recovered`；
- `checkpoints/stage3.json`、state、events、resources、failures、summary，随后 `seal()`；
- 八个 gate 全 passed 的 `StageGateEvidenceBundle`，每个 source hash 必须来自上述 indexed artifact；
- 一个含 meeting family、selected candidate `fun` 的真实 `Stage1DecisionReport`，以及 Stage 2–4
  `StageDecisionReport`，全部使用同 family/candidate/hash identity；
- `FinalistSelectionEvidence(eligible_candidate_ids=("fun",), selected_candidate_id="fun")`，并绑定四个
  upstream report hash；
- 所有 run、gate、selection、upstream、decision output 都在 `tmp_path/external`，mode `0600`。

helper 最后返回上述 paths 构造的 `DecisionFixture`；不得 monkeypatch verifier 或跳过 artifact index，确保
正路径测试覆盖真实 hash 交叉验证。

- [ ] **Step 3: 运行测试并确认 verifier 缺失**

Run: `uv run pytest tests/benchmarks/test_asr_stage_decision.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 4: 定义只含路径的 decision request 与严格 loader**

```python
@pydantic_dataclass(config=ConfigDict(extra="forbid", frozen=True))
class StageDecisionRequest:
    stage: StageNumber
    family_id: str
    candidate_id: str
    run_dir: Path
    gate_evidence_path: Path
    finalist_selection_path: Path
    upstream_report_paths: Mapping[UpstreamStage, Path]
    output_path: Path
    repository_root: Path


def verify_stage_decision(request: StageDecisionRequest) -> StageDecisionReport:
    run = verify_sealed_run(request.run_dir)
    gates = load_and_verify_gate_evidence(request.gate_evidence_path, run)
    upstream = load_and_verify_upstream_reports(request.upstream_report_paths, request)
    selection = load_and_verify_selection(request.finalist_selection_path, upstream, request)
    metrics = load_verified_stage_metrics(run, request.stage)
    faults = load_verified_fault_execution(run)
    return _build_report(request, run, gates, upstream, selection, metrics, faults)
```

`verify_sealed_run()` 验证 terminal completed、manifest/index hash、所有 indexed regular file/size/hash/mode；Stage 3 允许且只允许读取 `stage=5, covered_stages=(3,5)` 的 checkpoint slice。failed/deferred/unsealed 只能生成非 Promote 状态。

- [ ] **Step 5: 实现 gate、upstream、selection 与 Promote 交叉验证**

对八个固定 gate 的每个 source hash，必须在 artifact index 或显式 upstream report 中找到并重算；Stage 2–4 未测 gate 必须是 `not_applicable/unsupported`，Promote 时全部为 passed。Stage 1 report 从 `FamilyLookDecision.selected_candidate_id` 校验 family/candidate；Stage 2–4 report 顺序、身份和 hash 必须一致。`FinalistSelectionEvidence.eligible_candidate_ids` 必须恰好一个且等于 selected candidate；由 verifier 设置 `unique_finalist=True`，CLI 无该参数。

从 `metrics.json` 读取 `canonical_audio_duration_ms==3_600_000`、`monotonic_wall_elapsed_ms>=3_600_000`；从 fault JSONL 按唯一 event ID 统计 `applied+recovered`；任一 unknown/failed/重复/missing 使 Promote 失败。`write_stage_decision_report()` 使用不可覆盖、0600、原子 JSON。

- [ ] **Step 6: 运行 decision、contract、report 回归**

Run: `uv run pytest tests/benchmarks/test_asr_stage_decision.py tests/benchmarks/test_asr_stage_contracts.py tests/benchmarks/test_asr_report.py -q`

Expected: PASS。

- [ ] **Step 7: 运行定点类型与 lint**

Run: `uv run mypy src/sona/benchmarks/asr/stage_decision.py src/sona/benchmarks/asr/stage_contracts.py`

Expected: `Success: no issues found`。

Run: `uv run ruff check src/sona/benchmarks/asr/stage_decision.py src/sona/benchmarks/asr/stage_contracts.py tests/benchmarks/test_asr_stage_decision.py tests/benchmarks/test_asr_stage_contracts.py`

Expected: `All checks passed!`。

- [ ] **Step 8: 运行 Mandatory Per-Commit Gate 并提交决策 verifier**

```bash
git add src/sona/benchmarks/asr/stage_decision.py src/sona/benchmarks/asr/stage_contracts.py tests/benchmarks/asr_stage_fakes.py tests/benchmarks/test_asr_stage_decision.py tests/benchmarks/test_asr_stage_contracts.py
git commit -m "feat(asr): 验证阶段决策证据链"
```

---

### Task 8: 接入 CLI、更新科学方案并完成全量门禁

**Files:**
- Modify: `src/sona/benchmarks/asr/cli.py`
- Modify: `tests/benchmarks/test_asr_cli.py`
- Modify: `docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

**Interfaces:**
- Consumes: `StageExecutorRegistry`、`run_stage()`、`StageDecisionRequest`、`verify_stage_decision()`。
- Produces: `run-stage`、`decide-stage` CLI、`run_stage_from_request()`、`decide_stage_from_request()`；不在生产 registry 注册 synthetic executor。

- [ ] **Step 1: 写 parser、唯一锁 owner、formal synthetic 拒绝和 decide source 参数测试**

```python
def test_parser_exposes_stage_commands() -> None:
    parser = build_parser()
    run_args = parser.parse_args([
        "run-stage", "--request", "stage-request.json", "--repo-root", ".",
    ])
    decide_args = parser.parse_args([
        "decide-stage", "--request", "decision-request.json", "--repo-root", ".",
    ])
    assert run_args.command == "run-stage"
    assert decide_args.command == "decide-stage"


def test_run_stage_cli_does_not_take_a_second_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli_module, "run_stage_from_request", lambda *args: calls.append("run") or 0)
    assert main(["run-stage", "--request", "request.json", "--repo-root", "."]) == 0
    assert calls == ["run"]
```

- [ ] **Step 2: 运行 CLI 测试并确认 subcommand 缺失**

Run: `uv run pytest tests/benchmarks/test_asr_cli.py -q`

Expected: FAIL，argparse 不认识 `run-stage`/`decide-stage`。

- [ ] **Step 3: 增加稳定 CLI 边界**

```python
run_stage_parser = subparsers.add_parser("run-stage", help="执行 Stage 2-5 冻结运行")
run_stage_parser.add_argument("--request", required=True)
run_stage_parser.add_argument("--repo-root", default=".")

decide_stage_parser = subparsers.add_parser("decide-stage", help="从封存制品生成 Stage 决策")
decide_stage_parser.add_argument("--request", required=True)
decide_stage_parser.add_argument("--repo-root", default=".")
```

增加以下 helper；request loader 使用 `TypeAdapter` 严格拒绝 extra 字段，并将 CLI `repo_root` 作为唯一
repository boundary，拒绝 request 中不一致的 repo：

```python
def run_stage_from_request(
    request_path: Path,
    repository_root: Path,
    registry: StageExecutorRegistry,
) -> int:
    request = load_stage_run_request(request_path, repository_root)
    executor_factory = lambda: registry.create(request.executor_id)
    result = asyncio.run(
        run_stage(
            request,
            executor_factory=executor_factory,
            policy=build_stage_policy(request),
        )
    )
    return 0 if result.status in {"completed", "deferred"} else 1


def decide_stage_from_request(
    request_path: Path,
    repository_root: Path,
) -> int:
    request = load_stage_decision_request(request_path, repository_root)
    report = verify_stage_decision(request)
    write_stage_decision_report(request.output_path, report)
    return 0
```

`main()` 先验证 request path 在项目外，从 `_build_stage_executor_registry()` 按 request.executor_id 构造
轻量真实 executor factory，然后调用上述 helper；不得在 CLI 再调用 `exclusive_resource_lock()`。首版
registry 可以没有真实 Stage executor，未知 ID 稳定返回 exit 2；测试通过 monkeypatch registry 注入
synthetic，但 production module 不导入 test helper。`decide-stage` 只接收 request 路径，不暴露
`unique_finalist`、duration、fault count 或 gate map CLI 参数。边界捕获现有 `OSError/ValueError` 以及
`ResourceBusyError`、`ResourceQuarantinedError`、`StageRunnerError`、`StageEvidenceError`，统一输出脱敏
错误并返回 2。

- [ ] **Step 4: 更新科学方案的 runner 契约**

在 §8、§9.4、§9.5、§11、§12 明确：

```bash
uv run vr-asr-benchmark run-stage \
  --request "$ASR_BENCH_ROOT/stage-requests/stage2-meeting-qwen.json" \
  --repo-root .

uv run vr-asr-benchmark decide-stage \
  --request "$ASR_BENCH_ROOT/stage-decisions/meeting-fun-stage5-request.json" \
  --repo-root .
```

文档示例先定义 `ASR_BENCH_ROOT=/path/to/external/asr-benchmark`；不得写本机 wrapper、个人绝对路径或终端历史。补充 `state.json`、checkpoint、artifact index、quarantine、synthetic 非正式、Stage 3/5 lineage 和 Stage 1 finalist 后才注册真实 executor 的边界。

- [ ] **Step 5: 运行全部 Stage 定点测试**

Run: `uv run pytest tests/benchmarks/test_asr_stage_contracts.py tests/benchmarks/test_asr_stage_inputs.py tests/benchmarks/test_asr_stage_artifacts.py tests/benchmarks/test_asr_stage_executors.py tests/benchmarks/test_asr_stage_runner.py tests/benchmarks/test_asr_stage_decision.py tests/benchmarks/test_resource_lock.py tests/benchmarks/test_asr_cli.py -q`

Expected: PASS。

- [ ] **Step 6: 运行完整质量门禁，严格串行且不启动模型或服务**

Run: `SONA_TEST_DATABASE_URL=postgresql:///knowledge uv run pytest tests/`

Expected: 全部测试通过，branch coverage 不低于 80%。

Run: `uv run mypy src/`

Expected: `Success: no issues found`。

Run: `uv run ruff check src/ tests/`

Expected: `All checks passed!`。

Run: `cd ui && npm test -- --run`

Expected: 11 个 test files、62 个 tests 全部通过或更多。

Run: `cd ui && npm run build`

Expected: TypeScript 与 Vite production build 成功。

- [ ] **Step 7: 审计 diff、隐私、工作树和禁止项**

Run: `git diff --check`

Expected: 无输出。

Run: `rg -n '/Users/|file://|rtk ' src/sona/benchmarks/asr/stage_*.py tests/benchmarks/test_asr_stage_*.py docs/Fun-ASR与现有ASR后端科学对比测试方案.md`

Expected: 无输出；代码/文档中没有个人绝对路径、`file://` 或 RTK 持久化命令。

Run: `rg -n 'reference_raw|reference_normalized' src/sona/benchmarks/asr/stage_*.py`

Expected: 无输出；Stage runner 生产模块的类型边界不包含 reference。

Run: `git status --short`

Expected: 只包含本 Task 的 CLI、测试和文档变更。

- [ ] **Step 8: 提交 CLI 与文档（Step 6 已完成本 Task 的 Mandatory Per-Commit Gate）**

```bash
git add src/sona/benchmarks/asr/cli.py tests/benchmarks/test_asr_cli.py docs/Fun-ASR与现有ASR后端科学对比测试方案.md
git commit -m "feat(asr): 接入阶段执行器命令"
```

- [ ] **Step 9: 确认最终工作区和提交序列**

Run: `git status --short --branch`

Expected: 当前分支工作区干净。

Run: `git log -8 --oneline`

Expected: 能看到本计划的八个原子提交；不 push、不合并、不打 tag，除非用户另行明确授权。

---

## Spec Coverage Matrix

| 设计规范范围 | 实施任务 | 证明方式 |
|:---|:---|:---|
| §3 目标/非目标、项目外边界 | Tasks 1、2、5、8 | contract/path/CLI tests + diff privacy audit |
| §5 模块边界 | Tasks 1–7 | 每个模块单一责任、typed Interfaces block |
| §6 request/input/executor/registry | Tasks 1、2、4、5 | Pydantic contract、resolver 与 registry tests |
| §7 生命周期、失败保留、禁止 resume | Tasks 3、5 | state transition、partial/unsealed、no-overwrite tests |
| §8 Screen→Confirm 连续性 | Task 5 | start count、session、identity、cursor、未消费 Confirm assertions |
| §9 Stage 3/5 一次 60 分钟复用 | Task 6 | single session + checkpoint/metrics slice test |
| §10 cursor、五故障、EOF delay | Tasks 1、6 | fault contract + exact cursor/outcome/finalize order tests |
| §11 权限、原子写、ArtifactIndex | Task 3 | 0700/0600、symlink、hash、seal、post-seal tests |
| §12 决策证据链 | Task 7 | tamper、formal tier、selection、upstream、Promote tests |
| §13 资源互斥/quarantine | Task 5 | contention-no-side-effect、release、quarantine tests |
| §14 阶段 policy | Tasks 5、6 | pure policy 与 stage-specific synthetic integration tests |
| §15 稳定错误语义 | Tasks 2、3、4、5、7、8 | typed errors + CLI exit-2 boundary tests |
| §16 测试策略 | Tasks 1–8 | red/green targeted suites + complete gates |
| §17 实施/回退 | 每个 Task | 八个原子提交，不改生产默认，不 push |
| §18 验收标准 | Task 8 | 全部定点测试、后端/前端五门禁、最终 diff audit |
| §19 已知取舍 | Tasks 5、7、8 | unsealed no-resume、单向 report 引用、无真实 executor fallback |

本计划没有覆盖真实 Stage 2–5 executor：这与规范 §17 的首个实施单元一致。Stage 1 产生唯一 finalist
后，才为仍存活 family 编写对应真实 adapter 变更；该条件工作不属于当前八个提交。

---

## Plan Completion Criteria

- 八个任务均按 TDD 完成并各自提交。
- 所有设计规范 §18 验收项都有对应测试或完整门禁证据。
- 首版统一核心不启动真实模型/服务，不读取私人录音或 reference，不产生资源竞争。
- formal CLI 对未注册真实 executor fail-closed，不回退 synthetic。
- 工作区干净；提交只存在于 `feature/asr-benchmark-runner`，未改变远端状态。
