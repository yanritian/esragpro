from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from app.memory_core.decision_contracts import (
    AbstainedDecision,
    ActionRisk,
    CandidateAction,
    DecisionContractError,
    DecisionFrame,
    DecisionRoute,
    RoutedDecision,
    SelectedActionDecision,
)
from app.memory_core.decision_handoff import (
    AgentDecisionHandoff,
    DecisionDisposition,
    DecisionHandoffError,
    build_agent_decision_handoff,
)


def _action(risk: ActionRisk = ActionRisk.LOW) -> CandidateAction:
    return CandidateAction("inspect", "inspect the evidence", risk)


def _frame(
    action: CandidateAction | None = None,
    *,
    evidence_ids: tuple[str, ...] | None = None,
) -> DecisionFrame:
    return DecisionFrame(
        frame_id="frame-1",
        query_id="query-1",
        goal="choose a bounded next step",
        as_of=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
        asserted_state=(),
        observations=(),
        uncertainties=(),
        legal_actions=(action if action is not None else _action(),),
        evidence_ids=("evidence-1",) if evidence_ids is None else evidence_ids,
    )


def _selected(model_id: str, action_id: str = "inspect") -> SelectedActionDecision:
    return SelectedActionDecision(model_id, action_id, 0.9, 0.9)


def _routed(
    route: DecisionRoute,
    result: SelectedActionDecision | AbstainedDecision,
    *,
    trace: tuple[str, ...] | None = None,
) -> RoutedDecision:
    model_trace = trace if trace is not None else ("fast-model", result.model_id)
    return RoutedDecision(route, result, model_trace)


@pytest.mark.parametrize(
    ("route", "risk"),
    [
        (DecisionRoute.FAST_MODEL, ActionRisk.LOW),
        (DecisionRoute.SLOW_MODEL, ActionRisk.LOW),
        (DecisionRoute.SLOW_MODEL, ActionRisk.MEDIUM),
    ],
)
def test_fast_low_risk_selection_builds_advisory_proposal(
    route: DecisionRoute,
    risk: ActionRisk,
) -> None:
    action = _action(risk)
    frame = _frame(action)
    result = _selected("slow-model" if route is DecisionRoute.SLOW_MODEL else "fast-model")
    routed = _routed(route, result)

    handoff = build_agent_decision_handoff(frame, routed)

    assert handoff.frame_id == frame.frame_id
    assert handoff.query_id == frame.query_id
    assert handoff.goal == frame.goal
    assert handoff.as_of == datetime(2026, 9, 29, 12, 0)
    assert handoff.route is route
    assert handoff.disposition is DecisionDisposition.ACTION_PROPOSED
    assert handoff.result is result
    assert handoff.proposed_action is action
    assert handoff.evidence_ids == frame.evidence_ids
    assert handoff.model_trace == routed.model_trace
    assert handoff.execution_authorized is False
    with pytest.raises(FrozenInstanceError):
        handoff.goal = "changed"


def test_selected_human_review_retains_unauthorized_action() -> None:
    action = _action(ActionRisk.HIGH)
    result = _selected("slow-model")

    handoff = build_agent_decision_handoff(_frame(action), _routed(DecisionRoute.HUMAN_REVIEW, result))

    assert handoff.disposition is DecisionDisposition.HUMAN_REVIEW_REQUIRED
    assert handoff.proposed_action is action
    assert handoff.execution_authorized is False


def test_abstention_builds_non_actionable_handoff() -> None:
    result = AbstainedDecision("slow-model", 0.2, "evidence is insufficient")

    handoff = build_agent_decision_handoff(
        _frame(),
        _routed(DecisionRoute.HUMAN_REVIEW, result),
    )

    assert handoff.disposition is DecisionDisposition.ABSTAINED
    assert handoff.proposed_action is None
    assert handoff.execution_authorized is False


