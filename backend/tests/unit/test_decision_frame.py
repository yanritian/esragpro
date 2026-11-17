from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from app.memory_core.decision_contracts import (
    ActionRisk,
    CandidateAction,
    DecisionFrameBuildError,
    StateAssertionKind,
)
from app.memory_core.decision_frame import (
    DecisionFrameRequest,
    compile_decision_frame,
)
from app.memory_core.world_memory import WorldContextItem, WorldContextItemKind, WorldContextPack


def _item(
    kind: WorldContextItemKind,
    content: str,
    evidence_ids: tuple[UUID, ...] = (),
) -> WorldContextItem:
    return WorldContextItem(kind=kind, id=uuid4(), content=content, evidence_ids=evidence_ids)


def _actions() -> tuple[CandidateAction, ...]:
    return (
        CandidateAction(
            action_id="inspect",
            description="inspect the available evidence",
            risk=ActionRisk.LOW,
        ),
    )


def _request(
    items: tuple[WorldContextItem, ...],
    *,
    max_context_items: int = 8,
    legal_actions: tuple[CandidateAction, ...] | None = None,
) -> DecisionFrameRequest:
    pack = WorldContextPack(query_id=uuid4(), items=items)
    return DecisionFrameRequest(
        goal="choose a safe next step",
        as_of=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
        context_pack=pack,
        legal_actions=_actions() if legal_actions is None else legal_actions,
        max_context_items=max_context_items,
    )


def test_compiler_maps_typed_items_and_deduplicates_evidence_in_order() -> None:
    evidence_a = uuid4()
    evidence_b = uuid4()
    observation = _item(
        WorldContextItemKind.OBSERVATION,
        "an observation",
        (evidence_a,),
    )
    state = _item(WorldContextItemKind.STATE, "a current state", (evidence_b, evidence_a))
    hypothesis = _item(WorldContextItemKind.HYPOTHESIS, "an unresolved hypothesis", (evidence_b,))
    fact = _item(WorldContextItemKind.FACT, "a supported fact", (evidence_a,))
    request = _request((observation, state, hypothesis, fact))

    frame = compile_decision_frame(request)

    assert frame.frame_id == request.context_pack.query_id
    assert frame.query_id == request.context_pack.query_id
    assert frame.goal == request.goal
    assert frame.as_of == datetime(2026, 9, 29, 12, 0)
    assert tuple(assertion.item_id for assertion in frame.asserted_state) == (state.id, fact.id)
    assert frame.asserted_state[0].kind is StateAssertionKind.STATE
    assert tuple(item.id for item in frame.observations) == (observation.id,)
    assert tuple(item.id for item in frame.uncertainties) == (hypothesis.id,)
    assert frame.evidence_ids == (evidence_a, evidence_b)


def test_compiler_uses_a_stable_prefix_and_ignores_items_beyond_budget() -> None:
    retained_evidence = uuid4()
    outside_item = _item(WorldContextItemKind.FACT, "unsupported outside prefix")
    request = _request(
        (
            _item(WorldContextItemKind.OBSERVATION, "retained", (retained_evidence,)),
            outside_item,
        ),
        max_context_items=1,
    )

    frame = compile_decision_frame(request)

    assert len(frame.observations) == 1
    assert frame.observations[0].content == "retained"
    assert frame.asserted_state == ()
    assert frame.evidence_ids == (retained_evidence,)


@pytest.mark.parametrize(
    "request_factory",
    [
        lambda: _request((), max_context_items=1, legal_actions=()),
        lambda: _request(
            (_item(WorldContextItemKind.FACT, "unsupported fact"),),
            max_context_items=1,
        ),
        lambda: _request((_item(WorldContextItemKind.OBSERVATION, "no evidence"),), max_context_items=1),
    ],
)
def test_compiler_fails_closed_for_missing_legal_actions_or_evidence(
    request_factory: Callable[[], DecisionFrameRequest],
) -> None:
    request = request_factory()
    with pytest.raises(DecisionFrameBuildError):
        _ = compile_decision_frame(request)


def test_compiler_rejects_non_positive_budget() -> None:
    with pytest.raises(DecisionFrameBuildError):
        _ = compile_decision_frame(_request((), max_context_items=0))


def test_compiler_preserves_passive_context_pack() -> None:
    item = _item(WorldContextItemKind.OBSERVATION, "unchanged", (uuid4(),))
    request = _request((item,))
    before = request.context_pack

    _ = compile_decision_frame(request)

    assert request.context_pack is before
    assert request.context_pack.items == (item,)
