from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import Callable, TypeAlias
from uuid import uuid4

import pytest

from app.memory_core.decision_contracts import (
    AbstainedDecision,
    ActionRisk,
    CandidateAction,
    DecisionResult,
    DecisionRoute,
    RoutedDecision,
    SelectedActionDecision,
)
from app.memory_core.decision_frame import DecisionFrameRequest, compile_decision_frame
from app.memory_core.decision_handoff import (
    AgentDecisionHandoff,
    DecisionDisposition,
)
from app.memory_core.decision_kernel import DecisionKernelOutcome
from app.memory_core.world_memory import (
    WorldContextItem,
    WorldContextItemKind,
    WorldContextPack,
)
from app.memory_core.decision_replay import (
    DecisionReplayCase,
    DecisionReplayError,
    DecisionReplayGateResult,
    DecisionReplayMetrics,
    DecisionReplayReport,
    DecisionRouteCount,
    DecisionReplayThresholds,
    DecisionReplayVerdict,
    evaluate_decision_replay,
    run_decision_replay,
)


def _action(action_id: str = "inspect", risk: ActionRisk = ActionRisk.LOW) -> CandidateAction:
    return CandidateAction(action_id, f"take {action_id}", risk)


def _request(*actions: CandidateAction) -> DecisionFrameRequest:
    evidence_id = uuid4()
    return DecisionFrameRequest(
        goal="choose a bounded next step",
        as_of=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
        context_pack=WorldContextPack(
            query_id=uuid4(),
            items=(WorldContextItem(WorldContextItemKind.OBSERVATION, uuid4(), "evidence", (evidence_id,)),),
        ),
        legal_actions=actions or (_action(),),
        max_context_items=1,
    )


def _outcome(
    request: DecisionFrameRequest,
    route: DecisionRoute,
    disposition: DecisionDisposition,
    action_id: str | None,
    *,
    evidence_ids: tuple[str, ...] | None = None,
) -> DecisionKernelOutcome:
    frame = compile_decision_frame(request)
    model_id = route.value
    result: DecisionResult = (
        AbstainedDecision(model_id, 0.2, "insufficient evidence")
        if action_id is None
        else SelectedActionDecision(model_id, action_id, 0.9, 0.9)
    )
    routed = RoutedDecision(route, result, (model_id,))
    handoff = AgentDecisionHandoff(
        frame_id=frame.frame_id,
        query_id=frame.query_id,
        goal=frame.goal,
        as_of=frame.as_of,
        disposition=disposition,
        route=route,
        result=result,
        proposed_action=next(
            (action for action in frame.legal_actions if action.action_id == action_id),
            None,
        ),
        evidence_ids=frame.evidence_ids if evidence_ids is None else evidence_ids,
        model_trace=routed.model_trace,
    )
    return DecisionKernelOutcome(frame, routed, handoff)


