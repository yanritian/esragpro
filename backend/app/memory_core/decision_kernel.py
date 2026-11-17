from __future__ import annotations

from dataclasses import dataclass

from app.memory_core.decision_contracts import (
    DecisionFrame,
    DecisionModel,
    RoutedDecision,
)
from app.memory_core.decision_frame import DecisionFrameRequest, compile_decision_frame
from app.memory_core.decision_handoff import (
    AgentDecisionHandoff,
    build_agent_decision_handoff,
)
from app.memory_core.decision_router import DecisionRoutePolicy, DecisionRouter


@dataclass(frozen=True, slots=True)
class DecisionKernelOutcome:
    frame: DecisionFrame
    routed: RoutedDecision
    handoff: AgentDecisionHandoff


class DecisionKernel:
    __slots__ = ("_fast_model", "_slow_model", "_router")

    def __init__(
        self,
        fast_model: DecisionModel,
        slow_model: DecisionModel,
        policy: DecisionRoutePolicy,
    ) -> None:
        self._fast_model = fast_model
        self._slow_model = slow_model
        self._router = DecisionRouter(policy)

    def decide(self, request: DecisionFrameRequest) -> DecisionKernelOutcome:
        frame = compile_decision_frame(request)
        routed = self._router.route(
            frame,
            fast_model=self._fast_model,
            slow_model=self._slow_model,
        )
        handoff = build_agent_decision_handoff(frame, routed)
        return DecisionKernelOutcome(frame=frame, routed=routed, handoff=handoff)


__all__ = ["DecisionKernel", "DecisionKernelOutcome"]
