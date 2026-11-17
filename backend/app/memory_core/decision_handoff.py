from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, assert_never
from uuid import UUID

from app.memory_core.decision_contracts import (
    AbstainedDecision,
    ActionRisk,
    CandidateAction,
    DecisionContractError,
    DecisionFrame,
    DecisionResult,
    DecisionRoute,
    RoutedDecision,
    SelectedActionDecision,
    UnknownActionError,
    Identifier,
)


class DecisionHandoffError(DecisionContractError):
    __slots__ = ()


class DecisionDisposition(str, Enum):
    ACTION_PROPOSED = "action_proposed"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    ABSTAINED = "abstained"


def _fail(field_name: str, detail: str) -> DecisionHandoffError:
    return DecisionHandoffError(field=field_name, detail=detail)


def _identifier(value: Identifier, field_name: str) -> Identifier:
    if isinstance(value, str):
        if not value.strip():
            raise _fail(field_name, "must be non-empty")
        return value
    if isinstance(value, UUID):
        return value
    raise _fail(field_name, "must be a string or UUID")


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(field_name, "must be non-empty")
    return value


def _utc_naive(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise _fail("as_of", "must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _route(value: DecisionRoute | str) -> DecisionRoute:
    if isinstance(value, DecisionRoute):
        return value
    if isinstance(value, str):
        try:
            return DecisionRoute(value)
        except ValueError as exc:
            raise _fail("route", f"unknown route {value!r}") from exc
    raise _fail("route", "must be a DecisionRoute or string")


def _disposition(value: DecisionDisposition | str) -> DecisionDisposition:
    if isinstance(value, DecisionDisposition):
        return value
    if isinstance(value, str):
        try:
            return DecisionDisposition(value)
        except ValueError as exc:
            raise _fail("disposition", f"unknown disposition {value!r}") from exc
    raise _fail("disposition", "must be a DecisionDisposition or string")


def _result(value: DecisionResult) -> DecisionResult:
    if isinstance(value, (SelectedActionDecision, AbstainedDecision)):
        return value
    raise _fail("result", "must be a supported decision result")


def _action(value: CandidateAction | None) -> CandidateAction | None:
    if value is None or isinstance(value, CandidateAction):
        return value
    raise _fail("proposed_action", "must be a CandidateAction or None")


def _tuple_identifiers(values: tuple[Identifier, ...] | list[Identifier]) -> tuple[Identifier, ...]:
    if not isinstance(values, (tuple, list)):
        raise _fail("evidence_ids", "must be a tuple or list")
    normalized = tuple(values)
    for identifier in normalized:
        _identifier(identifier, "evidence_ids")
    if not normalized:
        raise _fail("evidence_ids", "must be non-empty")
    return normalized


def _tuple_model_ids(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise _fail("model_trace", "must be a tuple or list")
    normalized = tuple(values)
    for model_id in normalized:
        _text(model_id, "model_trace")
    if not normalized:
        raise _fail("model_trace", "must be non-empty")
    return normalized


def _validate_mapping(
    route: DecisionRoute,
    disposition: DecisionDisposition,
    result: DecisionResult,
    proposed_action: CandidateAction | None,
) -> None:
    match result:
        case SelectedActionDecision():
            if proposed_action is None:
                raise _fail("proposed_action", "selected results require an action")
            if proposed_action.action_id != result.action_id:
                raise _fail("proposed_action", "action_id must match the selected result")
            match route:
                case DecisionRoute.FAST_MODEL:
                    if proposed_action.risk is not ActionRisk.LOW or disposition is not DecisionDisposition.ACTION_PROPOSED:
                        raise _fail("route", "fast_model only proposes low-risk actions")
                case DecisionRoute.SLOW_MODEL:
                    if proposed_action.risk not in (ActionRisk.LOW, ActionRisk.MEDIUM) or disposition is not DecisionDisposition.ACTION_PROPOSED:
                        raise _fail("route", "slow_model only proposes low- or medium-risk actions")
                case DecisionRoute.HUMAN_REVIEW:
                    if disposition is not DecisionDisposition.HUMAN_REVIEW_REQUIRED:
                        raise _fail("disposition", "human_review selections require human review")
                case unreachable:
                    assert_never(unreachable)
        case AbstainedDecision():
            if proposed_action is not None:
                raise _fail("proposed_action", "abstentions cannot carry an action")
            if route is not DecisionRoute.HUMAN_REVIEW or disposition is not DecisionDisposition.ABSTAINED:
                raise _fail("route", "abstentions require human_review and abstained disposition")
        case unreachable:
            assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class AgentDecisionHandoff:
    frame_id: Identifier
    query_id: Identifier
    goal: str
    as_of: datetime
    disposition: DecisionDisposition
    route: DecisionRoute
    result: DecisionResult
    proposed_action: CandidateAction | None
    evidence_ids: tuple[Identifier, ...]
    model_trace: tuple[str, ...]
    execution_authorized: Literal[False] = field(default=False, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame_id", _identifier(self.frame_id, "frame_id"))
        object.__setattr__(self, "query_id", _identifier(self.query_id, "query_id"))
        object.__setattr__(self, "goal", _text(self.goal, "goal"))
        object.__setattr__(self, "as_of", _utc_naive(self.as_of))
        route = _route(self.route)
        disposition = _disposition(self.disposition)
        result = _result(self.result)
        proposed_action = _action(self.proposed_action)
        evidence_ids = _tuple_identifiers(self.evidence_ids)
        model_trace = _tuple_model_ids(self.model_trace)
        if model_trace[-1] != result.model_id:
            raise _fail("model_trace", "final model must match the decision result")
        _validate_mapping(route, disposition, result, proposed_action)
        object.__setattr__(self, "route", route)
        object.__setattr__(self, "disposition", disposition)
        object.__setattr__(self, "result", result)
        object.__setattr__(self, "proposed_action", proposed_action)
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "model_trace", model_trace)


def build_agent_decision_handoff(
    frame: DecisionFrame,
    routed: RoutedDecision,
) -> AgentDecisionHandoff:
    if not frame.evidence_ids:
        raise _fail("evidence_ids", "frame must contain evidence")
    if not routed.model_trace:
        raise _fail("model_trace", "routed decision must contain a model trace")
    result = _result(routed.result)
    if routed.model_trace[-1] != result.model_id:
        raise _fail("model_trace", "final model must match the decision result")

    proposed_action: CandidateAction | None = None
    match result:
        case SelectedActionDecision(action_id=action_id, model_id=model_id):
            for candidate in frame.legal_actions:
                if candidate.action_id == action_id:
                    proposed_action = candidate
                    break
            if proposed_action is None:
                raise UnknownActionError(model_id=model_id, action_id=action_id)
            match routed.route:
                case DecisionRoute.FAST_MODEL | DecisionRoute.SLOW_MODEL:
                    disposition = DecisionDisposition.ACTION_PROPOSED
                case DecisionRoute.HUMAN_REVIEW:
                    disposition = DecisionDisposition.HUMAN_REVIEW_REQUIRED
                case unreachable:
                    assert_never(unreachable)
        case AbstainedDecision():
            disposition = DecisionDisposition.ABSTAINED
        case unreachable:
            assert_never(unreachable)
    return AgentDecisionHandoff(
        frame_id=frame.frame_id,
        query_id=frame.query_id,
        goal=frame.goal,
        as_of=frame.as_of,
        disposition=disposition,
        route=routed.route,
        result=result,
        proposed_action=proposed_action,
        evidence_ids=frame.evidence_ids,
        model_trace=routed.model_trace,
    )


__all__ = [
    "AgentDecisionHandoff",
    "DecisionDisposition",
    "DecisionHandoffError",
    "build_agent_decision_handoff",
]
