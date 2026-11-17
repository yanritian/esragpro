from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.memory_core import decision_kernel
from app.memory_core.decision_contracts import (
    AbstainedDecision,
    ActionRisk,
    CandidateAction,
    DecisionFrame,
    DecisionFrameBuildError,
    DecisionResult,
    DecisionRoute,
    RoutedDecision,
    SelectedActionDecision,
    UnknownActionError,
)
from app.memory_core.decision_frame import DecisionFrameRequest, compile_decision_frame
from app.memory_core.decision_handoff import (
    AgentDecisionHandoff,
    DecisionDisposition,
    DecisionHandoffError,
)
from app.memory_core.decision_router import DecisionRoutePolicy
from app.memory_core.world_memory import (
    WorldContextItem,
    WorldContextItemKind,
    WorldContextPack,
)


class FakeModel:
    __slots__ = ("calls", "result")

    def __init__(self, result: DecisionResult) -> None:
        self.calls = 0
        self.result = result

    def decide(self, frame: DecisionFrame) -> DecisionResult:
        del frame
        self.calls += 1
        return self.result


def _action(risk: ActionRisk = ActionRisk.LOW) -> CandidateAction:
    return CandidateAction("inspect", "inspect the available evidence", risk)


def _policy() -> DecisionRoutePolicy:
    return DecisionRoutePolicy(fast_confidence_threshold=0.8, slow_confidence_threshold=0.7)


def _request(
    action: CandidateAction | None = None,
    *,
    legal_actions: tuple[CandidateAction, ...] | None = None,
) -> DecisionFrameRequest:
    evidence_id = uuid4()
    context = WorldContextPack(
        query_id=uuid4(),
        items=(
            WorldContextItem(
                kind=WorldContextItemKind.OBSERVATION,
                id=uuid4(),
                content="the context is evidence-backed",
                evidence_ids=(evidence_id,),
            ),
        ),
    )
    actions = legal_actions if legal_actions is not None else (action or _action(),)
    return DecisionFrameRequest(
        goal="choose a bounded next step",
        as_of=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
        context_pack=context,
        legal_actions=actions,
        max_context_items=1,
    )


def _kernel(
    fast_result: DecisionResult,
    slow_result: DecisionResult,
    *,
    action: CandidateAction | None = None,
) -> tuple[decision_kernel.DecisionKernel, FakeModel, FakeModel, DecisionFrameRequest]:
    fast = FakeModel(fast_result)
    slow = FakeModel(slow_result)
    kernel = decision_kernel.DecisionKernel(
        fast,
        slow,
        _policy(),
    )
    return kernel, fast, slow, _request(action)


def test_fast_selection_returns_complete_outcome_without_slow_call() -> None:
    action = _action()
    kernel, fast, slow, request = _kernel(
        SelectedActionDecision("fast", action.action_id, 0.95, 0.95),
        AbstainedDecision("slow", 0.1, "unused"),
        action=action,
    )

    outcome = kernel.decide(request)

    assert outcome.frame.query_id == request.context_pack.query_id
    assert (outcome.routed.route, outcome.routed.model_trace) == (DecisionRoute.FAST_MODEL, ("fast",))
    assert outcome.handoff.disposition is DecisionDisposition.ACTION_PROPOSED
    assert outcome.handoff.proposed_action is action
    assert (fast.calls, slow.calls) == (1, 0)


def test_slow_escalation_preserves_trace() -> None:
    action = _action()
    kernel, fast, slow, request = _kernel(
        SelectedActionDecision("fast", action.action_id, 0.4, 0.4),
        SelectedActionDecision("slow", action.action_id, 0.9, 0.9),
        action=action,
    )

    outcome = kernel.decide(request)

    assert (outcome.routed.route, outcome.routed.model_trace) == (
        DecisionRoute.SLOW_MODEL,
        ("fast", "slow"),
    )
    assert outcome.handoff.model_trace == outcome.routed.model_trace
    assert (fast.calls, slow.calls) == (1, 1)


def test_high_risk_selection_returns_unauthorized_human_handoff() -> None:
    action = _action(ActionRisk.HIGH)
    kernel, fast, slow, request = _kernel(
        SelectedActionDecision("fast", action.action_id, 0.99, 0.99),
        AbstainedDecision("slow", 0.1, "unused"),
        action=action,
    )

    outcome = kernel.decide(request)

    assert outcome.routed.route is DecisionRoute.HUMAN_REVIEW
    assert outcome.handoff.disposition is DecisionDisposition.HUMAN_REVIEW_REQUIRED
    assert outcome.handoff.proposed_action is action
    assert outcome.handoff.execution_authorized is False
    assert (fast.calls, slow.calls) == (1, 0)


def test_double_abstention_returns_non_actionable_handoff() -> None:
    kernel, fast, slow, request = _kernel(
        AbstainedDecision("fast", 0.2, "insufficient evidence"),
        AbstainedDecision("slow", 0.3, "still insufficient evidence"),
    )

    outcome = kernel.decide(request)

    assert outcome.routed.route is DecisionRoute.HUMAN_REVIEW
    assert outcome.handoff.disposition is DecisionDisposition.ABSTAINED
    assert outcome.handoff.proposed_action is None
    assert outcome.handoff.execution_authorized is False
    assert (fast.calls, slow.calls) == (1, 1)