class ScriptedSystem:
    def __init__(self, outcomes: tuple[DecisionKernelOutcome, ...]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def decide(self, request: DecisionFrameRequest) -> DecisionKernelOutcome:
        self.calls.append(request.goal)
        return self.outcomes[len(self.calls) - 1]


def _case(
    case_id: str,
    request: DecisionFrameRequest,
    disposition: DecisionDisposition,
    route: DecisionRoute,
    action_id: str | None,
) -> DecisionReplayCase:
    return DecisionReplayCase(case_id, request, disposition, route, action_id)


def test_mixed_suite_runs_once_in_order_and_preserves_immutable_records() -> None:
    requests = tuple(_request(_action(name, risk)) for name, risk in (
        ("fast", ActionRisk.LOW), ("slow", ActionRisk.MEDIUM),
        ("review", ActionRisk.HIGH), ("abstain", ActionRisk.LOW),
    ))
    outcomes = (
        _outcome(requests[0], DecisionRoute.FAST_MODEL, DecisionDisposition.ACTION_PROPOSED, "fast"),
        _outcome(requests[1], DecisionRoute.SLOW_MODEL, DecisionDisposition.ACTION_PROPOSED, "slow"),
        _outcome(requests[2], DecisionRoute.HUMAN_REVIEW, DecisionDisposition.HUMAN_REVIEW_REQUIRED, "review"),
        _outcome(requests[3], DecisionRoute.HUMAN_REVIEW, DecisionDisposition.ABSTAINED, None),
    )
    cases = tuple(_case(str(index), request, outcome.handoff.disposition, outcome.handoff.route,
                        outcome.handoff.proposed_action.action_id if outcome.handoff.proposed_action else None)
                  for index, (request, outcome) in enumerate(zip(requests, outcomes, strict=True)))
    system = ScriptedSystem(outcomes)

    report = run_decision_replay(system, cases)

    assert system.calls == [request.goal for request in requests]
    assert tuple(item.case_id for item in report.items) == ("0", "1", "2", "3")
    assert report.metrics.route_counts == (
        DecisionRouteCount(DecisionRoute.FAST_MODEL, 1), DecisionRouteCount(DecisionRoute.SLOW_MODEL, 1),
        DecisionRouteCount(DecisionRoute.HUMAN_REVIEW, 2),
    )
    assert all(item.exact_match and item.evidence_preserved for item in report.items)
    with pytest.raises(FrozenInstanceError):
        report.items = ()
    with pytest.raises(TypeError):
        report.items[0] = report.items[0]


def test_metrics_count_each_match_and_cross_object_evidence_failure() -> None:
    requests = (
        _request(_action("one")),
        _request(_action("two"), _action("wrong")),
        _request(_action("three")),
        _request(_action("four")),
    )
    outcomes = (
        _outcome(requests[0], DecisionRoute.FAST_MODEL, DecisionDisposition.ACTION_PROPOSED, "one"),
        _outcome(requests[1], DecisionRoute.SLOW_MODEL, DecisionDisposition.ACTION_PROPOSED, "two"),
        _outcome(requests[2], DecisionRoute.HUMAN_REVIEW, DecisionDisposition.HUMAN_REVIEW_REQUIRED, "three",
                 evidence_ids=("different",)),
        _outcome(requests[3], DecisionRoute.HUMAN_REVIEW, DecisionDisposition.ABSTAINED, None),
    )
    cases = (
        _case("one", requests[0], DecisionDisposition.ACTION_PROPOSED, DecisionRoute.FAST_MODEL, "one"),
        _case("two", requests[1], DecisionDisposition.ACTION_PROPOSED, DecisionRoute.FAST_MODEL, "wrong"),
        _case("three", requests[2], DecisionDisposition.ACTION_PROPOSED, DecisionRoute.HUMAN_REVIEW, "three"),
        _case("four", requests[3], DecisionDisposition.ABSTAINED, DecisionRoute.HUMAN_REVIEW, None),
    )

    metrics = run_decision_replay(ScriptedSystem(outcomes), cases).metrics

    assert (metrics.total_cases, metrics.exact_outcome_accuracy) == (4, 0.5)
    assert metrics.disposition_accuracy == 0.75
    assert metrics.action_match_rate == 0.75
    assert metrics.route_accuracy == 0.75
    assert metrics.evidence_preservation_rate == 0.75
    assert metrics.unexpected_auto_proposal_count == 0


def test_gate_passes_perfect_nonempty_suite_and_fails_unexpected_proposal() -> None:
    request = _request()
    outcome = _outcome(request, DecisionRoute.FAST_MODEL, DecisionDisposition.ACTION_PROPOSED, "inspect")
    perfect = run_decision_replay(
        ScriptedSystem((outcome,)),
        (_case("perfect", request, DecisionDisposition.ACTION_PROPOSED, DecisionRoute.FAST_MODEL, "inspect"),),
    )
    assert evaluate_decision_replay(perfect, DecisionReplayThresholds()) == DecisionReplayGateResult(
        DecisionReplayVerdict.PASS, ()
    )

    unexpected = run_decision_replay(
        ScriptedSystem((outcome,)),
        (_case("unexpected", request, DecisionDisposition.HUMAN_REVIEW_REQUIRED,
                DecisionRoute.FAST_MODEL, "inspect"),),
    )
    verdict = evaluate_decision_replay(unexpected, DecisionReplayThresholds())
    assert verdict.verdict is DecisionReplayVerdict.FAIL
    assert verdict.failed_checks == ("exact_outcome_accuracy", "unexpected_auto_proposals")


ReplayFactory: TypeAlias = Callable[
    [], DecisionReplayCase | DecisionReplayThresholds | DecisionReplayMetrics
]


@pytest.mark.parametrize(
    "factory",
    (
        lambda: DecisionReplayCase(" ", _request(), DecisionDisposition.ABSTAINED, DecisionRoute.HUMAN_REVIEW, None),
        lambda: DecisionReplayCase("bad", _request(), DecisionDisposition.ABSTAINED, "unknown", None),
        lambda: DecisionReplayCase("bad", _request(), "unknown", DecisionRoute.HUMAN_REVIEW, None),
        lambda: DecisionReplayCase("bad", _request(), DecisionDisposition.ABSTAINED, DecisionRoute.HUMAN_REVIEW, "inspect"),
        lambda: DecisionReplayCase("bad", _request(), DecisionDisposition.ACTION_PROPOSED, DecisionRoute.FAST_MODEL, None),
        lambda: DecisionReplayCase("bad", _request(_action("other")), DecisionDisposition.ACTION_PROPOSED, DecisionRoute.FAST_MODEL, "inspect"),
        lambda: DecisionReplayThresholds(min_exact_outcome_accuracy=float("nan")),
        lambda: DecisionReplayThresholds(max_unexpected_auto_proposals=-1),
        lambda: DecisionReplayMetrics(1, 1.0, 1.0, 1.0, 1.0, 1.0, 0, ()),
    ),
)


def test_contract_violations_use_typed_replay_error(factory: ReplayFactory) -> None:
    with pytest.raises(DecisionReplayError) as raised:
        factory()
    assert raised.value.field and raised.value.detail


def test_suite_rejects_empty_and_duplicate_ids_and_system_errors_propagate() -> None:
    with pytest.raises(DecisionReplayError):
        run_decision_replay(ScriptedSystem(()), ())
    request = _request()
    outcome = _outcome(request, DecisionRoute.FAST_MODEL, DecisionDisposition.ACTION_PROPOSED, "inspect")
    case = _case("duplicate", request, DecisionDisposition.ACTION_PROPOSED, DecisionRoute.FAST_MODEL, "inspect")
    with pytest.raises(DecisionReplayError):
        run_decision_replay(ScriptedSystem((outcome, outcome)), (case, case))

    error = RuntimeError("preserve me")

    class RaisingSystem:
        def decide(self, request: DecisionFrameRequest) -> DecisionKernelOutcome:
            del request
            raise error

    with pytest.raises(RuntimeError) as raised:
        run_decision_replay(RaisingSystem(), (case,))
    assert raised.value is error


def test_direct_zero_report_fails_closed_and_api_is_side_effect_free() -> None:
    empty = DecisionReplayReport((), DecisionReplayMetrics(0, 1.0, 1.0, 1.0, 1.0, 1.0, 0,
                                                            (DecisionRouteCount(DecisionRoute.FAST_MODEL, 0),
                                                             DecisionRouteCount(DecisionRoute.SLOW_MODEL, 0),
                                                             DecisionRouteCount(DecisionRoute.HUMAN_REVIEW, 0))))
    verdict = evaluate_decision_replay(empty, DecisionReplayThresholds())
    assert verdict == DecisionReplayGateResult(DecisionReplayVerdict.FAIL, ("nonempty",))
    assert list(inspect.signature(run_decision_replay).parameters) == ["system", "cases"]
    assert list(inspect.signature(evaluate_decision_replay).parameters) == ["report", "thresholds"]
