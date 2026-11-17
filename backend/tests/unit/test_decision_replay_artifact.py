from __future__ import annotations

import json

import pytest

import app.memory_core.decision_replay_artifact as artifact_module
from app.memory_core.decision_contracts import DecisionRoute
from app.memory_core.decision_handoff import DecisionDisposition
from app.memory_core.decision_replay import (
    DecisionReplayError,
    DecisionReplayGateResult,
    DecisionReplayItemResult,
    DecisionReplayMetrics,
    DecisionReplayReport,
    DecisionReplayThresholds,
    DecisionReplayVerdict,
    DecisionRouteCount,
)
from app.memory_core.decision_replay_artifact import build_decision_replay_artifact


def _report() -> tuple[DecisionReplayReport, DecisionReplayThresholds]:
    items = (
        DecisionReplayItemResult(
            case_id="fast-action",
            expected_disposition=DecisionDisposition.ACTION_PROPOSED,
            actual_disposition=DecisionDisposition.ACTION_PROPOSED,
            expected_route=DecisionRoute.FAST_MODEL,
            actual_route=DecisionRoute.FAST_MODEL,
            expected_action_id="inspect",
            actual_action_id="inspect",
            disposition_matches=True,
            route_matches=True,
            action_matches=True,
            exact_match=True,
            evidence_preserved=True,
        ),
        DecisionReplayItemResult(
            case_id="slow-review",
            expected_disposition=DecisionDisposition.HUMAN_REVIEW_REQUIRED,
            actual_disposition=DecisionDisposition.ABSTAINED,
            expected_route=DecisionRoute.SLOW_MODEL,
            actual_route=DecisionRoute.HUMAN_REVIEW,
            expected_action_id="review",
            actual_action_id=None,
            disposition_matches=False,
            route_matches=False,
            action_matches=False,
            exact_match=False,
            evidence_preserved=False,
        ),
        DecisionReplayItemResult(
            case_id="human-action",
            expected_disposition=DecisionDisposition.ABSTAINED,
            actual_disposition=DecisionDisposition.ACTION_PROPOSED,
            expected_route=DecisionRoute.HUMAN_REVIEW,
            actual_route=DecisionRoute.FAST_MODEL,
            expected_action_id=None,
            actual_action_id="unexpected",
            disposition_matches=False,
            route_matches=False,
            action_matches=False,
            exact_match=False,
            evidence_preserved=False,
        ),
    )
    metrics = DecisionReplayMetrics(
        total_cases=3,
        exact_outcome_accuracy=1 / 3,
        disposition_accuracy=1 / 3,
        action_match_rate=1 / 3,
        route_accuracy=1 / 3,
        evidence_preservation_rate=1 / 3,
        unexpected_auto_proposal_count=1,
        route_counts=(
            DecisionRouteCount(DecisionRoute.FAST_MODEL, 2),
            DecisionRouteCount(DecisionRoute.SLOW_MODEL, 0),
            DecisionRouteCount(DecisionRoute.HUMAN_REVIEW, 1),
        ),
    )
    return (
        DecisionReplayReport(items=items, metrics=metrics),
        DecisionReplayThresholds(
            min_exact_outcome_accuracy=0.8,
            min_evidence_preservation_rate=0.8,
            max_unexpected_auto_proposals=0,
        ),
    )


