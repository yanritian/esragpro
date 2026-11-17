from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import uuid4

import pytest

from app.memory_core.decision_contracts import (
    AbstainedDecision,
    ActionRisk,
    CandidateAction,
    DecisionFrame,
    DecisionResult,
    DecisionRoute,
    SelectedActionDecision,
)
from app.memory_core.decision_router import (
    DecisionRoutePolicy,
    DecisionRouter,
    UnknownActionError,
)


def _frame() -> DecisionFrame:
    return DecisionFrame(
        frame_id=uuid4(),
        query_id=uuid4(),
        goal="choose",
        as_of=datetime(2026, 9, 29, 12, 0),
        asserted_state=(),
        observations=(),
        uncertainties=(),
        legal_actions=(
            CandidateAction("low", "low-risk action", ActionRisk.LOW),
            CandidateAction("medium", "medium-risk action", ActionRisk.MEDIUM),
            CandidateAction("high", "high-risk action", ActionRisk.HIGH),
        ),
        evidence_ids=(),
    )


class FakeModel:
    __slots__ = ("calls", "results")

    def __init__(self, results: Sequence[DecisionResult]) -> None:
        self.results = tuple(results)
        self.calls = 0

    def decide(self, frame: DecisionFrame) -> DecisionResult:
        del frame
        self.calls += 1
        return self.results[self.calls - 1]


def _selected(model_id: str, action_id: str, confidence: float) -> SelectedActionDecision:
    return SelectedActionDecision(
        model_id=model_id,
        action_id=action_id,
        probability=confidence,
        confidence=confidence,
    )


def _router() -> DecisionRouter:
    return DecisionRouter(
        DecisionRoutePolicy(fast_confidence_threshold=0.8, slow_confidence_threshold=0.7)
    )


@pytest.mark.parametrize(
    ("action_id", "confidence", "expected_route", "slow_called"),
    [
        ("low", 0.8, DecisionRoute.FAST_MODEL, False),
        ("low", 0.79, DecisionRoute.SLOW_MODEL, True),
        ("medium", 0.99, DecisionRoute.SLOW_MODEL, True),
        ("high", 0.99, DecisionRoute.HUMAN_REVIEW, False),
        ("high", 0.1, DecisionRoute.HUMAN_REVIEW, False),
    ],
)
def test_fast_selection_precedence_and_call_counts(
    action_id: str,
    confidence: float,
    expected_route: DecisionRoute,
    slow_called: bool,
) -> None:
    fast = FakeModel((_selected("fast", action_id, confidence),))
    slow = FakeModel((_selected("slow", "low", 0.9),))

    routed = _router().route(_frame(), fast_model=fast, slow_model=slow)

    assert routed.route is expected_route
    assert fast.calls == 1
    assert slow.calls == int(slow_called)
    assert routed.model_trace == (("fast",) if not slow_called else ("fast", "slow"))


@pytest.mark.parametrize(
    ("slow_result", "expected_route"),
    [
        (_selected("slow", "low", 0.7), DecisionRoute.SLOW_MODEL),
        (_selected("slow", "medium", 0.7), DecisionRoute.SLOW_MODEL),
        (_selected("slow", "high", 0.99), DecisionRoute.HUMAN_REVIEW),
        (AbstainedDecision("slow", 0.3, "still uncertain"), DecisionRoute.HUMAN_REVIEW),
        (_selected("slow", "low", 0.69), DecisionRoute.HUMAN_REVIEW),
    ],
)
def test_slow_model_precedence_and_human_gate(
    slow_result: DecisionResult,
    expected_route: DecisionRoute,
) -> None:
    fast = FakeModel((AbstainedDecision("fast", 0.2, "uncertain"),))
    slow = FakeModel((slow_result,))

    routed = _router().route(_frame(), fast_model=fast, slow_model=slow)

    assert routed.route is expected_route
    assert fast.calls == 1
    assert slow.calls == 1
    assert routed.model_trace == ("fast", "slow")
    assert routed.result == slow_result


def test_fast_abstention_escalates_once_and_preserves_result() -> None:
    fast_result = AbstainedDecision("fast", 0.2, "insufficient evidence")
    fast = FakeModel((fast_result,))
    slow = FakeModel((_selected("slow", "low", 0.75),))

    routed = _router().route(_frame(), fast_model=fast, slow_model=slow)

    assert routed.route is DecisionRoute.SLOW_MODEL
    assert routed.result == slow.results[0]
    assert routed.model_trace == ("fast", "slow")


@pytest.mark.parametrize("model_position", ["fast", "slow"])
def test_unknown_action_ids_are_typed_errors_without_fallback(model_position: str) -> None:
    unknown = _selected(model_position, "missing", 0.99)
    fast = FakeModel((unknown if model_position == "fast" else AbstainedDecision("fast", 0.2, "uncertain"),))
    slow = FakeModel((unknown if model_position == "slow" else _selected("slow", "low", 0.9),))

    with pytest.raises(UnknownActionError):
        _ = _router().route(_frame(), fast_model=fast, slow_model=slow)

    assert fast.calls == 1
    assert slow.calls == int(model_position == "slow")
