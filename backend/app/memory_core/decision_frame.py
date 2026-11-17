from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

from app.memory_core.decision_contracts import (
    CandidateAction,
    DecisionFrame,
    DecisionFrameBuildError,
    Identifier,
    StateAssertion,
    StateAssertionKind,
)
from app.memory_core.world_memory import WorldContextItem, WorldContextItemKind, WorldContextPack


@dataclass(frozen=True, slots=True)
class DecisionFrameRequest:
    goal: str
    as_of: datetime
    context_pack: WorldContextPack
    legal_actions: tuple[CandidateAction, ...]
    max_context_items: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "legal_actions", tuple(self.legal_actions))


def _fail(field: str, detail: str) -> DecisionFrameBuildError:
    return DecisionFrameBuildError(field=field, detail=detail)


def compile_decision_frame(request: DecisionFrameRequest) -> DecisionFrame:
    if request.max_context_items < 1:
        raise _fail("max_context_items", "must be at least 1")
    if not request.legal_actions:
        raise _fail("legal_actions", "must be non-empty")

    retained = request.context_pack.items[: request.max_context_items]
    asserted_state: list[StateAssertion] = []
    observations: list[WorldContextItem] = []
    uncertainties: list[WorldContextItem] = []
    evidence_ids: list[Identifier] = []
    seen_evidence: set[Identifier] = set()

    for item in retained:
        for evidence_id in item.evidence_ids:
            if evidence_id not in seen_evidence:
                seen_evidence.add(evidence_id)
                evidence_ids.append(evidence_id)
        match item.kind:
            case WorldContextItemKind.FACT:
                if not item.evidence_ids:
                    raise _fail("asserted_state", f"fact {item.id!r} has no evidence")
                asserted_state.append(
                    StateAssertion(
                        item_id=item.id,
                        kind=StateAssertionKind.FACT,
                        content=item.content,
                        evidence_ids=item.evidence_ids,
                    )
                )
            case WorldContextItemKind.STATE:
                if not item.evidence_ids:
                    raise _fail("asserted_state", f"state {item.id!r} has no evidence")
                asserted_state.append(
                    StateAssertion(
                        item_id=item.id,
                        kind=StateAssertionKind.STATE,
                        content=item.content,
                        evidence_ids=item.evidence_ids,
                    )
                )
            case WorldContextItemKind.OBSERVATION:
                observations.append(item)
            case WorldContextItemKind.HYPOTHESIS:
                uncertainties.append(item)
            case unreachable:
                assert_never(unreachable)

    if not evidence_ids:
        raise _fail("evidence_ids", "retained context must contribute evidence")

    return DecisionFrame(
        frame_id=request.context_pack.query_id,
        query_id=request.context_pack.query_id,
        goal=request.goal,
        as_of=request.as_of,
        asserted_state=tuple(asserted_state),
        observations=tuple(observations),
        uncertainties=tuple(uncertainties),
        legal_actions=request.legal_actions,
        evidence_ids=tuple(evidence_ids),
    )


__all__ = ["DecisionFrameRequest", "compile_decision_frame"]
