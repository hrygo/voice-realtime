"""ASR 对比实验的确定性基础指标。"""

from __future__ import annotations

import math
import random
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise

PRIMARY_NORMALIZATION_VERSION = "nfkc-casefold-punct-space-v1"


class MetricStatus(StrEnum):
    """禁止用伪零值表达缺失或不支持。"""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    NOT_APPLICABLE = "not_applicable"
    MISSING = "missing"


@dataclass(frozen=True)
class MetricValue:
    """带显式可用状态的标量指标。"""

    status: MetricStatus
    value: float | None


@dataclass(frozen=True)
class ErrorRateResult(MetricValue):
    """可追溯到 S/D/I/N 的字符或词错误率。"""

    substitutions: int
    deletions: int
    insertions: int
    reference_tokens: int


@dataclass(frozen=True)
class HotwordScores:
    """热词 precision、recall 与 F1。"""

    precision: MetricValue
    recall: MetricValue
    f1: MetricValue


@dataclass(frozen=True)
class TimedText:
    """在单调时间轴到达的一版 confirmed 文本。"""

    arrival_ms: float
    text: str


@dataclass(frozen=True)
class BootstrapDifference:
    """候选减基线的会话级配对 bootstrap 结果。"""

    paired_samples: int
    mean_difference: float
    ci_low: float
    ci_high: float


def _edit_counts(reference: Sequence[str], hypothesis: Sequence[str]) -> tuple[int, int, int]:
    """返回最小编辑路径的 substitutions、deletions、insertions。"""
    previous = [(index, 0, index) for index in range(len(hypothesis) + 1)]
    for ref_index, reference_token in enumerate(reference, start=1):
        current: list[tuple[int, int, int]] = [(0, ref_index, 0)]
        for hyp_index, hypothesis_token in enumerate(hypothesis, start=1):
            if reference_token == hypothesis_token:
                current.append(previous[hyp_index - 1])
                continue
            substitution = previous[hyp_index - 1]
            deletion = previous[hyp_index]
            insertion = current[hyp_index - 1]
            candidates = (
                (sum(substitution) + 1, 0, (substitution[0] + 1, substitution[1], substitution[2])),
                (sum(deletion) + 1, 1, (deletion[0], deletion[1] + 1, deletion[2])),
                (sum(insertion) + 1, 2, (insertion[0], insertion[1], insertion[2] + 1)),
            )
            current.append(min(candidates, key=lambda item: (item[0], item[1]))[2])
        previous = current
    return previous[-1]


def _error_rate(reference: Sequence[str], hypothesis: Sequence[str]) -> ErrorRateResult:
    if not reference:
        return ErrorRateResult(
            status=MetricStatus.NOT_APPLICABLE,
            value=None,
            substitutions=0,
            deletions=0,
            insertions=len(hypothesis),
            reference_tokens=0,
        )
    substitutions, deletions, insertions = _edit_counts(reference, hypothesis)
    return ErrorRateResult(
        status=MetricStatus.SUPPORTED,
        value=(substitutions + deletions + insertions) / len(reference),
        substitutions=substitutions,
        deletions=deletions,
        insertions=insertions,
        reference_tokens=len(reference),
    )


def character_error_rate(reference: str, hypothesis: str) -> ErrorRateResult:
    """计算已按同一规则归一化文本的 CER。"""
    reference_tokens = tuple(character for character in reference if not character.isspace())
    hypothesis_tokens = tuple(character for character in hypothesis if not character.isspace())
    return _error_rate(reference_tokens, hypothesis_tokens)


def normalize_primary_text(text: str) -> str:
    """应用主指标冻结的 NFKC、casefold、标点和空白规则。"""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )


def word_error_rate(reference: str, hypothesis: str) -> ErrorRateResult:
    """按空白 token 计算已归一化英文文本的 WER。"""
    return _error_rate(tuple(reference.split()), tuple(hypothesis.split()))


