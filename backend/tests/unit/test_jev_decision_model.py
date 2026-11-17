# noqa: SIZE_OK - the whitelist requires one consolidated adapter conformance matrix
from __future__ import annotations

import json
import math
from datetime import datetime
from typing import TypeAlias
from uuid import UUID

import httpx
import pytest

from app.memory_core.decision_contracts import (
    AbstainedDecision,
    ActionRisk,
    CandidateAction,
    DecisionFrame,
    DecisionRoute,
    SelectedActionDecision,
    StateAssertion,
    StateAssertionKind,
)
from app.memory_core.decision_router import DecisionRoutePolicy, DecisionRouter
from app.memory_core.world_memory import WorldContextItem, WorldContextItemKind
from app.memory_core.jev_decision_model import (
    ABSTAIN_ACTION_ID,
    JEV_MODEL,
    QUESTION_ID,
    QUESTION_INSTRUCTIONS,
    HttpxJevTransport,
    JevDecisionModel,
    JevHttpResponse,
    JevHttpSettings,
    JevHttpStatusError,
    JevModelMismatchError,
    JevRequestError,
    JevResponseError,
    JevTransport,
    JevTransportError,
)

JsonValue: TypeAlias = str | int | float | None | list["JsonValue"] | dict[str, "JsonValue"]


class FakeTransport:
    __slots__ = ("calls", "error", "response", "bodies")

    def __init__(
        self,
        response: JevHttpResponse | None = None,
        error: httpx.RequestError | None = None,
    ) -> None:
        self.calls = 0
        self.error = error
        self.response = response
        self.bodies: list[bytes] = []

    def post(self, body: bytes) -> JevHttpResponse:
        self.calls += 1
        self.bodies.append(body)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class ScenarioModel:
    __slots__ = ("calls", "result")

    def __init__(self, result: SelectedActionDecision) -> None:
        self.calls = 0
        self.result = result

    def decide(self, frame: DecisionFrame) -> SelectedActionDecision:
        del frame
        self.calls += 1
        return self.result


def _actions(count: int = 1) -> tuple[CandidateAction, ...]:
    if count == 1:
        return (CandidateAction("inspect", "inspect the evidence", ActionRisk.LOW),)
    return tuple(
        CandidateAction(f"action-{index}", f"take action {index}", ActionRisk.LOW)
        for index in range(count)
    )


def _frame(actions: tuple[CandidateAction, ...] | None = None) -> DecisionFrame:
    return DecisionFrame(
        frame_id="frame-1",
        query_id="query-1",
        goal="choose the safest next step",
        as_of=datetime(2026, 9, 29, 12, 0, 0, 123456),
        asserted_state=(),
        observations=(),
        uncertainties=(),
        legal_actions=_actions() if actions is None else actions,
        evidence_ids=(),
    )


def _payload(
    *,
    choice: str = "inspect",
    model: str = JEV_MODEL,
    probabilities: dict[str, JsonValue] | None = None,
    confidence: float = 0.9,
    usage: dict[str, int | None] | None = None,
) -> dict[str, JsonValue]:
    return {
        "model": model,
        "answers": {
            QUESTION_ID: {
                "type": "choice",
                "choice": choice,
                "probabilities": probabilities
                if probabilities is not None
                else {"inspect": 0.9, ABSTAIN_ACTION_ID: 0.1},
                "confidence": confidence,
            },
        },
        "usage": usage if usage is not None else {"input_tokens": 1, "output_tokens": 2},
    }


def _model_for(payload: dict[str, JsonValue]) -> tuple[JevDecisionModel, FakeTransport]:
    transport = FakeTransport(
        response=JevHttpResponse(200, json.dumps(payload, allow_nan=True).encode())
    )
    return JevDecisionModel(transport), transport


def test_selected_action_and_abstain_are_typed_results() -> None:
    selected, selected_transport = _model_for(_payload())
    selected_result = selected.decide(_frame())
    assert isinstance(selected_result, SelectedActionDecision)
    assert selected_result.action_id == "inspect"
    assert selected_result.probability == 0.9
    assert selected_transport.calls == 1

    abstained, abstain_transport = _model_for(
        _payload(
            choice=ABSTAIN_ACTION_ID,
            probabilities={"inspect": 0.2, ABSTAIN_ACTION_ID: 0.8},
            confidence=0.4,
        )
    )
    abstain_result = abstained.decide(_frame())
    assert isinstance(abstain_result, AbstainedDecision)
    assert abstain_result.reason == "Jev selected the reserved abstain option"
    assert abstain_result.confidence == 0.4
    assert abstain_transport.calls == 1


