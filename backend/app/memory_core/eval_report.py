"""Typed report structure for P5 evaluation (no Any / no object).

Every report field is a frozen slotted dataclass with an explicit type.
Conversion from the evaluator's measurement types to report record
types is done with explicit field-by-field mapping — no generic
reflection or escape hatches.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.memory_core.eval_gate import ProjectionHealth, ReleaseDecision
from app.memory_core.eval_metrics import (
    EvaluationMeasurement,
    LatencyPercentiles,
)
from app.memory_core.evaluator import EvaluationRunResult

# ---------------------------------------------------------------------------
# Leaf record types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LatencyRecord:
    """Latency distribution summary for the JSON report."""

    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    sample_count: int


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """Idempotency check result for the JSON report."""

    passed: bool
    checked_count: int
    mismatched_count: int


@dataclass(frozen=True, slots=True)
class FreshnessRecord:
    """Freshness measurement for the JSON report."""

    avg_age_days: float
    max_age_days: float
    within_threshold_count: int
    applicable_count: int


# ---------------------------------------------------------------------------
# Composite record types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MeasurementRecord:
    """Complete measurement block for the JSON report."""

    evidence_coverage: float
    refusal_precision: float
    refusal_recall: float
    retrieval_precision: float
    retrieval_recall: float
    retrieval_f1: float
    hot_query_latency: LatencyRecord
    idempotency: IdempotencyRecord
    freshness: FreshnessRecord
    total_items: int
    corpus_hash: str
    live_query_latency: LatencyRecord | None = None


@dataclass(frozen=True, slots=True)
class ProjectionHealthRecord:
    """Per-projection health record for the JSON report."""

    kind: str
    enabled: bool
    healthy: bool
    lag_items: int
    checked_at: str


@dataclass(frozen=True, slots=True)
class GateRecord:
    """Release-gate decision record for the JSON report."""

    verdict: str
    reason: str
    checks: dict[str, bool]
    timestamp: str
    thresholds_applied: dict[str, str | int | float | bool]


@dataclass(frozen=True, slots=True)
class PerItemRecord:
    """Per-item evaluation outcome for the JSON report."""

    item_id: str
    category: str
    expected_refusal: bool
    actual_refusal: bool
    item_precision: float
    item_recall: float


@dataclass(frozen=True, slots=True)
class ReportPayload:
    """Top-level JSON report produced by the CLI."""

    schema_version: str
    holdout_name: str
    holdout_version: str
    holdout_hash: str
    corpus_hash: str
    evidence_kind: str
    measured_at: str
    measurement: MeasurementRecord
    release_gate: GateRecord
    projections: list[ProjectionHealthRecord]
    per_item_summary: list[PerItemRecord]


# ---------------------------------------------------------------------------
# Conversion functions (evaluator types → report records)
# ---------------------------------------------------------------------------


def _latency_record(lat: LatencyPercentiles) -> LatencyRecord:
    """Convert a LatencyPercentiles to a LatencyRecord."""
    return LatencyRecord(
        p50_ms=lat.p50_ms,
        p95_ms=lat.p95_ms,
        p99_ms=lat.p99_ms,
        max_ms=lat.max_ms,
        sample_count=lat.sample_count,
    )


def measurement_record(
    measurement: EvaluationMeasurement,
    corpus_hash: str,
) -> MeasurementRecord:
    """Convert an EvaluationMeasurement to a MeasurementRecord."""
    idem = measurement.idempotency
    fresh = measurement.freshness
    live_lat = (
        _latency_record(measurement.live_query_latency)
        if measurement.live_query_latency is not None
        else None
    )
    return MeasurementRecord(
        evidence_coverage=measurement.evidence_coverage,
        refusal_precision=measurement.refusal_precision,
        refusal_recall=measurement.refusal_recall,
        retrieval_precision=measurement.retrieval_precision,
        retrieval_recall=measurement.retrieval_recall,
        retrieval_f1=measurement.retrieval_f1,
        hot_query_latency=_latency_record(measurement.hot_query_latency),
        idempotency=IdempotencyRecord(
            passed=idem.passed,
            checked_count=idem.checked_count,
            mismatched_count=idem.mismatched_count,
        ),
        freshness=FreshnessRecord(
            avg_age_days=fresh.avg_age_days,
            max_age_days=fresh.max_age_days,
            within_threshold_count=fresh.within_threshold_count,
            applicable_count=fresh.applicable_count,
        ),
        total_items=measurement.total_items,
        corpus_hash=corpus_hash,
        live_query_latency=live_lat,
    )


def gate_record(gate: ReleaseDecision) -> GateRecord:
    """Convert a ReleaseDecision to a GateRecord."""
    return GateRecord(
        verdict=gate.verdict,
        reason=gate.reason,
        checks=dict(gate.checks),
        timestamp=gate.timestamp,
        thresholds_applied=dict(gate.thresholds_applied),
    )


def projection_health_records(
    projections: list[ProjectionHealth],
) -> list[ProjectionHealthRecord]:
    """Convert ProjectionHealth values to ProjectionHealthRecords."""
    return [
        ProjectionHealthRecord(
            kind=p.kind,
            enabled=p.enabled,
            healthy=p.healthy,
            lag_items=p.lag_items,
            checked_at=p.checked_at,
        )
        for p in projections
    ]


def per_item_records(result: EvaluationRunResult) -> list[PerItemRecord]:
    """Convert per-item results from an EvaluationRunResult."""
    return [
        PerItemRecord(
            item_id=r.item_id,
            category=r.category,
            expected_refusal=r.expected_refusal,
            actual_refusal=r.actual_refusal,
            item_precision=r.item_precision,
            item_recall=r.item_recall,
        )
        for r in result.per_item_results
    ]