def hotword_scores(reference: str, hypothesis: str, hotwords: Sequence[str]) -> HotwordScores:
    """按预冻结热词表和出现次数计算 precision、recall、F1。"""
    normalized_hotwords = tuple(word.strip().casefold() for word in hotwords if word.strip())
    reference_text = reference.casefold()
    hypothesis_text = hypothesis.casefold()
    reference_counts = Counter(
        {word: reference_text.count(word) for word in normalized_hotwords}
    )
    hypothesis_counts = Counter(
        {word: hypothesis_text.count(word) for word in normalized_hotwords}
    )
    correct = sum(
        min(reference_counts[word], hypothesis_counts[word]) for word in normalized_hotwords
    )
    reference_total = sum(reference_counts.values())
    hypothesis_total = sum(hypothesis_counts.values())
    precision_value = correct / hypothesis_total if hypothesis_total else None
    recall_value = correct / reference_total if reference_total else None
    precision = MetricValue(
        status=(
            MetricStatus.SUPPORTED
            if precision_value is not None
            else MetricStatus.NOT_APPLICABLE
        ),
        value=precision_value,
    )
    recall = MetricValue(
        status=MetricStatus.SUPPORTED if recall_value is not None else MetricStatus.NOT_APPLICABLE,
        value=recall_value,
    )
    if precision_value is None or recall_value is None or precision_value + recall_value == 0:
        f1 = MetricValue(
            status=(
                MetricStatus.SUPPORTED
                if precision_value is not None and recall_value is not None
                else MetricStatus.NOT_APPLICABLE
            ),
            value=0.0 if precision_value is not None and recall_value is not None else None,
        )
    else:
        f1 = MetricValue(
            status=MetricStatus.SUPPORTED,
            value=2 * precision_value * recall_value / (precision_value + recall_value),
        )
    return HotwordScores(precision=precision, recall=recall, f1=f1)


def revision_burden(partials: Sequence[str] | None, *, final_text: str) -> MetricValue:
    """计算相邻 partial 编辑总量除以 final 字符数。"""
    if partials is None:
        return MetricValue(status=MetricStatus.UNSUPPORTED, value=None)
    final_tokens = tuple(character for character in final_text if not character.isspace())
    if not final_tokens:
        return MetricValue(status=MetricStatus.NOT_APPLICABLE, value=None)
    edits = 0
    for previous, current in pairwise(partials):
        edits += sum(_edit_counts(tuple(previous), tuple(current)))
    return MetricValue(status=MetricStatus.SUPPORTED, value=edits / len(final_tokens))


def commit_latency(
    word: str,
    *,
    reference_end_ms: float | None,
    confirmed: Sequence[TimedText] | None,
) -> MetricValue:
    """返回目标词首次进入且此后不再回滚的 confirmed 延迟。"""
    if reference_end_ms is None or confirmed is None:
        return MetricValue(status=MetricStatus.UNSUPPORTED, value=None)
    normalized_word = word.strip()
    if not normalized_word:
        return MetricValue(status=MetricStatus.NOT_APPLICABLE, value=None)
    for index, event in enumerate(confirmed):
        if normalized_word not in event.text:
            continue
        if all(normalized_word in later.text for later in confirmed[index:]):
            return MetricValue(
                status=MetricStatus.SUPPORTED,
                value=event.arrival_ms - reference_end_ms,
            )
    return MetricValue(status=MetricStatus.MISSING, value=None)


def realtime_factor(*, wall_time_ms: float, audio_duration_ms: float) -> MetricValue:
    """计算 RTF；无正时长时不伪造零值。"""
    if audio_duration_ms <= 0:
        return MetricValue(status=MetricStatus.NOT_APPLICABLE, value=None)
    if wall_time_ms < 0 or not math.isfinite(wall_time_ms):
        return MetricValue(status=MetricStatus.MISSING, value=None)
    return MetricValue(status=MetricStatus.SUPPORTED, value=wall_time_ms / audio_duration_ms)


def percentile(values: Sequence[float], percentile_value: float) -> MetricValue:
    """用线性插值计算 0..100 百分位。"""
    if not 0 <= percentile_value <= 100:
        raise ValueError("percentile 必须位于 0..100")
    finite_values = sorted(value for value in values if math.isfinite(value))
    if not finite_values:
        return MetricValue(status=MetricStatus.MISSING, value=None)
    rank = (len(finite_values) - 1) * percentile_value / 100
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        result = finite_values[lower]
    else:
        fraction = rank - lower
        result = finite_values[lower] + (finite_values[upper] - finite_values[lower]) * fraction
    return MetricValue(status=MetricStatus.SUPPORTED, value=result)


