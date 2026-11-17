from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from app.memory_core.decision_contracts import (
    AbstainedDecision,
    ActionRisk,
    CandidateAction,
    DecisionContractError,
    DecisionFrame,
    DecisionModel,
    DecisionRoute,
    RoutedDecision,
    SelectedActionDecision,
    UnknownActionError,
)


def _threshold(value: float, field: str) -> float:
    if not 0.0 <= value <= 1.0:
        raise DecisionContractError(field=field, detail="must be between 0 and 1")
    return value


@dataclass(frozen=True, slots=True)
class DecisionRoutePolicy:
    fast_confidence_threshold: float
    slow_confidence_threshold: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "fast_confidence_threshold",
            _threshold(self.fast_confidence_threshold, "fast_confidence_threshold"),
        )
        object.__setattr__(
            self,
            "slow_confidence_threshold",
            _threshold(self.slow_confidence_threshold, "slow_confidence_threshold"),
        )


def _legal_action(frame: DecisionFrame, result: SelectedActionDecision) -> CandidateAction:
    actions = {action.action_id: action for action in frame.legal_actions}
    action = actions.get(result.action_id)
    if action is None:
        raise UnknownActionError(model_id=result.model_id, action_id=result.action_id)
    return action


def _fast_needs_slow(action: CandidateAction, result: SelectedActionDecision, policy: DecisionRoutePolicy) -> bool:
    match action.risk:
        case ActionRisk.LOW:
            return result.confidence < policy.fast_confidence_threshold
        case ActionRisk.MEDIUM:
            return True
        case ActionRisk.HIGH:
            return False
        case unreachable:
            assert_never(unreachable)


def _slow_route(
    action: CandidateAction,
    result: SelectedActionDecision,
    policy: DecisionRoutePolicy,
) -> DecisionRoute:
    match action.risk:
        case ActionRisk.LOW | ActionRisk.MEDIUM:
            if result.confidence >= policy.slow_confidence_threshold:
                return DecisionRoute.SLOW_MODEL
            return DecisionRoute.HUMAN_REVIEW
        case ActionRisk.HIGH:
            return DecisionRoute.HUMAN_REVIEW
        case unreachable:
            assert_never(unreachable)


class DecisionRouter:
    def __init__(self, policy: DecisionRoutePolicy) -> None:
        self._policy = policy

    def route(
        self,
        frame: DecisionFrame,
        *,
        fast_model: DecisionModel,
        slow_model: DecisionModel,
    ) -> RoutedDecision:
        fast_result = fast_model.decide(frame)
        match fast_result:
            case SelectedActionDecision():
                fast_action = _legal_action(frame, fast_result)
                if fast_action.risk is ActionRisk.HIGH:
                    return RoutedDecision(
                        route=DecisionRoute.HUMAN_REVIEW,
                        result=fast_result,
                        model_trace=(fast_result.model_id,),
                    )
                if not _fast_needs_slow(fast_action, fast_result, self._policy):
                    return RoutedDecision(
                        route=DecisionRoute.FAST_MODEL,
                        result=fast_result,
                        model_trace=(fast_result.model_id,),
                    )
            case AbstainedDecision():
                pass
            case unreachable:
                assert_never(unreachable)

        slow_result = slow_model.decide(frame)
        match slow_result:
            case SelectedActionDecision():
                slow_action = _legal_action(frame, slow_result)
                route = _slow_route(slow_action, slow_result, self._policy)
            case AbstainedDecision():
                route = DecisionRoute.HUMAN_REVIEW
            case unreachable:
                assert_never(unreachable)
        return RoutedDecision(
            route=route,
            result=slow_result,
            model_trace=(fast_result.model_id, slow_result.model_id),
        )


def route_decision(
    frame: DecisionFrame,
    fast_model: DecisionModel,
    slow_model: DecisionModel,
    policy: DecisionRoutePolicy,
) -> RoutedDecision:
    return DecisionRouter(policy).route(frame, fast_model=fast_model, slow_model=slow_model)


__all__ = [
    "DecisionRoutePolicy",
    "DecisionRouter",
    "UnknownActionError",
    "route_decision",
]
