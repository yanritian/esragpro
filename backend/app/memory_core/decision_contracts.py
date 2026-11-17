from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, TypeAlias
from uuid import UUID

from app.memory_core.world_memory import WorldContextItem

Identifier: TypeAlias = str | UUID


class DecisionContractError(ValueError):
    __slots__ = ("field", "detail")

    field: str
    detail: str

    def __init__(self, *, field: str, detail: str) -> None:
        self.field = field
        self.detail = detail
        super().__init__(f"{field}: {detail}")


class DuplicateActionError(DecisionContractError):
    __slots__ = ("action_id",)

    action_id: str

    def __init__(self, action_id: str) -> None:
        self.action_id = action_id
        super().__init__(field="legal_actions", detail=f"duplicate action id {action_id!r}")


class UnknownActionError(DecisionContractError):
    __slots__ = ("action_id", "model_id")

    action_id: str
    model_id: str

    def __init__(self, *, model_id: str, action_id: str) -> None:
        self.model_id = model_id
        self.action_id = action_id
        super().__init__(field="action_id", detail=f"{model_id!r} selected unknown action {action_id!r}")


class DecisionFrameBuildError(DecisionContractError):
    __slots__ = ()


class ActionRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class StateAssertionKind(str, Enum):
    FACT = "fact"
    STATE = "state"


class DecisionRoute(str, Enum):
    FAST_MODEL = "fast_model"
    SLOW_MODEL = "slow_model"
    HUMAN_REVIEW = "human_review"


def _identifier(value: Identifier, field: str) -> Identifier:
    if isinstance(value, str):
        if not value.strip():
            raise DecisionContractError(field=field, detail="must be non-empty")
        return value
    if isinstance(value, UUID):
        return value
    raise DecisionContractError(field=field, detail="must be a string or UUID")


def _text(value: str, field: str) -> str:
    if not value.strip():
        raise DecisionContractError(field=field, detail="must be non-empty")
    return value


def _score(value: float, field: str) -> float:
    if not 0.0 <= value <= 1.0:
        raise DecisionContractError(field=field, detail="must be between 0 and 1")
    return value


def _risk(value: ActionRisk | str) -> ActionRisk:
    if isinstance(value, ActionRisk):
        return value
    try:
        return ActionRisk(value)
    except ValueError as exc:
        raise DecisionContractError(field="risk", detail=f"unknown risk {value!r}") from exc


def _assertion_kind(value: StateAssertionKind | str) -> StateAssertionKind:
    if isinstance(value, StateAssertionKind):
        return value
    try:
        return StateAssertionKind(value)
    except ValueError as exc:
        raise DecisionContractError(field="kind", detail=f"unknown assertion kind {value!r}") from exc


def _route(value: DecisionRoute | str) -> DecisionRoute:
    if isinstance(value, DecisionRoute):
        return value
    try:
        return DecisionRoute(value)
    except ValueError as exc:
        raise DecisionContractError(field="route", detail=f"unknown route {value!r}") from exc


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class CandidateAction:
    action_id: str
    description: str
    risk: ActionRisk

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _text(self.action_id, "action_id"))
        object.__setattr__(self, "description", _text(self.description, "description"))
        object.__setattr__(self, "risk", _risk(self.risk))


@dataclass(frozen=True, slots=True)
class StateAssertion:
    item_id: Identifier
    kind: StateAssertionKind
    content: str
    evidence_ids: tuple[Identifier, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _identifier(self.item_id, "item_id"))
        object.__setattr__(self, "kind", _assertion_kind(self.kind))
        object.__setattr__(self, "content", _text(self.content, "content"))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        for evidence_id in self.evidence_ids:
            _identifier(evidence_id, "evidence_ids")


@dataclass(frozen=True, slots=True)
class DecisionFrame:
    frame_id: Identifier
    query_id: Identifier
    goal: str
    as_of: datetime
    asserted_state: tuple[StateAssertion, ...]
    observations: tuple[WorldContextItem, ...]
    uncertainties: tuple[WorldContextItem, ...]
    legal_actions: tuple[CandidateAction, ...]
    evidence_ids: tuple[Identifier, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame_id", _identifier(self.frame_id, "frame_id"))
        object.__setattr__(self, "query_id", _identifier(self.query_id, "query_id"))
        object.__setattr__(self, "goal", _text(self.goal, "goal"))
        object.__setattr__(self, "as_of", _utc_naive(self.as_of))
        object.__setattr__(self, "asserted_state", tuple(self.asserted_state))
        object.__setattr__(self, "observations", tuple(self.observations))
        object.__setattr__(self, "uncertainties", tuple(self.uncertainties))
        object.__setattr__(self, "legal_actions", tuple(self.legal_actions))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        if not self.legal_actions:
            raise DecisionContractError(field="legal_actions", detail="must be non-empty")
        seen: set[str] = set()
        for action in self.legal_actions:
            if action.action_id in seen:
                raise DuplicateActionError(action.action_id)
            seen.add(action.action_id)
        for evidence_id in self.evidence_ids:
            _identifier(evidence_id, "evidence_ids")


@dataclass(frozen=True, slots=True)
class SelectedActionDecision:
    model_id: str
    action_id: str
    probability: float
    confidence: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "action_id", _text(self.action_id, "action_id"))
        object.__setattr__(self, "probability", _score(self.probability, "probability"))
        object.__setattr__(self, "confidence", _score(self.confidence, "confidence"))


@dataclass(frozen=True, slots=True)
class AbstainedDecision:
    model_id: str
    confidence: float
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "confidence", _score(self.confidence, "confidence"))
        object.__setattr__(self, "reason", _text(self.reason, "reason"))


DecisionResult: TypeAlias = SelectedActionDecision | AbstainedDecision


class DecisionModel(Protocol):
    def decide(self, frame: DecisionFrame) -> DecisionResult: ...


@dataclass(frozen=True, slots=True)
class RoutedDecision:
    route: DecisionRoute
    result: DecisionResult
    model_trace: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "route", _route(self.route))
        object.__setattr__(self, "model_trace", tuple(self.model_trace))
        for model_id in self.model_trace:
            _text(model_id, "model_trace")


__all__ = [
    "AbstainedDecision",
    "ActionRisk",
    "CandidateAction",
    "DecisionContractError",
    "DecisionFrame",
    "DecisionFrameBuildError",
    "DecisionModel",
    "DecisionResult",
    "DecisionRoute",
    "DuplicateActionError",
    "Identifier",
    "RoutedDecision",
    "SelectedActionDecision",
    "StateAssertion",
    "StateAssertionKind",
    "UnknownActionError",
]