def cluster_bootstrap_difference(
    baseline: Mapping[str, float],
    candidate: Mapping[str, float],
    *,
    iterations: int = 10_000,
    seed: int = 0,
) -> BootstrapDifference:
    """对共享会话 ID 做候选减基线的配对 cluster bootstrap。"""
    if iterations <= 0:
        raise ValueError("iterations 必须为正数")
    sample_ids = sorted(baseline.keys() & candidate.keys())
    if not sample_ids:
        raise ValueError("baseline 与 candidate 没有配对样本")
    differences = [candidate[sample_id] - baseline[sample_id] for sample_id in sample_ids]
    if any(not math.isfinite(value) for value in differences):
        raise ValueError("配对指标必须是有限数值")
    random_generator = random.Random(seed)
    bootstrapped = [
        sum(random_generator.choice(differences) for _ in differences) / len(differences)
        for _ in range(iterations)
    ]
    low = percentile(bootstrapped, 2.5).value
    high = percentile(bootstrapped, 97.5).value
    if low is None or high is None:  # pragma: no cover - finite non-empty input guarantees values
        raise RuntimeError("bootstrap percentile calculation failed")
    return BootstrapDifference(
        paired_samples=len(sample_ids),
        mean_difference=sum(differences) / len(differences),
        ci_low=low,
        ci_high=high,
    )


def stratified_cluster_bootstrap_difference(
    differences_by_stratum: Mapping[str, Sequence[float]],
    *,
    iterations: int = 10_000,
    seed: int = 0,
) -> BootstrapDifference:
    """在层内重采样独立样本；仅用于未提供 cluster 身份的敏感性分析。"""
    if iterations <= 0:
        raise ValueError("iterations 必须为正数")
    groups = {
        stratum: tuple(values)
        for stratum, values in differences_by_stratum.items()
        if values
    }
    if not groups:
        raise ValueError("stratified bootstrap requires paired samples")
    all_values = [value for values in groups.values() for value in values]
    if any(not math.isfinite(value) for value in all_values):
        raise ValueError("配对指标必须是有限数值")

    def macro_average(sampled_groups: Mapping[str, Sequence[float]]) -> float:
        stratum_means = [sum(values) / len(values) for values in sampled_groups.values()]
        return sum(stratum_means) / len(stratum_means)

    random_generator = random.Random(seed)
    bootstrapped = []
    for _ in range(iterations):
        sampled = {
            stratum: tuple(random_generator.choice(values) for _ in values)
            for stratum, values in groups.items()
        }
        bootstrapped.append(macro_average(sampled))
    low = percentile(bootstrapped, 2.5).value
    high = percentile(bootstrapped, 97.5).value
    if low is None or high is None:  # pragma: no cover - validated input guarantees values
        raise RuntimeError("bootstrap percentile calculation failed")
    return BootstrapDifference(
        paired_samples=len(all_values),
        mean_difference=macro_average(groups),
        ci_low=low,
        ci_high=high,
    )


def stratified_grouped_cluster_bootstrap_difference(
    differences_by_stratum: Mapping[str, Mapping[str, Sequence[float]]],
    *,
    iterations: int = 10_000,
    seed: int = 0,
) -> BootstrapDifference:
    """在场景内重采样完整 cluster，再对场景样本均值等权汇总。"""
    if iterations <= 0:
        raise ValueError("iterations 必须为正数")
    groups = {
        stratum: {
            cluster_id: tuple(values)
            for cluster_id, values in clusters.items()
            if values
        }
        for stratum, clusters in differences_by_stratum.items()
    }
    groups = {stratum: clusters for stratum, clusters in groups.items() if clusters}
    if not groups:
        raise ValueError("stratified cluster bootstrap requires paired clusters")
    all_values = [
        value
        for clusters in groups.values()
        for values in clusters.values()
        for value in values
    ]
    if any(not math.isfinite(value) for value in all_values):
        raise ValueError("配对指标必须是有限数值")

    def macro_average(
        sampled_groups: Mapping[str, Sequence[float]],
    ) -> float:
        stratum_means = [sum(values) / len(values) for values in sampled_groups.values()]
        return sum(stratum_means) / len(stratum_means)

    observed = {
        stratum: tuple(
            value for values in clusters.values() for value in values
        )
        for stratum, clusters in groups.items()
    }
    random_generator = random.Random(seed)
    bootstrapped: list[float] = []
    for _ in range(iterations):
        sampled: dict[str, tuple[float, ...]] = {}
        for stratum, clusters in groups.items():
            cluster_values = tuple(clusters.values())
            selected = tuple(
                random_generator.choice(cluster_values)
                for _ in cluster_values
            )
            sampled[stratum] = tuple(
                value for values in selected for value in values
            )
        bootstrapped.append(macro_average(sampled))
    low = percentile(bootstrapped, 2.5).value
    high = percentile(bootstrapped, 97.5).value
    if low is None or high is None:  # pragma: no cover - validated input guarantees values
        raise RuntimeError("bootstrap percentile calculation failed")
    return BootstrapDifference(
        paired_samples=len(all_values),
        mean_difference=macro_average(observed),
        ci_low=low,
        ci_high=high,
    )
