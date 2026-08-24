"""公共代理语料的确定性精确配额选择测试。"""

from __future__ import annotations

import pytest

from voice_realtime.benchmarks.asr.public_proxy import (
    SelectionCandidate,
    select_exact_duration,
    select_scenario_quotas,
)


def _candidate(
    candidate_id: str,
    duration_ms: int,
    scenario: str = "meeting",
) -> SelectionCandidate:
    return SelectionCandidate(
        candidate_id=candidate_id,
        duration_ms=duration_ms,
        scenario=scenario,
    )


def test_exact_duration_selection_is_deterministic_and_does_not_pad_or_trim() -> None:
    candidates = tuple(
        _candidate(candidate_id, duration)
        for candidate_id, duration in (
            ("a", 1_000),
            ("b", 2_000),
            ("c", 3_000),
            ("d", 4_000),
            ("e", 5_000),
        )
    )

    first = select_exact_duration(candidates, target_duration_ms=9_000, seed="fixed")
    second = select_exact_duration(
        tuple(reversed(candidates)), target_duration_ms=9_000, seed="fixed"
    )

    assert first == second
    assert sum(candidate.duration_ms for candidate in first) == 9_000
    assert len({candidate.candidate_id for candidate in first}) == len(first)


def test_exact_duration_selection_rejects_duplicate_ids_and_unreachable_quota() -> None:
    duplicate = (_candidate("same", 1_000), _candidate("same", 2_000))
    with pytest.raises(ValueError, match="candidate_id"):
        select_exact_duration(duplicate, target_duration_ms=1_000, seed="fixed")

    with pytest.raises(ValueError, match="exact duration"):
        select_exact_duration(
            (_candidate("a", 2_000), _candidate("b", 4_000)),
            target_duration_ms=3_000,
            seed="fixed",
        )


def test_scenario_selection_applies_each_primary_quota_independently() -> None:
    candidates = (
        _candidate("m1", 1_000),
        _candidate("m2", 2_000),
        _candidate("m3", 3_000),
        _candidate("n1", 2_000, "negative"),
        _candidate("n2", 3_000, "negative"),
    )

    selected = select_scenario_quotas(
        candidates,
        quotas_ms={"meeting": 4_000, "negative": 5_000},
        seed="fixed",
    )

    durations = {
        scenario: sum(item.duration_ms for item in items)
        for scenario, items in selected.items()
    }
    assert durations == {
        "meeting": 4_000,
        "negative": 5_000,
    }
    assert set(selected) == {"meeting", "negative"}