def test_mixed_report_projects_every_field_in_stable_json_shape() -> None:
    report, thresholds = _report()

    artifact = build_decision_replay_artifact(report, thresholds)

    assert set(artifact) == {"schema_version", "report", "thresholds", "gate"}
    assert artifact["schema_version"] == "1"
    assert artifact["report"]["items"] == [
        {
            "case_id": "fast-action",
            "expected_disposition": "action_proposed",
            "actual_disposition": "action_proposed",
            "expected_route": "fast_model",
            "actual_route": "fast_model",
            "expected_action_id": "inspect",
            "actual_action_id": "inspect",
            "disposition_matches": True,
            "route_matches": True,
            "action_matches": True,
            "exact_match": True,
            "evidence_preserved": True,
        },
        {
            "case_id": "slow-review",
            "expected_disposition": "human_review_required",
            "actual_disposition": "abstained",
            "expected_route": "slow_model",
            "actual_route": "human_review",
            "expected_action_id": "review",
            "actual_action_id": None,
            "disposition_matches": False,
            "route_matches": False,
            "action_matches": False,
            "exact_match": False,
            "evidence_preserved": False,
        },
        {
            "case_id": "human-action",
            "expected_disposition": "abstained",
            "actual_disposition": "action_proposed",
            "expected_route": "human_review",
            "actual_route": "fast_model",
            "expected_action_id": None,
            "actual_action_id": "unexpected",
            "disposition_matches": False,
            "route_matches": False,
            "action_matches": False,
            "exact_match": False,
            "evidence_preserved": False,
        },
    ]
    assert artifact["report"]["metrics"] == {
        "total_cases": 3,
        "exact_outcome_accuracy": 1 / 3,
        "disposition_accuracy": 1 / 3,
        "action_match_rate": 1 / 3,
        "route_accuracy": 1 / 3,
        "evidence_preservation_rate": 1 / 3,
        "unexpected_auto_proposal_count": 1,
        "route_counts": [
            {"route": "fast_model", "count": 2},
            {"route": "slow_model", "count": 0},
            {"route": "human_review", "count": 1},
        ],
    }
    assert artifact["thresholds"] == {
        "min_exact_outcome_accuracy": 0.8,
        "min_evidence_preservation_rate": 0.8,
        "max_unexpected_auto_proposals": 0,
    }
    assert artifact["gate"] == {
        "verdict": "fail",
        "failed_checks": [
            "exact_outcome_accuracy",
            "evidence_preservation_rate",
            "unexpected_auto_proposals",
        ],
    }
    assert json.dumps(artifact)


def test_projection_detaches_nested_collections_and_equal_inputs_match() -> None:
    report, thresholds = _report()
    artifact = build_decision_replay_artifact(report, thresholds)

    artifact["report"]["items"].append(artifact["report"]["items"][0])
    artifact["report"]["items"][0]["case_id"] = "changed"
    artifact["report"]["metrics"]["route_counts"][0]["count"] = 99
    artifact["gate"]["failed_checks"].append("changed")
    artifact["thresholds"]["max_unexpected_auto_proposals"] = 99

    assert len(report.items) == 3
    assert report.items[0].case_id == "fast-action"
    assert report.metrics.route_counts[0].count == 2
    assert thresholds.max_unexpected_auto_proposals == 0
    assert build_decision_replay_artifact(*_report()) == build_decision_replay_artifact(*_report())


def test_gate_is_evaluated_once_and_errors_propagate_without_partial_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, thresholds = _report()
    calls: list[tuple[DecisionReplayReport, DecisionReplayThresholds]] = []

    def evaluate_once(
        given_report: DecisionReplayReport,
        given_thresholds: DecisionReplayThresholds,
    ) -> DecisionReplayGateResult:
        calls.append((given_report, given_thresholds))
        return DecisionReplayGateResult(DecisionReplayVerdict.PASS, ("ordered", "checks"))

    monkeypatch.setattr(artifact_module, "evaluate_decision_replay", evaluate_once)
    artifact = build_decision_replay_artifact(report, thresholds)

    assert calls == [(report, thresholds)]
    assert artifact["gate"] == {"verdict": "pass", "failed_checks": ["ordered", "checks"]}

    error = DecisionReplayError(field="gate", detail="preserve this error")

    def raise_error(
        given_report: DecisionReplayReport,
        given_thresholds: DecisionReplayThresholds,
    ) -> None:
        del given_report, given_thresholds
        raise error

    monkeypatch.setattr(artifact_module, "evaluate_decision_replay", raise_error)
    with pytest.raises(DecisionReplayError) as raised:
        _ = build_decision_replay_artifact(report, thresholds)
    assert raised.value is error
