"""纯函数 stage policy 与 Screen/Confirm 观察门禁。

Policy 只消费 runner 已记录的 observation；它不读取文件、不启动运行时，也不
修改制品。生命周期、资源锁和状态快照全部由 ``stage_runner`` 负责。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol, cast

from voice_realtime.benchmarks.asr.stage_contracts import (
    ScheduleSegment,
    StagePhase,
)
from voice_realtime.benchmarks.asr.stage_executors import (
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
    "ScreenDecision",
    "StagePolicy",
    "validate_screen_observations",
]
