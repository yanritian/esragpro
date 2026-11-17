"""Fail-closed release-gate decision logic (P5).

The gate translates measured metrics into a pass / fail verdict.
Missing required measurements always result in failure — the gate
never reports passing when a metric was not measured.

Only *enabled* projections are checked.  A future projection (e.g.
Neo4j) can be represented as disabled without pretending it exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.memory_core.eval_metrics import EvaluationMeasurement


@dataclass(frozen=True, slots=True)
class ProjectionHealth:
    """Health status of a single projection backend."""

    kind: str
    enabled: bool
    healthy: bool
    lag_items: int = 0
    checked_at: str = ""


@dataclass(frozen=True, slots=True)
class ReleaseThresholds:
    """Numeric thresholds for the release-gate decision."""

    min_evidence_coverage: float = 1.0
    min_refusal_precision: float = 0.85
    min_refusal_recall: float = 0.85
    max_hot_query_p99_ms: float = 100.0
    require_idempotency: bool = True


@dataclass(frozen=True, slots=True)
class ReleaseDecision:
    """Outcome of the release-gate evaluation."""

    verdict: str  # "pass" | "fail"
    reason: str
    checks: dict[str, bool]
    timestamp: str
    thresholds_applied: dict[str, str | int | float | bool]


def evaluate_release_gate(
    measurement: EvaluationMeasurement,
    thresholds: ReleaseThresholds,
    *,
    projections: list[ProjectionHealth] | None = None,
    required_projections: list[str] | None = None,
) -> ReleaseDecision:
    """Derive a pass / fail verdict from *measurement* and *thresholds*.

    The gate is fail-closed: any missing required measurement or any
    threshold violation produces a ``fail`` verdict.

    ``required_projections`` is the list of projection kinds the caller
    expected to be measured.  If a required projection has no health
    entry in *projections*, the gate fails.
    """
    checks: dict[str, bool] = {}
    reasons: list[str] = []

    # 0. Non-empty holdout.
    nonempty = measurement.total_items > 0
    checks["nonempty_holdout"] = nonempty
    if not nonempty:
        reasons.append("holdout contained zero items")

    # 1. Evidence coverage (always required).
    ec_pass = measurement.evidence_coverage >= thresholds.min_evidence_coverage
    checks["evidence_coverage"] = ec_pass
    if not ec_pass:
        reasons.append(
            f"evidence_coverage={measurement.evidence_coverage:.4f} "
            f"< {thresholds.min_evidence_coverage}"
        )

    # 2. Refusal precision.
    rp_pass = (
        measurement.refusal_precision >= thresholds.min_refusal_precision
    )
    checks["refusal_precision"] = rp_pass
    if not rp_pass:
        reasons.append(
            f"refusal_precision={measurement.refusal_precision:.4f} "
            f"< {thresholds.min_refusal_precision}"
        )

    # 3. Refusal recall.
    rr_pass = measurement.refusal_recall >= thresholds.min_refusal_recall
    checks["refusal_recall"] = rr_pass
    if not rr_pass:
        reasons.append(
            f"refusal_recall={measurement.refusal_recall:.4f} "
            f"< {thresholds.min_refusal_recall}"
        )

    # 4. Hot-query P99 latency — must have at least one sample.
    has_latency = measurement.hot_query_latency.sample_count > 0
    checks["latency_measured"] = has_latency
    if not has_latency:
        reasons.append("zero latency samples recorded")
    else:
        lp_pass = (
            measurement.hot_query_latency.p99_ms
            <= thresholds.max_hot_query_p99_ms
        )
        checks["hot_query_p99"] = lp_pass
        if not lp_pass:
            reasons.append(
                f"hot_query_p99={measurement.hot_query_latency.p99_ms:.2f}ms "
                f"> {thresholds.max_hot_query_p99_ms:.1f}ms"
            )

    # 5. Idempotency — must have at least one check when required.
    if thresholds.require_idempotency:
        has_idem = measurement.idempotency.checked_count > 0
        checks["idempotency_measured"] = has_idem
        if not has_idem:
            reasons.append("zero idempotency checks recorded")
        else:
            id_pass = measurement.idempotency.passed
            checks["idempotency"] = id_pass
            if not id_pass:
                reasons.append(
                    f"idempotency failed: "
                    f"{measurement.idempotency.mismatched_count}/"
                    f"{measurement.idempotency.checked_count} mismatched"
                )

    # 6. Projection health (honest accounting).
    if projections is not None or required_projections:
        actual = projections or []
        required = required_projections or []
        by_kind = {p.kind: p for p in actual}
        missing_req = [k for k in required if k not in by_kind]
        checks["projections_measured"] = len(missing_req) == 0
        if missing_req:
            reasons.append(f"unmeasured required projections: {missing_req}")
        enabled = [p for p in actual if p.enabled]
        if enabled:
            all_healthy = all(p.healthy for p in enabled)
            checks["projection_health"] = all_healthy
            if not all_healthy:
                unhealthy = [p.kind for p in enabled if not p.healthy]
                reasons.append(f"unhealthy projections: {unhealthy}")
        elif required:
            checks["projection_health"] = False
            reasons.append("no enabled projections measured")

    verdict = "pass" if all(checks.values()) else "fail"
    reason = "All gates passed" if verdict == "pass" else "; ".join(reasons)

    thresholds_applied: dict[str, str | int | float | bool] = {
        "min_evidence_coverage": thresholds.min_evidence_coverage,
        "min_refusal_precision": thresholds.min_refusal_precision,
        "min_refusal_recall": thresholds.min_refusal_recall,
        "max_hot_query_p99_ms": thresholds.max_hot_query_p99_ms,
        "require_idempotency": thresholds.require_idempotency,
    }

    return ReleaseDecision(
        verdict=verdict,
        reason=reason,
        checks=checks,
        timestamp=datetime.now(timezone.utc).isoformat(),
        thresholds_applied=thresholds_applied,
    )
