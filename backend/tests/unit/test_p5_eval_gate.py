"""Tests for release-gate decision logic (P5)."""

from __future__ import annotations

from app.memory_core.eval_gate import (
    ProjectionHealth,
    ReleaseThresholds,
    evaluate_release_gate,
)
from app.memory_core.eval_metrics import (
    EvaluationMeasurement,
    FreshnessStats,
    IdempotencyResult,
    LatencyPercentiles,
)


def _measurement(
    *,
    evidence_coverage: float = 1.0,
    refusal_precision: float = 1.0,
    refusal_recall: float = 1.0,
    retrieval_precision: float = 1.0,
    retrieval_recall: float = 1.0,
    retrieval_f1: float = 1.0,
    p99_ms: float = 5.0,
    idempotency_passed: bool = True,
    total_items: int = 8,
    latency_sample_count: int = 10,
    idempotency_checked: int = 10,
) -> EvaluationMeasurement:
    return EvaluationMeasurement(
        evidence_coverage=evidence_coverage,
        refusal_precision=refusal_precision,
        refusal_recall=refusal_recall,
        retrieval_precision=retrieval_precision,
        retrieval_recall=retrieval_recall,
        retrieval_f1=retrieval_f1,
        hot_query_latency=LatencyPercentiles(
            p50_ms=1.0, p95_ms=3.0, p99_ms=p99_ms, max_ms=p99_ms,
            sample_count=latency_sample_count,
        ),
        idempotency=IdempotencyResult(
            passed=idempotency_passed, checked_count=idempotency_checked,
            mismatched_count=0,
        ),
        freshness=FreshnessStats(
            avg_age_days=5.0, max_age_days=10.0,
            within_threshold_count=5, applicable_count=5,
        ),
        total_items=total_items,
    )


class TestReleaseGate:
    def test_all_pass(self) -> None:
        result = evaluate_release_gate(_measurement(), ReleaseThresholds())
        assert result.verdict == "pass"
        assert all(result.checks.values())

    def test_evidence_coverage_fail(self) -> None:
        measurement = _measurement(evidence_coverage=0.5)
        result = evaluate_release_gate(measurement, ReleaseThresholds())
        assert result.verdict == "fail"
        assert result.checks["evidence_coverage"] is False

    def test_refusal_precision_fail(self) -> None:
        measurement = _measurement(refusal_precision=0.7)
        result = evaluate_release_gate(measurement, ReleaseThresholds())
        assert result.verdict == "fail"
        assert result.checks["refusal_precision"] is False

    def test_refusal_recall_fail(self) -> None:
        measurement = _measurement(refusal_recall=0.5)
        result = evaluate_release_gate(measurement, ReleaseThresholds())
        assert result.verdict == "fail"
        assert result.checks["refusal_recall"] is False

    def test_latency_fail(self) -> None:
        measurement = _measurement(p99_ms=200.0)
        result = evaluate_release_gate(measurement, ReleaseThresholds())
        assert result.verdict == "fail"
        assert result.checks["hot_query_p99"] is False

    def test_idempotency_fail(self) -> None:
        measurement = _measurement(idempotency_passed=False)
        result = evaluate_release_gate(measurement, ReleaseThresholds())
        assert result.verdict == "fail"
        assert result.checks["idempotency"] is False

    def test_boundary_values_pass(self) -> None:
        """Exact boundary values should pass (>= and <=)."""
        measurement = _measurement(
            evidence_coverage=1.0,
            refusal_precision=0.85,
            refusal_recall=0.85,
            p99_ms=100.0,
        )
        result = evaluate_release_gate(measurement, ReleaseThresholds())
        assert result.verdict == "pass"

    def test_just_below_boundary_fails(self) -> None:
        measurement = _measurement(refusal_precision=0.849)
        result = evaluate_release_gate(measurement, ReleaseThresholds())
        assert result.verdict == "fail"


class TestProjectionHealth:
    def test_enabled_unhealthy_fails(self) -> None:
        projections = [
            ProjectionHealth(
                kind="postgres", enabled=True, healthy=True
            ),
            ProjectionHealth(
                kind="elasticsearch", enabled=True, healthy=False, lag_items=5
            ),
        ]
        result = evaluate_release_gate(
            _measurement(), ReleaseThresholds(),
            projections=projections,
        )
        assert result.verdict == "fail"
        assert result.checks["projection_health"] is False

    def test_disabled_unhealthy_ok(self) -> None:
        """Disabled projections are not checked."""
        projections = [
            ProjectionHealth(
                kind="postgres", enabled=True, healthy=True
            ),
            ProjectionHealth(
                kind="neo4j", enabled=False, healthy=False
            ),
        ]
        result = evaluate_release_gate(
            _measurement(), ReleaseThresholds(),
            projections=projections,
        )
        assert result.verdict == "pass"

    def test_all_healthy_passes(self) -> None:
        projections = [
            ProjectionHealth(kind="postgres", enabled=True, healthy=True),
            ProjectionHealth(
                kind="elasticsearch", enabled=True, healthy=True
            ),
        ]
        result = evaluate_release_gate(
            _measurement(), ReleaseThresholds(),
            projections=projections,
        )
        assert result.verdict == "pass"

    def test_no_projections_passes(self) -> None:
        result = evaluate_release_gate(
            _measurement(), ReleaseThresholds(),
        )
        assert result.verdict == "pass"
        assert "projection_health" not in result.checks

    def test_unmeasured_required_projection_fails(self) -> None:
        result = evaluate_release_gate(
            _measurement(), ReleaseThresholds(),
            projections=[],
            required_projections=["postgres"],
        )
        assert result.verdict == "fail"
        assert result.checks["projections_measured"] is False


class TestFailClosed:
    def test_zero_latency_samples_fail(self) -> None:
        m = _measurement(latency_sample_count=0)
        result = evaluate_release_gate(m, ReleaseThresholds())
        assert result.verdict == "fail"
        assert result.checks["latency_measured"] is False

    def test_zero_idempotency_checks_fail(self) -> None:
        m = _measurement(idempotency_checked=0)
        result = evaluate_release_gate(m, ReleaseThresholds())
        assert result.verdict == "fail"
        assert result.checks["idempotency_measured"] is False

    def test_empty_holdout_fails(self) -> None:
        m = _measurement(total_items=0)
        result = evaluate_release_gate(m, ReleaseThresholds())
        assert result.verdict == "fail"
        assert result.checks["nonempty_holdout"] is False


class TestReleaseDecisionStructure:
    def test_decision_has_timestamp(self) -> None:
        result = evaluate_release_gate(_measurement(), ReleaseThresholds())
        assert result.timestamp
        assert "T" in result.timestamp

    def test_thresholds_recorded(self) -> None:
        result = evaluate_release_gate(_measurement(), ReleaseThresholds())
        assert "min_evidence_coverage" in result.thresholds_applied
        assert result.thresholds_applied["min_evidence_coverage"] == 1.0

    def test_fail_reason_includes_details(self) -> None:
        measurement = _measurement(evidence_coverage=0.5)
        result = evaluate_release_gate(measurement, ReleaseThresholds())
        assert "evidence_coverage" in result.reason
