from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.memory_core.decision_contracts import (
    AbstainedDecision,
    ActionRisk,
    CandidateAction,
    DecisionContractError,
    DecisionFrame,
    DecisionRoute,
    SelectedActionDecision,
    StateAssertion,
    StateAssertionKind,
)


def _frame() -> DecisionFrame:
    evidence_id = uuid4()
    return DecisionFrame(
        frame_id=uuid4(),
        query_id=uuid4(),
        goal="choose a safe next step",
        as_of=datetime(2026, 9, 29, 12, 0),
        asserted_state=(
            StateAssertion(
                item_id=uuid4(),
                kind=StateAssertionKind.FACT,
                content="the observed state is current",
                evidence_ids=(evidence_id,),
            ),
        ),
        observations=(),
        uncertainties=(),
        legal_actions=(
            CandidateAction(
                action_id="inspect",
                description="inspect the available evidence",
                risk=ActionRisk.LOW,
            ),
        ),
        evidence_ids=(evidence_id,),
    )


def test_contracts_are_immutable_and_normalize_as_of_to_naive_utc() -> None:
    local_time = datetime(
        2026,
        9,
        29,
        14,
        0,
        tzinfo=timezone(timedelta(hours=2)),
    )
    frame = _frame()
    normalized = DecisionFrame(
        frame_id=frame.frame_id,
        query_id=frame.query_id,
        goal=frame.goal,
        as_of=local_time,
        asserted_state=frame.asserted_state,
        observations=frame.observations,
        uncertainties=frame.uncertainties,
        legal_actions=frame.legal_actions,
        evidence_ids=frame.evidence_ids,
    )

    assert normalized.as_of == datetime(2026, 9, 29, 12, 0)
    assert normalized.as_of.tzinfo is None
    with pytest.raises(FrozenInstanceError):
        normalized.goal = "changed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("probability", -0.01),
        ("probability", 1.01),
        ("confidence", -0.01),
        ("confidence", 1.01),
    ],
)
def test_decision_scores_must_be_between_zero_and_one(field: str, value: float) -> None:
    kwargs = {
        "model_id": "fast",
        "action_id": "inspect",
        "probability": 0.5,
        "confidence": 0.5,
    }
    kwargs[field] = value
    with pytest.raises(DecisionContractError):
        _ = SelectedActionDecision(**kwargs)

    abstained_kwargs = {"model_id": "fast", "confidence": 0.5, "reason": "uncertain"}
    abstained_kwargs[field if field == "confidence" else "confidence"] = value
    with pytest.raises(DecisionContractError):
        _ = AbstainedDecision(**abstained_kwargs)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CandidateAction(action_id=" ", description="inspect", risk=ActionRisk.LOW),
        lambda: CandidateAction(action_id="inspect", description=" ", risk=ActionRisk.LOW),
        lambda: SelectedActionDecision(
            model_id=" ", action_id="inspect", probability=0.5, confidence=0.5
        ),
        lambda: AbstainedDecision(model_id=" ", confidence=0.5, reason="uncertain"),
        lambda: AbstainedDecision(model_id="fast", confidence=0.5, reason=" "),
    ],
)
def test_contracts_reject_blank_identifiers_and_text(
    factory: Callable[[], CandidateAction | SelectedActionDecision | AbstainedDecision],
) -> None:
    with pytest.raises(DecisionContractError):
        factory()


def test_decision_frame_rejects_duplicate_legal_action_ids() -> None:
    action = CandidateAction(action_id="inspect", description="inspect", risk=ActionRisk.LOW)
    frame = _frame()
    with pytest.raises(DecisionContractError):
        _ = DecisionFrame(
            frame_id=frame.frame_id,
            query_id=frame.query_id,
            goal=frame.goal,
            as_of=frame.as_of,
            asserted_state=frame.asserted_state,
            observations=frame.observations,
            uncertainties=frame.uncertainties,
            legal_actions=(action, action),
            evidence_ids=frame.evidence_ids,
        )


def test_abstained_decision_has_no_action_id() -> None:
    result = AbstainedDecision(model_id="fast", confidence=0.2, reason="insufficient evidence")

    assert result.model_id == "fast"
    assert not hasattr(result, "action_id")
    assert result.reason == "insufficient evidence"


def test_route_and_result_values_are_typed() -> None:
    assert ActionRisk.LOW.value == "low"
    assert DecisionRoute.HUMAN_REVIEW.value == "human_review"
    assert isinstance(_frame().frame_id, UUID)
