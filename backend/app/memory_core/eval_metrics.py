"""Typed metric value objects and pure computation functions (P5).

All functions are deterministic and side-effect free.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    """Aggregate retrieval precision, recall, and F1."""

    precision: float
    recall: float
    f1: float

@dataclass(frozen=True, slots=True)
class LatencyPercentiles:
    """Latency distribution summary in milliseconds."""

    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    sample_count: int


@dataclass(frozen=True, slots=True)
class IdempotencyResult:
    """Result of the repeated-query idempotency check."""

    passed: bool
    checked_count: int
    mismatched_count: int


@dataclass(frozen=True, slots=True)
class FreshnessStats:
    """Freshness measurement across retrieved items."""

    avg_age_days: float
    max_age_days: float
    within_threshold_count: int
    applicable_count: int


@dataclass(frozen=True, slots=True)
class PerItemResult:
    """Evaluation outcome for a single holdout query item."""

    item_id: str
    category: str
    expected_memory_ids: list[str]
    retrieved_memory_ids: list[str]
    expected_refusal: bool
    actual_refusal: bool
    latency_ms: float
    item_precision: float
    item_recall: float


@dataclass(frozen=True, slots=True)
class EvaluationMeasurement:
    """All measured metrics for one evaluation run.

    Fields without defaults are always measured.  Fields with
    ``default=None`` may be absent when the corresponding measurement
    mode was not executed (e.g. live-stack on an in-process-only run).
    """

    # Always measured
    evidence_coverage: float
    refusal_precision: float
    refusal_recall: float
    retrieval_precision: float
    retrieval_recall: float
    retrieval_f1: float
    hot_query_latency: LatencyPercentiles
    idempotency: IdempotencyResult
    freshness: FreshnessStats
    total_items: int = 0

    # Optional — only populated for live-stack runs
    live_query_latency: LatencyPercentiles | None = None


def compute_evidence_coverage(
    results: Sequence[PerItemResult],
) -> float:
    """Aggregate recall for items that expect non-empty retrieval.

    Returns 1.0 when no items expect evidence (vacuous truth).
    """
    total_expected = 0
    total_retrieved = 0
    for r in results:
        if r.expected_refusal or not r.expected_memory_ids:
            continue
        total_expected += len(r.expected_memory_ids)
        relevant = set(r.expected_memory_ids) & set(r.retrieved_memory_ids)
        total_retrieved += len(relevant)
    if total_expected == 0:
        return 1.0
    return total_retrieved / total_expected


def compute_refusal_precision(results: Sequence[PerItemResult]) -> float:
    """Of all actual refusals, what fraction should have been refused.

    Returns 1.0 when the system never refuses and no items expect refusal
    (vacuous truth).  Returns 0.0 when the system refuses but nothing
    expected refusal.
    """
    actual_refusals = [r for r in results if r.actual_refusal]
    if not actual_refusals:
        return 0.0 if any(r.expected_refusal for r in results) else 1.0
    true_positives = sum(1 for r in actual_refusals if r.expected_refusal)
    return true_positives / len(actual_refusals)


def compute_refusal_recall(results: Sequence[PerItemResult]) -> float:
    """Of all items that should be refused, what fraction were refused.

    Returns 1.0 when no items expect refusal (vacuous truth).
    """
    expected_refusals = [r for r in results if r.expected_refusal]
    if not expected_refusals:
        return 1.0
    true_positives = sum(1 for r in expected_refusals if r.actual_refusal)
    return true_positives / len(expected_refusals)


def compute_retrieval_metrics(
    results: Sequence[PerItemResult],
) -> RetrievalMetrics:
    """Macro-averaged precision, recall, and F1 across all items."""
    if not results:
        return RetrievalMetrics(precision=0.0, recall=0.0, f1=0.0)

    precisions: list[float] = []
    recalls: list[float] = []
    for r in results:
        expected = set(r.expected_memory_ids)
        retrieved = set(r.retrieved_memory_ids)
        if not expected and not retrieved:
            precisions.append(1.0)
            recalls.append(1.0)
        elif not expected:
            precisions.append(0.0)
            recalls.append(1.0)
        elif not retrieved:
            precisions.append(1.0 if not expected else 0.0)
            recalls.append(0.0)
        else:
            overlap = expected & retrieved
            precisions.append(len(overlap) / len(retrieved))
            recalls.append(len(overlap) / len(expected))

    precision = mean(precisions) if precisions else 0.0
    recall = mean(recalls) if recalls else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    return RetrievalMetrics(precision=precision, recall=recall, f1=f1)


def compute_latency_percentiles(latencies_ms: Sequence[float]) -> LatencyPercentiles:
    """Compute P50 / P95 / P99 / max from a sequence of latency samples."""
    if not latencies_ms:
        return LatencyPercentiles(
            p50_ms=0.0, p95_ms=0.0, p99_ms=0.0, max_ms=0.0, sample_count=0
        )
    sorted_vals = sorted(latencies_ms)
    count = len(sorted_vals)
    return LatencyPercentiles(
        p50_ms=_percentile(sorted_vals, 50),
        p95_ms=_percentile(sorted_vals, 95),
        p99_ms=_percentile(sorted_vals, 99),
        max_ms=sorted_vals[-1],
        sample_count=count,
    )


def compute_idempotency(
    pairs: Sequence[tuple[list[str], list[str]]],
) -> IdempotencyResult:
    """Check whether repeated queries return identical result sets.

    *pairs* is a sequence of ``(first_run_ids, second_run_ids)`` tuples.
    """
    if not pairs:
        return IdempotencyResult(passed=True, checked_count=0, mismatched_count=0)
    mismatched = sum(
        1 for first, second in pairs if set(first) != set(second)
    )
    return IdempotencyResult(
        passed=mismatched == 0,
        checked_count=len(pairs),
        mismatched_count=mismatched,
    )


def compute_freshness_stats(
    ages_days: Sequence[float],
    max_days_threshold: int | None,
) -> FreshnessStats:
    """Summarise item ages and count items within the freshness threshold."""
    if not ages_days:
        return FreshnessStats(
            avg_age_days=0.0, max_age_days=0.0,
            within_threshold_count=0, applicable_count=0,
        )
    threshold = (
        float(max_days_threshold) if max_days_threshold is not None else None
    )
    within = (
        sum(1 for a in ages_days if threshold is not None and a <= threshold)
        if threshold is not None
        else len(ages_days)
    )
    return FreshnessStats(
        avg_age_days=mean(ages_days),
        max_age_days=max(ages_days),
        within_threshold_count=within,
        applicable_count=len(ages_days),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile on an already-sorted sequence."""
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (n - 1)
    lower = int(rank)
    upper = min(lower + 1, n - 1)
    weight = rank - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight
