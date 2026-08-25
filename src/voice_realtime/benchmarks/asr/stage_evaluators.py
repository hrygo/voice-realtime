"""纯函数 stage policy 与 Screen/Confirm 观察门禁。

Policy 只消费 runner 已记录的 observation；它不读取文件、不启动运行时，也不
修改制品。生命周期、资源锁和状态快照全部由 ``stage_runner`` 负责。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Protocol, cast

from voice_realtime.benchmarks.asr.stage_contracts import (
    ScheduleManifest,
    ScheduleSegment,
    StagePhase,
)
from voice_realtime.benchmarks.asr.stage_executors import (
    CursorRange,
    SegmentObservation,
)


class ScreenDecision(StrEnum):
    """Screen 阶段的唯一终态决策。"""

    PASS = "Screen-Pass"
    FAIL = "Screen-Fail"


class StagePolicy(Protocol):
    """可注入、无副作用的阶段策略。"""

    def phase_for(self, segment: ScheduleSegment) -> StagePhase: ...

    def evaluate_screen(
        self, observations: tuple[SegmentObservation, ...]
    ) -> ScreenDecision: ...


class DefaultStagePolicy:
    """默认合成/冒烟策略：只验证有 Screen observation 后继续。"""

    def phase_for(self, segment: ScheduleSegment) -> StagePhase:
        return cast(StagePhase, segment.purpose)

    def evaluate_screen(
        self, observations: tuple[SegmentObservation, ...]
    ) -> ScreenDecision:
        return ScreenDecision.PASS if observations else ScreenDecision.FAIL


@dataclass(frozen=True, slots=True)
class MeetingStagePolicy:
    """Stage 5 meeting policy with the Stage 3/5 composite windows.

    The policy is intentionally pure: it classifies schedule segments and
    exposes the fixed windows, while the runner owns checkpoint I/O and the
    executor lifecycle.
    """

    STAGE3_WINDOW: ClassVar[CursorRange] = CursorRange(0, 1_800_000)
    STAGE5_WINDOW: ClassVar[CursorRange] = CursorRange(1_800_000, 3_600_000)

    @property
    def stage3_window(self) -> CursorRange:
        return self.STAGE3_WINDOW

    @property
    def stage5_window(self) -> CursorRange:
        return self.STAGE5_WINDOW

    def phase_for(self, segment: ScheduleSegment) -> StagePhase:
        if segment.purpose == "reliability":
            return "reliability"
        if segment.segment_id.lower().startswith("preflight"):
            return "preflight"
        if segment.purpose == "system":
            return "confirm"
        return cast(StagePhase, segment.purpose)

    def evaluate_screen(
        self, observations: tuple[SegmentObservation, ...]
    ) -> ScreenDecision:
        return ScreenDecision.PASS if observations else ScreenDecision.FAIL

    def is_stage5_composite(self, *, covered_stages: tuple[int, ...], family_id: str) -> bool:
        return covered_stages == (3, 5) and family_id == "meeting"


@dataclass(frozen=True, slots=True)
class InteractionStagePolicy:
    """Pure policy for interaction schedules; it performs no runtime work."""

    def phase_for(self, segment: ScheduleSegment) -> StagePhase:
        return cast(StagePhase, segment.purpose)

    def evaluate_screen(
        self, observations: tuple[SegmentObservation, ...]
    ) -> ScreenDecision:
        return ScreenDecision.PASS if observations else ScreenDecision.FAIL


def validate_composite_schedule(
    schedule: ScheduleManifest,
    covered_stages: tuple[int, ...] = (3, 5),
) -> None:
    """Require the frozen meeting Stage 3/5 composite schedule shape."""

    expected = (
        ("preflight", "system", 300_000),
        ("stage3-main", "system", 1_500_000),
        ("stage5-reliability", "reliability", 1_800_000),
    )
    actual = tuple(
        (segment.segment_id, segment.purpose, segment.duration_ms, segment.repetition)
        for segment in schedule.segments
    )
    if covered_stages != (3, 5):
        raise ValueError("composite schedule requires covered_stages (3, 5)")
    if schedule.stage != 5 or schedule.family_id != "meeting":
        raise ValueError("composite schedule must be a Stage 5 meeting schedule")
    if actual != tuple((*item, 1) for item in expected):
        raise ValueError("composite schedule does not match the frozen Stage 3/5 windows")
    if schedule.total_duration_ms != 3_600_000:
        raise ValueError("composite schedule duration must be 3600000 ms")


def validate_screen_observations(
    observations: Sequence[SegmentObservation],
) -> None:
    """验证 Screen observation 的会话/游标连续性。

    该函数只做 policy 输入校验，不能替代 runner 对每次 feed 前的输入 hash
    验证；它故意不检查具体模型或文件路径。
    """

    if not observations:
        raise ValueError("screen observations must not be empty")
    first = observations[0]
    previous_end = first.cursor.start_ms
    for observation in observations:
        if observation.session_id != first.session_id:
            raise ValueError("screen observations must share one session")
        if observation.source_epoch != first.source_epoch:
            raise ValueError("screen observations must share one source epoch")
        if observation.cursor.start_ms != previous_end:
            raise ValueError("screen observation cursors must be contiguous")
        previous_end = observation.cursor.end_ms


__all__ = [
    "DefaultStagePolicy",
    "InteractionStagePolicy",
    "MeetingStagePolicy",
    "ScreenDecision",
    "StagePolicy",
    "validate_composite_schedule",
    "validate_screen_observations",
]