def test_request_state_and_criteria_are_deterministic_and_bounded() -> None:
    state_id = UUID("11111111-1111-1111-1111-111111111111")
    observation_id = UUID("22222222-2222-2222-2222-222222222222")
    evidence_id = UUID("33333333-3333-3333-3333-333333333333")
    frame = DecisionFrame(
        frame_id=state_id,
        query_id="query-1",
        goal="目标",
        as_of=datetime(2026, 9, 29, 12, 0, 0, 123456),
        asserted_state=(
            StateAssertion(
                item_id="assertion-1",
                kind=StateAssertionKind.FACT,
                content="a supported fact",
                evidence_ids=(evidence_id,),
            ),
        ),
        observations=(
            WorldContextItem(
                kind=WorldContextItemKind.OBSERVATION,
                id=observation_id,
                content="an observation",
                evidence_ids=(evidence_id,),
            ),
        ),
        uncertainties=(),
        legal_actions=(
            CandidateAction("inspect", "inspect the evidence", ActionRisk.LOW),
            CandidateAction("archive", "archive the evidence", ActionRisk.MEDIUM),
        ),
        evidence_ids=(evidence_id,),
    )
    adapter, transport = _model_for(
        _payload(
            probabilities={
                "inspect": 0.8,
                "archive": 0.1,
                ABSTAIN_ACTION_ID: 0.1,
            }
        )
    )

    _ = adapter.decide(frame)
    body = json.loads(transport.bodies[0])
    state = json.loads(body["state"])
    expected_state = {
        "frame_id": str(state_id),
        "query_id": "query-1",
        "goal": "目标",
        "as_of": "2026-09-29T12:00:00.123456Z",
        "asserted_state": [
            {
                "item_id": "assertion-1",
                "kind": "fact",
                "content": "a supported fact",
                "evidence_ids": [str(evidence_id)],
            },
        ],
        "observations": [
            {
                "item_id": str(observation_id),
                "kind": "observation",
                "content": "an observation",
                "evidence_ids": [str(evidence_id)],
            },
        ],
        "uncertainties": [],
        "legal_actions": [
            {"action_id": "inspect", "description": "inspect the evidence", "risk": "low"},
            {"action_id": "archive", "description": "archive the evidence", "risk": "medium"},
        ],
        "evidence_ids": [str(evidence_id)],
    }
    assert state == expected_state
    assert set(state) == {
        "frame_id",
        "query_id",
        "goal",
        "as_of",
        "asserted_state",
        "observations",
        "uncertainties",
        "legal_actions",
        "evidence_ids",
    }
    criteria = body["questions"][QUESTION_ID]["criteria"]
    assert list(criteria) == ["inspect", "archive", ABSTAIN_ACTION_ID]
    assert body["model"] == JEV_MODEL
    assert body["questions"][QUESTION_ID]["instructions"] == QUESTION_INSTRUCTIONS


def test_httpx_transport_posts_once_with_exact_adapter_headers_and_does_not_close_client() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=json.dumps(_payload()).encode(),
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        settings = JevHttpSettings(api_key="test-key")
        adapter = JevDecisionModel(HttpxJevTransport(client, settings))
        result = adapter.decide(_frame())
        assert isinstance(result, SelectedActionDecision)
        assert len(requests) == 1
        request = requests[0]
        assert request.method == "POST"
        assert str(request.url) == settings.endpoint
        assert request.headers["authorization"] == "Bearer test-key"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["accept"] == "application/json"
        assert not client.is_closed