def test_compile_errors_happen_before_models_and_typed_errors_propagate() -> None:
    fast = FakeModel(SelectedActionDecision("fast", "inspect", 0.9, 0.9))
    slow = FakeModel(SelectedActionDecision("slow", "inspect", 0.9, 0.9))
    kernel = decision_kernel.DecisionKernel(
        fast,
        slow,
        _policy(),
    )
    invalid_request = _request(legal_actions=())

    with pytest.raises(DecisionFrameBuildError):
        _ = kernel.decide(invalid_request)
    assert (fast.calls, slow.calls) == (0, 0)

    unknown_request = _request()
    unknown_fast = FakeModel(SelectedActionDecision("fast", "missing", 0.9, 0.9))
    unknown_kernel = decision_kernel.DecisionKernel(
        unknown_fast,
        slow,
        _policy(),
    )
    with pytest.raises(UnknownActionError):
        _ = unknown_kernel.decide(unknown_request)
    assert (unknown_fast.calls, slow.calls) == (1, 0)


def test_handoff_error_propagates_unchanged_without_partial_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = DecisionHandoffError(field="handoff", detail="synthetic failure")
    kernel, _, _, request = _kernel(
        SelectedActionDecision("fast", "inspect", 0.95, 0.95),
        AbstainedDecision("slow", 0.1, "unused"),
    )

    def fail_handoff(frame: DecisionFrame, routed: RoutedDecision) -> AgentDecisionHandoff:
        del frame, routed
        raise error

    monkeypatch.setattr(decision_kernel, "build_agent_decision_handoff", fail_handoff)

    with pytest.raises(DecisionHandoffError) as raised:
        _ = kernel.decide(request)

    assert raised.value is error


def test_outcome_is_exactly_frozen_and_kernel_is_injected_and_deterministic() -> None:
    assert tuple(field.name for field in fields(decision_kernel.DecisionKernelOutcome)) == ("frame", "routed", "handoff")
    assert list(inspect.signature(decision_kernel.DecisionKernel).parameters) == ["fast_model", "slow_model", "policy"]

    first, _, _, request = _kernel(
        SelectedActionDecision("fast", "inspect", 0.95, 0.95),
        AbstainedDecision("slow", 0.1, "unused"),
    )
    second, _, _, _ = _kernel(
        SelectedActionDecision("fast", "inspect", 0.95, 0.95),
        AbstainedDecision("slow", 0.1, "unused"),
    )
    second_request = DecisionFrameRequest(
        goal=request.goal,
        as_of=request.as_of,
        context_pack=request.context_pack,
        legal_actions=request.legal_actions,
        max_context_items=request.max_context_items,
    )
    first_outcome = first.decide(request)
    second_outcome = second.decide(second_request)

    assert first_outcome == second_outcome
    assert not hasattr(first, "__dict__")
    with pytest.raises(FrozenInstanceError):
        first_outcome.frame = first_outcome.frame


def test_kernel_uses_compile_route_handoff_aliases_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    frame = compile_decision_frame(request)
    result = SelectedActionDecision("fake", "inspect", 0.9, 0.9)
    routed = RoutedDecision(DecisionRoute.FAST_MODEL, result, ("fake",))
    handoff = AgentDecisionHandoff(
        frame_id=frame.frame_id,
        query_id=frame.query_id,
        goal=frame.goal,
        as_of=frame.as_of,
        disposition=DecisionDisposition.ACTION_PROPOSED,
        route=routed.route,
        result=result,
        proposed_action=frame.legal_actions[0],
        evidence_ids=frame.evidence_ids,
        model_trace=routed.model_trace,
    )
    calls: list[str] = []

    def fake_compile(value: DecisionFrameRequest) -> DecisionFrame:
        calls.append("compile")
        assert value is request
        return frame

    class FakeRouter:
        def __init__(self, policy: DecisionRoutePolicy) -> None:
            del policy

        def route(
            self,
            value: DecisionFrame,
            *,
            fast_model: FakeModel,
            slow_model: FakeModel,
        ) -> RoutedDecision:
            calls.append("route")
            assert value is frame
            del fast_model, slow_model
            return routed

    def fake_handoff(value: DecisionFrame, value_routed: RoutedDecision) -> AgentDecisionHandoff:
        calls.append("handoff")
        assert value is frame
        assert value_routed is routed
        return handoff

    monkeypatch.setattr(decision_kernel, "compile_decision_frame", fake_compile)
    monkeypatch.setattr(decision_kernel, "DecisionRouter", FakeRouter)
    monkeypatch.setattr(decision_kernel, "build_agent_decision_handoff", fake_handoff)

    kernel = decision_kernel.DecisionKernel(
        FakeModel(result),
        FakeModel(result),
        _policy(),
    )

    outcome = kernel.decide(request)

    assert outcome == decision_kernel.DecisionKernelOutcome(frame, routed, handoff)
    assert calls == ["compile", "route", "handoff"]
