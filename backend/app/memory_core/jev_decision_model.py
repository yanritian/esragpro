from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Final, Literal, Protocol, TypedDict

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr, ValidationError

from app.memory_core.decision_contracts import (
    AbstainedDecision,
    DecisionFrame,
    DecisionResult,
    SelectedActionDecision,
    StateAssertion,
)
from app.memory_core.world_memory import WorldContextItem

JEV_MODEL: Final[str] = "jev-1.13.0"
QUESTION_ID: Final[str] = "next_action"
QUESTION_INSTRUCTIONS: Final[str] = (
    "Which legal action best advances the stated goal using only the supplied state? "
    "Select abstain when the evidence is insufficient."
)
ABSTAIN_ACTION_ID: Final[str] = "__adapter_abstain__"
ABSTAIN_DESCRIPTION: Final[str] = "Evidence is insufficient to select a legal action."
_MAX_LEGAL_ACTIONS: Final[int] = 254
_PROBABILITY_TOLERANCE: Final[float] = 1e-6


class JevAdapterError(Exception):
    __slots__ = ("detail",)
    detail: str

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)

    def __str__(self) -> str:
        return self.detail


class JevRequestError(JevAdapterError):
    __slots__ = ()


class JevTransportError(JevAdapterError):
    __slots__ = ()


class JevHttpStatusError(JevAdapterError):
    __slots__ = ("status_code",)
    status_code: int

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__("Jev returned a non-success HTTP status")


class JevResponseError(JevAdapterError):
    __slots__ = ()


class JevModelMismatchError(JevAdapterError):
    __slots__ = ("expected_model",)
    expected_model: str

    def __init__(self, expected_model: str) -> None:
        self.expected_model = expected_model
        super().__init__("Jev response model did not match the requested model")


@dataclass(frozen=True, slots=True)
class JevHttpResponse:
    status_code: int
    content: bytes


class JevTransport(Protocol):
    def post(self, body: bytes) -> JevHttpResponse: ...