@pytest.mark.parametrize("status_code", [400, 429, 500, 529])
def test_http_status_failures_are_typed_and_single_call(status_code: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, content=b"provider body", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = JevDecisionModel(HttpxJevTransport(client, JevHttpSettings("secret-key")))
        with pytest.raises(JevHttpStatusError) as caught:
            adapter.decide(_frame())
        assert caught.value.status_code == status_code
    assert calls == 1
    assert "secret-key" not in str(caught.value)
    assert "provider body" not in str(caught.value)


@pytest.mark.parametrize("exception_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_failures_are_typed_and_single_call(
    exception_type: type[httpx.RequestError],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise exception_type("transport failed", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = JevDecisionModel(HttpxJevTransport(client, JevHttpSettings("secret-key")))
        with pytest.raises(JevTransportError):
            adapter.decide(_frame())
    assert calls == 1


@pytest.mark.parametrize(
    "probabilities",
    [
        {"inspect": 0.8, ABSTAIN_ACTION_ID: 0.1},
        {"inspect": 0.4, ABSTAIN_ACTION_ID: 0.6},
        {"inspect": 0.8, ABSTAIN_ACTION_ID: 0.1, "unknown": 0.1},
        {"inspect": "0.8", ABSTAIN_ACTION_ID: 0.2},
        {"inspect": math.nan, ABSTAIN_ACTION_ID: 0.0},
    ],
)
def test_probability_distribution_and_choice_are_fail_closed(
    probabilities: dict[str, float],
) -> None:
    adapter, transport = _model_for(_payload(probabilities=probabilities))
    with pytest.raises(JevResponseError):
        adapter.decide(_frame())
    assert transport.calls == 1


@pytest.mark.parametrize(
    "payload",
    [
        _payload(choice="unknown"),
        _payload(model="other"),
        _payload(usage={"input_tokens": None, "output_tokens": 1}),
        {
            **_payload(),
            "answers": {
                QUESTION_ID: {
                    **_payload()["answers"][QUESTION_ID],
                    "extra": "forbidden",
                },
            },
        },
    ],
)
def test_response_schema_and_model_mismatch_fail_closed(payload: dict[str, JsonValue]) -> None:
    adapter, transport = _model_for(payload)
    error_type: type[JevResponseError | JevModelMismatchError] = (
        JevModelMismatchError if payload.get("model") == "other" else JevResponseError
    )
    with pytest.raises(error_type):
        adapter.decide(_frame())
    assert transport.calls == 1


def test_reserved_id_and_cardinality_fail_before_transport() -> None:
    collision = _frame((CandidateAction(ABSTAIN_ACTION_ID, "collision", ActionRisk.LOW),))
    collision_transport = FakeTransport(response=JevHttpResponse(200, b"{}"))
    with pytest.raises(JevRequestError):
        JevDecisionModel(collision_transport).decide(collision)
    assert collision_transport.calls == 0

    too_many = _frame(_actions(255))
    too_many_transport = FakeTransport(response=JevHttpResponse(200, b"{}"))
    with pytest.raises(JevRequestError):
        JevDecisionModel(too_many_transport).decide(too_many)
    assert too_many_transport.calls == 0

    boundary = _frame(_actions(254))
    boundary_transport = FakeTransport(
        response=JevHttpResponse(
            200,
            json.dumps(
                _payload(
                    choice="action-0",
                    probabilities={
                        "action-0": 1.0,
                        **{f"action-{index}": 0.0 for index in range(1, 254)},
                        ABSTAIN_ACTION_ID: 0.0,
                    },
                )
            ).encode(),
        )
    )
    _ = JevDecisionModel(boundary_transport).decide(boundary)
    request = json.loads(boundary_transport.bodies[0])
    assert len(request["questions"][QUESTION_ID]["criteria"]) == 255
    assert boundary_transport.calls == 1


def test_settings_validate_without_disclosing_secret() -> None:
    with pytest.raises(JevRequestError) as caught:
        JevHttpSettings(api_key=" ")
    assert "secret" not in str(caught.value)
    with pytest.raises(JevRequestError):
        JevHttpSettings(api_key="secret", timeout_seconds=0)
    with pytest.raises(JevRequestError):
        JevHttpSettings(api_key="secret", timeout_seconds=math.nan)


def test_adapter_composes_with_router_without_execution_side_effects() -> None:
    adapter, transport = _model_for(_payload())
    slow = ScenarioModel(SelectedActionDecision("slow", "inspect", 0.9, 0.9))
    routed = DecisionRouter(
        DecisionRoutePolicy(fast_confidence_threshold=0.8, slow_confidence_threshold=0.7)
    ).route(_frame(), fast_model=adapter, slow_model=slow)
    assert routed.route is DecisionRoute.FAST_MODEL
    assert isinstance(routed.result, SelectedActionDecision)
    assert transport.calls == 1
    assert slow.calls == 0


def test_protocol_is_satisfied_by_fake_transport() -> None:
    transport: JevTransport = FakeTransport(response=JevHttpResponse(200, b"{}"))
    assert transport is not None