def test_builder_fails_closed_for_membership_evidence_trace_and_route_invariants() -> None:
    result = _selected("slow-model")
    frame = _frame()
    cases = (
        (_frame(), _routed(DecisionRoute.FAST_MODEL, _selected("fast-model", "missing")), None),
        (_frame(evidence_ids=()), _routed(DecisionRoute.FAST_MODEL, _selected("fast-model")), DecisionHandoffError),
        (frame, _routed(DecisionRoute.FAST_MODEL, result, trace=()), DecisionHandoffError),
        (frame, _routed(DecisionRoute.FAST_MODEL, result, trace=("fast-model", "other")), DecisionHandoffError),
        (frame, _routed(DecisionRoute.FAST_MODEL, AbstainedDecision("fast-model", 0.2, "uncertain")), DecisionHandoffError),
        (_frame(_action(ActionRisk.MEDIUM)), _routed(DecisionRoute.FAST_MODEL, _selected("fast-model")), DecisionHandoffError),
        (_frame(_action(ActionRisk.HIGH)), _routed(DecisionRoute.SLOW_MODEL, _selected("slow-model")), DecisionHandoffError),
    )

    for case_frame, routed, expected in cases:
        error = expected or DecisionContractError
        with pytest.raises(error):
            _ = build_agent_decision_handoff(case_frame, routed)


def test_direct_construction_normalizes_and_rejects_malformed_mapping() -> None:
    action = _action()
    result = _selected("fast-model")
    handoff = AgentDecisionHandoff(
        frame_id="frame-1",
        query_id="query-1",
        goal="inspect",
        as_of=datetime(2026, 9, 29, 14, 0, tzinfo=timezone(timedelta(hours=2))),
        disposition=DecisionDisposition.ACTION_PROPOSED.value,
        route=DecisionRoute.FAST_MODEL.value,
        result=result,
        proposed_action=action,
        evidence_ids=["evidence-1"],
        model_trace=["fast-model"],
    )
    assert handoff.as_of == datetime(2026, 9, 29, 12, 0)
    assert handoff.evidence_ids == ("evidence-1",)
    assert handoff.model_trace == ("fast-model",)

    malformed = (
        dict(proposed_action=None),
        dict(proposed_action=CandidateAction("other", "other", ActionRisk.LOW)),
        dict(result=AbstainedDecision("fast-model", 0.2, "uncertain"), proposed_action=action),
        dict(disposition=DecisionDisposition.HUMAN_REVIEW_REQUIRED),
    )
    for changes in malformed:
        values = {
            "frame_id": "frame-1",
            "query_id": "query-1",
            "goal": "inspect",
            "as_of": handoff.as_of,
            "disposition": handoff.disposition,
            "route": handoff.route,
            "result": handoff.result,
            "proposed_action": handoff.proposed_action,
            "evidence_ids": handoff.evidence_ids,
            "model_trace": handoff.model_trace,
        }
        values.update(changes)
        with pytest.raises(DecisionHandoffError):
            _ = AgentDecisionHandoff(**values)


def test_handoff_is_deterministic_and_builder_signature_has_no_execution_dependency() -> None:
    frame = _frame()
    routed = _routed(DecisionRoute.FAST_MODEL, _selected("fast-model"))

    first = build_agent_decision_handoff(frame, routed)
    second = build_agent_decision_handoff(frame, routed)

    assert first == second
    assert list(inspect.signature(build_agent_decision_handoff).parameters) == ["frame", "routed"]
    with pytest.raises(TypeError):
        _ = AgentDecisionHandoff(
            frame_id=first.frame_id,
            query_id=first.query_id,
            goal=first.goal,
            as_of=first.as_of,
            disposition=first.disposition,
            route=first.route,
            result=first.result,
            proposed_action=first.proposed_action,
            evidence_ids=first.evidence_ids,
            model_trace=first.model_trace,
            execution_authorized=True,
        )
    with pytest.raises(FrozenInstanceError):
        first.execution_authorized = True
