from __future__ import annotations

from typing import Literal, TypedDict

from app.memory_core.decision_replay import (
    DecisionReplayMetrics,
    DecisionReplayReport,
    DecisionReplayThresholds,
    evaluate_decision_replay,
)


class DecisionReplayArtifactItemRecord(TypedDict):
    case_id: str
    expected_disposition: str
    actual_disposition: str
    expected_route: str
    actual_route: str
    expected_action_id: str | None
    actual_action_id: str | None
    disposition_matches: bool
    route_matches: bool
    action_matches: bool
    exact_match: bool
    evidence_preserved: bool


class DecisionReplayArtifactRouteCountRecord(TypedDict):
    route: str
    count: int


class DecisionReplayArtifactMetricsRecord(TypedDict):
    total_cases: int
    exact_outcome_accuracy: float
    disposition_accuracy: float
    action_match_rate: float
    route_accuracy: float
    evidence_preservation_rate: float
    unexpected_auto_proposal_count: int
    route_counts: list[DecisionReplayArtifactRouteCountRecord]


class DecisionReplayArtifactReportRecord(TypedDict):
    items: list[DecisionReplayArtifactItemRecord]
    metrics: DecisionReplayArtifactMetricsRecord


class DecisionReplayArtifactThresholdsRecord(TypedDict):
    min_exact_outcome_accuracy: float
    min_evidence_preservation_rate: float
    max_unexpected_auto_proposals: int


class DecisionReplayArtifactGateRecord(TypedDict):
    verdict: str
    failed_checks: list[str]


class DecisionReplayArtifactRecord(TypedDict):
    schema_version: Literal["1"]
    report: DecisionReplayArtifactReportRecord
    thresholds: DecisionReplayArtifactThresholdsRecord
    gate: DecisionReplayArtifactGateRecord


def build_decision_replay_artifact(
    report: DecisionReplayReport,
    thresholds: DecisionReplayThresholds,
) -> DecisionReplayArtifactRecord:
    gate = evaluate_decision_replay(report, thresholds)
    metrics: DecisionReplayMetrics = report.metrics
    return {
        "schema_version": "1",
        "report": {
            "items": [
                {
                    "case_id": item.case_id,
                    "expected_disposition": item.expected_disposition.value,
                    "actual_disposition": item.actual_disposition.value,
                    "expected_route": item.expected_route.value,
                    "actual_route": item.actual_route.value,
                    "expected_action_id": item.expected_action_id,
                    "actual_action_id": item.actual_action_id,
                    "disposition_matches": item.disposition_matches,
                    "route_matches": item.route_matches,
                    "action_matches": item.action_matches,
                    "exact_match": item.exact_match,
                    "evidence_preserved": item.evidence_preserved,
                }
                for item in report.items
            ],
            "metrics": {
                "total_cases": metrics.total_cases,
                "exact_outcome_accuracy": metrics.exact_outcome_accuracy,
                "disposition_accuracy": metrics.disposition_accuracy,
                "action_match_rate": metrics.action_match_rate,
                "route_accuracy": metrics.route_accuracy,
                "evidence_preservation_rate": metrics.evidence_preservation_rate,
                "unexpected_auto_proposal_count": metrics.unexpected_auto_proposal_count,
                "route_counts": [
                    {"route": route_count.route.value, "count": route_count.count}
                    for route_count in metrics.route_counts
                ],
            },
        },
        "thresholds": {
            "min_exact_outcome_accuracy": thresholds.min_exact_outcome_accuracy,
            "min_evidence_preservation_rate": thresholds.min_evidence_preservation_rate,
            "max_unexpected_auto_proposals": thresholds.max_unexpected_auto_proposals,
        },
        "gate": {
            "verdict": gate.verdict.value,
            "failed_checks": list(gate.failed_checks),
        },
    }


__all__ = [
    "DecisionReplayArtifactGateRecord",
    "DecisionReplayArtifactItemRecord",
    "DecisionReplayArtifactMetricsRecord",
    "DecisionReplayArtifactRecord",
    "DecisionReplayArtifactReportRecord",
    "DecisionReplayArtifactRouteCountRecord",
    "DecisionReplayArtifactThresholdsRecord",
    "build_decision_replay_artifact",
]
