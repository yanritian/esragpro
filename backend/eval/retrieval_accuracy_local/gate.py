from __future__ import annotations

import math
from dataclasses import dataclass

from .metrics import MetricValues


@dataclass(frozen=True, slots=True)
class BenchmarkThresholds:
    min_recall_at_10: float = 0.95
    min_ndcg_at_10: float = 0.85
    min_end_to_end_correctness: float = 0.90
    min_refusal_precision: float = 0.85
    min_refusal_recall: float = 0.85
    required_evidence_coverage: float = 1.0
    min_answerable_count: int = 40
    min_refusal_count: int = 20


@dataclass(frozen=True, slots=True)
class GateDecision:
    passed: bool
    blockers: tuple[str, ...]
    thresholds: BenchmarkThresholds


def _check_metric(
    blockers: list[str],
    name: str,
    value: float | None,
    threshold: float,
    *,
    exact: bool = False,
) -> None:
    if value is None:
        blockers.append(f"{name}_unmeasured")
        return
    if not math.isfinite(value):
        blockers.append(f"{name}_nonfinite")
        return
    passed = value == threshold if exact else value >= threshold
    if not passed:
        blockers.append(f"{name}={value:.6f} below required {threshold:.6f}")


def evaluate_gate(
    metrics: MetricValues | None,
    *,
    validation_errors: tuple[str, ...] = (),
    thresholds: BenchmarkThresholds | None = None,
) -> GateDecision:
    applied = thresholds or BenchmarkThresholds()
    blockers = list(validation_errors)
    if metrics is None:
        blockers.append("metrics_unmeasured")
        return GateDecision(passed=False, blockers=tuple(blockers), thresholds=applied)
    if metrics.answerable_count < applied.min_answerable_count:
        blockers.append("answerable_count_below_minimum")
    if metrics.refusal_count < applied.min_refusal_count:
        blockers.append("refusal_count_below_minimum")
    if metrics.measured_query_count <= 0:
        blockers.append("empty_measurement")
    if metrics.judgment_count <= 0:
        blockers.append("judgments_unmeasured")
    _check_metric(blockers, "recall_at_10", metrics.recall_at_10, applied.min_recall_at_10)
    _check_metric(blockers, "ndcg_at_10", metrics.ndcg_at_10, applied.min_ndcg_at_10)
    _check_metric(
        blockers,
        "end_to_end_correctness",
        metrics.end_to_end_correctness,
        applied.min_end_to_end_correctness,
    )
    _check_metric(blockers, "refusal_precision", metrics.refusal_precision, applied.min_refusal_precision)
    _check_metric(blockers, "refusal_recall", metrics.refusal_recall, applied.min_refusal_recall)
    _check_metric(
        blockers,
        "evidence_coverage",
        metrics.evidence_coverage,
        applied.required_evidence_coverage,
        exact=True,
    )
    return GateDecision(passed=not blockers, blockers=tuple(blockers), thresholds=applied)
