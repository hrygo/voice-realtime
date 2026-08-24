"""公共代理候选的确定性精确时长选择。"""

from __future__ import annotations

import hashlib
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SelectionCandidate:
    """不含逐字稿的最小候选选择身份。"""

    candidate_id: str
    duration_ms: int
    scenario: str

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("candidate_id cannot be empty")
        if self.duration_ms <= 0:
            raise ValueError("duration_ms must be positive")
        if not self.scenario.strip():
            raise ValueError("scenario cannot be empty")


def _selection_key(candidate: SelectionCandidate, seed: str) -> tuple[bytes, str]:
    digest = hashlib.sha256(
        f"{seed}\0{candidate.candidate_id}".encode()
    ).digest()
    return digest, candidate.candidate_id


def select_exact_duration(
    candidates: Sequence[SelectionCandidate],
    *,
    target_duration_ms: int,
    seed: str,
) -> tuple[SelectionCandidate, ...]:
    """以确定性 subset-sum 选择完整候选；禁止裁剪、填充或近似配额。"""
    if target_duration_ms <= 0:
        raise ValueError("target_duration_ms must be positive")
    if not seed:
        raise ValueError("seed cannot be empty")
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("candidate_id must be unique")
    ordered = tuple(
        sorted(
            (
                candidate
                for candidate in candidates
                if candidate.duration_ms <= target_duration_ms
            ),
            key=lambda candidate: _selection_key(candidate, seed),
        )
    )
    reachable = 1
    mask = (1 << (target_duration_ms + 1)) - 1
    predecessor_candidate = array("i", [-1]) * (target_duration_ms + 1)
    predecessor_sum = array("i", [-1]) * (target_duration_ms + 1)
    for index, candidate in enumerate(ordered):
        new_sums = ((reachable << candidate.duration_ms) & mask) & ~reachable
        reachable |= new_sums
        while new_sums:
            least_bit = new_sums & -new_sums
            total = least_bit.bit_length() - 1
            predecessor_candidate[total] = index
            predecessor_sum[total] = total - candidate.duration_ms
            new_sums ^= least_bit
        if reachable & (1 << target_duration_ms):
            break
    if not reachable & (1 << target_duration_ms):
        raise ValueError("candidate pool cannot satisfy exact duration quota")
    selected: list[SelectionCandidate] = []
    remaining = target_duration_ms
    while remaining:
        candidate_index = predecessor_candidate[remaining]
        previous = predecessor_sum[remaining]
        if candidate_index < 0 or previous < 0:
            raise RuntimeError("exact duration predecessor chain is incomplete")
        selected.append(ordered[candidate_index])
        remaining = previous
    return tuple(sorted(selected, key=lambda candidate: candidate.candidate_id))


def select_scenario_quotas(
    candidates: Sequence[SelectionCandidate],
    *,
    quotas_ms: Mapping[str, int],
    seed: str,
) -> dict[str, tuple[SelectionCandidate, ...]]:
    """分别冻结每个唯一主场景配额，避免正交标签重复累计。"""
    observed = {candidate.scenario for candidate in candidates}
    if observed - set(quotas_ms):
        raise ValueError("candidate contains an undeclared primary scenario")
    selected: dict[str, tuple[SelectionCandidate, ...]] = {}
    for scenario, target_duration_ms in sorted(quotas_ms.items()):
        pool = tuple(candidate for candidate in candidates if candidate.scenario == scenario)
        selected[scenario] = select_exact_duration(
            pool,
            target_duration_ms=target_duration_ms,
            seed=f"{seed}:{scenario}",
        )
    return selected