@dataclass(frozen=True, slots=True)
class JevHttpSettings:
    api_key: str
    endpoint: str = "https://api.typesafe.ai/v1/systemone"
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if (
            not self.api_key.strip()
            or not self.endpoint.strip()
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise JevRequestError("invalid Jev HTTP settings")


class _ContextPayload(TypedDict):
    item_id: str
    kind: str
    content: str
    evidence_ids: list[str]


class _ActionPayload(TypedDict):
    action_id: str
    description: str
    risk: str


class _StatePayload(TypedDict):
    frame_id: str
    query_id: str
    goal: str
    as_of: str
    asserted_state: list[_ContextPayload]
    observations: list[_ContextPayload]
    uncertainties: list[_ContextPayload]
    legal_actions: list[_ActionPayload]
    evidence_ids: list[str]


class _JevQuestion(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    type: Literal["choice"]
    instructions: StrictStr
    criteria: dict[StrictStr, StrictStr]


class _JevQuestions(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    next_action: _JevQuestion


class _JevRequest(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    state: StrictStr
    model: StrictStr
    questions: _JevQuestions


class _JevAnswer(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    type: Literal["choice"]
    choice: StrictStr
    probabilities: dict[StrictStr, StrictFloat]
    confidence: StrictFloat


class _JevUsage(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    input_tokens: StrictInt = Field(ge=0)
    output_tokens: StrictInt = Field(ge=0)


class _JevResponse(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")
    model: StrictStr
    answers: dict[StrictStr, _JevAnswer]
    usage: _JevUsage


def _state_item(item: StateAssertion) -> _ContextPayload:
    return {
        "item_id": str(item.item_id),
        "kind": item.kind.value,
        "content": item.content,
        "evidence_ids": [str(value) for value in item.evidence_ids],
    }


def _world_item(item: WorldContextItem) -> _ContextPayload:
    return {
        "item_id": str(item.id),
        "kind": item.kind.value,
        "content": item.content,
        "evidence_ids": [str(value) for value in item.evidence_ids],
    }


def _serialize_frame(frame: DecisionFrame) -> str:
    state: _StatePayload = {
        "frame_id": str(frame.frame_id),
        "query_id": str(frame.query_id),
        "goal": frame.goal,
        "as_of": f"{frame.as_of.isoformat(timespec='microseconds')}Z",
        "asserted_state": [_state_item(item) for item in frame.asserted_state],
        "observations": [_world_item(item) for item in frame.observations],
        "uncertainties": [_world_item(item) for item in frame.uncertainties],
        "legal_actions": [
            {"action_id": action.action_id, "description": action.description, "risk": action.risk.value}
            for action in frame.legal_actions
        ],
        "evidence_ids": [str(value) for value in frame.evidence_ids],
    }
    return json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _criteria(frame: DecisionFrame) -> dict[str, str]:
    if len(frame.legal_actions) > _MAX_LEGAL_ACTIONS:
        raise JevRequestError("too many legal actions for Jev Choice")
    criteria = {action.action_id: action.description for action in frame.legal_actions}
    if ABSTAIN_ACTION_ID in criteria:
        raise JevRequestError("reserved Jev action ID collides with a legal action")
    criteria[ABSTAIN_ACTION_ID] = ABSTAIN_DESCRIPTION
    return criteria


def _request_body(frame: DecisionFrame, model: str, criteria: dict[str, str]) -> bytes:
    request = _JevRequest(
        state=_serialize_frame(frame),
        model=model,
        questions=_JevQuestions(
            next_action=_JevQuestion(
                type="choice", instructions=QUESTION_INSTRUCTIONS, criteria=criteria
            )
        ),
    )
    return request.model_dump_json().encode("utf-8")


def _response(
    response: JevHttpResponse, expected_model: str, criteria: dict[str, str]
) -> _JevResponse:
    if not 200 <= response.status_code < 300:
        raise JevHttpStatusError(response.status_code)
    try:
        parsed = _JevResponse.model_validate_json(response.content)
    except ValidationError:
        raise JevResponseError("invalid Jev response") from None
    if not parsed.model.strip() or set(parsed.answers) != {QUESTION_ID}:
        raise JevResponseError("invalid Jev response")
    if parsed.model != expected_model:
        raise JevModelMismatchError(expected_model)
    answer = parsed.answers[QUESTION_ID]
    probabilities = answer.probabilities
    scores = (*probabilities.values(), answer.confidence)
    if (
        set(probabilities) != set(criteria)
        or not answer.choice.strip()
        or answer.choice not in criteria
        or any(not math.isfinite(score) or not 0 <= score <= 1 for score in scores)
        or abs(math.fsum(probabilities.values()) - 1.0) > _PROBABILITY_TOLERANCE
        or probabilities[answer.choice] + _PROBABILITY_TOLERANCE < max(probabilities.values())
    ):
        raise JevResponseError("invalid Jev response")
    return parsed


class HttpxJevTransport:
    __slots__ = ("_client", "_settings")

    def __init__(self, client: httpx.Client, settings: JevHttpSettings) -> None:
        self._client = client
        self._settings = settings

    def post(self, body: bytes) -> JevHttpResponse:
        try:
            response = self._client.post(
                self._settings.endpoint,
                content=body,
                headers={
                    "Authorization": f"Bearer {self._settings.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=self._settings.timeout_seconds,
            )
        except httpx.RequestError:
            raise JevTransportError("Jev transport request failed") from None
        return JevHttpResponse(response.status_code, response.content)


class JevDecisionModel:
    __slots__ = ("_model", "_transport")

    def __init__(self, transport: JevTransport, model: str = JEV_MODEL) -> None:
        if not model.strip():
            raise JevRequestError("Jev model must be nonblank")
        self._transport = transport
        self._model = model

    def decide(self, frame: DecisionFrame) -> DecisionResult:
        criteria = _criteria(frame)
        response = _response(
            self._transport.post(_request_body(frame, self._model, criteria)),
            self._model,
            criteria,
        )
        answer = response.answers[QUESTION_ID]
        if answer.choice == ABSTAIN_ACTION_ID:
            return AbstainedDecision(
                model_id=response.model,
                confidence=answer.confidence,
                reason="Jev selected the reserved abstain option",
            )
        return SelectedActionDecision(
            model_id=response.model,
            action_id=answer.choice,
            probability=answer.probabilities[answer.choice],
            confidence=answer.confidence,
        )
