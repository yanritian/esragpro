from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.memory_core.decision_contracts import (
    AbstainedDecision,
    ActionRisk,
    CandidateAction,
    DecisionFrame,
    DecisionResult,
    DecisionRoute,
    SelectedActionDecision,
)
from app.memory_core.decision_frame import DecisionFrameRequest, compile_decision_frame
from app.memory_core.decision_router import DecisionRoutePolicy, DecisionRouter
from app.memory_core.world_memory import WorldContextItem, WorldContextItemKind, WorldContextPack


class ScenarioModel:
    __slots__ = ("calls", "model_id", "result")

    def __init__(self, model_id: str, result: DecisionResult) -> None:
        self.model_id = model_id
        self.result = result
        self.calls = 0

    def decide(self, frame: DecisionFrame) -> DecisionResult:
        del frame
        self.calls += 1
        return self.result


def test_evidence_backed_decision_escalates_to_human_for_high_risk() -> None:
    evidence_id = uuid4()
    context_pack = WorldContextPack(
        query_id=uuid4(),
        items=(
            WorldContextItem(
                kind=WorldContextItemKind.OBSERVATION,
                id=uuid4(),
                content="the current context is inspectable",
                evidence_ids=(evidence_id,),
            ),
            WorldContextItem(
                kind=WorldContextItemKind.HYPOTHESIS,
                id=uuid4(),
                content="the next choice remains uncertain",
                evidence_ids=(evidence_id,),
            ),
        ),
    )
    high_risk = CandidateAction("escalate", "request human review", ActionRisk.HIGH)
    request = DecisionFrameRequest(
        goal="choose a bounded response",
        as_of=datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc),
        context_pack=context_pack,
        legal_actions=(high_risk,),
        max_context_items=2,
    )
    frame = compile_decision_frame(request)
    fast = ScenarioModel("fast", AbstainedDecision("fast", 0.3, "low confidence"))
    slow = ScenarioModel("slow", SelectedActionDecision("slow", "escalate", 0.95, 0.95))

    routed = DecisionRouter(
        DecisionRoutePolicy(fast_confidence_threshold=0.8, slow_confidence_threshold=0.7)
    ).route(frame, fast_model=fast, slow_model=slow)

    assert frame.evidence_ids == (evidence_id,)
    assert routed.route is DecisionRoute.HUMAN_REVIEW
    assert isinstance(routed.result, SelectedActionDecision)
    assert routed.result.action_id == "escalate"
    assert routed.model_trace == ("fast", "slow")
    assert fast.calls == 1
    assert slow.calls == 1
