from __future__ import annotations

import json
from datetime import datetime
from typing import Final, TypeAlias
from uuid import uuid4

import httpx
import pytest

from app.memory_core.decision_contracts import (
    AbstainedDecision,
    ActionRisk,
    CandidateAction,
    DecisionFrame,
    DecisionRoute,
    SelectedActionDecision,
)
from app.memory_core.decision_router import DecisionRoutePolicy, DecisionRouter
from app.memory_core.slow_llm_decision_model import (
    OpenAICompatibleDecisionTransport,
    SlowLlmDecisionModel,
    SlowLlmDecisionSettings,
    SlowLlmHttpResponse,
    SlowLlmHttpSettings,
    SlowLlmHttpStatusError,
    SlowLlmRequestError,
    SlowLlmResponseError,
    SlowLlmTransportError,
)

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
_MODEL: Final[str] = "slow-test-model"


def _frame() -> DecisionFrame:
    return DecisionFrame(
        frame_id=uuid4(),
        query_id=uuid4(),
        goal="choose a bounded next step",
        as_of=datetime(2026, 9, 29, 12, 0, 0, 123456),
        asserted_state=(), observations=(), uncertainties=(),
        legal_actions=(
            CandidateAction("inspect", "inspect the evidence", ActionRisk.LOW),
            CandidateAction("archive", "archive the evidence", ActionRisk.MEDIUM),
        ),
        evidence_ids=(),
    )


def _response_content(inner: dict[str, JsonValue], model: str = _MODEL) -> bytes:
    return json.dumps(
        {
            "model": model,
            "choices": [{"index": 0, "message": {"content": json.dumps(inner)}, "finish_reason": "stop"}],
        }
    ).encode()


class FakeTransport:
    __slots__ = ("calls", "response", "bodies")

    def __init__(self, response: SlowLlmHttpResponse | None = None) -> None:
        self.calls = 0
        self.response = response
        self.bodies: list[bytes] = []

    def post(self, body: bytes) -> SlowLlmHttpResponse:
        self.calls += 1
        self.bodies.append(body)
        assert self.response is not None
        return self.response


def _model(inner: dict[str, JsonValue]) -> tuple[SlowLlmDecisionModel, FakeTransport]:
    transport = FakeTransport(SlowLlmHttpResponse(200, _response_content(inner)))
    return SlowLlmDecisionModel(transport, SlowLlmDecisionSettings(_MODEL)), transport


def test_legal_selection_maps_to_typed_result() -> None:
    adapter, transport = _model({"decision": "select", "action_id": "inspect", "probability": 0.75, "confidence": 0.8})

    result = adapter.decide(_frame())

    assert result == SelectedActionDecision(_MODEL, "inspect", 0.75, 0.8)
    assert transport.calls == 1


def test_abstention_maps_to_typed_result() -> None:
    adapter, transport = _model({"decision": "abstain", "confidence": 0.2, "reason": "evidence is insufficient"})

    result = adapter.decide(_frame())

    assert result == AbstainedDecision(_MODEL, 0.2, "evidence is insufficient")
    assert transport.calls == 1


def test_request_is_deterministic_and_transport_is_single_call() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_response_content({"decision": "select", "action_id": "inspect", "probability": 0.75, "confidence": 0.8}),
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        frame = _frame()
        settings = SlowLlmHttpSettings("secret-key", "https://provider.test/v1")
        adapter = SlowLlmDecisionModel(
            OpenAICompatibleDecisionTransport(client, settings),
            SlowLlmDecisionSettings(_MODEL),
        )
        _ = adapter.decide(frame)
        _ = adapter.decide(frame)
        assert len(requests) == 2
        assert all(request.method == "POST" for request in requests)
        assert all(str(request.url) == "https://provider.test/v1/chat/completions" for request in requests)
        assert requests[0].headers["authorization"] == "Bearer secret-key"
        assert requests[0].headers["content-type"] == "application/json"
        assert requests[0].headers["accept"] == "application/json"
        assert requests[0].content == requests[1].content
        body = json.loads(requests[0].content)
        assert set(body) == {"model", "messages", "response_format"}
        assert body["model"] == _MODEL
        assert body["response_format"] == {"type": "json_object"}
        assert len(body["messages"]) == 2
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][1]["content"].startswith("DECISION_STATE_JSON\n")
        assert not client.is_closed
    finally:
        client.close()


def test_http_status_failure_is_typed_and_secret_safe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"provider secret body", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = SlowLlmDecisionModel(
            OpenAICompatibleDecisionTransport(
                client,
                SlowLlmHttpSettings("secret-key", "https://provider.test"),
            ),
            SlowLlmDecisionSettings(_MODEL),
        )
        with pytest.raises(SlowLlmHttpStatusError) as caught:
            adapter.decide(_frame())

    assert caught.value.status_code == 429
    assert "secret-key" not in str(caught.value)
    assert "provider secret body" not in str(caught.value)


def test_transport_failure_is_typed_and_single_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("transport failed", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = SlowLlmDecisionModel(
            OpenAICompatibleDecisionTransport(
                client,
                SlowLlmHttpSettings("secret-key", "https://provider.test"),
            ),
            SlowLlmDecisionSettings(_MODEL),
        )
        with pytest.raises(SlowLlmTransportError):
            adapter.decide(_frame())

    assert calls == 1


@pytest.mark.parametrize(
    "inner",
    [
        {"decision": "select", "action_id": "missing", "probability": 0.5, "confidence": 0.5},
        {"decision": "select", "action_id": "inspect", "probability": 1.1, "confidence": 0.5},
        {"decision": "select", "action_id": "inspect", "probability": 0.5, "confidence": -0.1},
        {"decision": "select", "action_id": "inspect", "probability": "0.5", "confidence": 0.5},
        {"decision": "select", "action_id": "inspect", "probability": 0.5, "confidence": 0.5, "extra": True},
        {"decision": "abstain", "confidence": 0.5, "reason": ""},
        {"decision": "other", "confidence": 0.5, "reason": "unknown"},
    ],
)
def test_invalid_inner_decisions_fail_closed(inner: dict[str, JsonValue]) -> None:
    adapter, transport = _model(inner)

    with pytest.raises(SlowLlmResponseError):
        adapter.decide(_frame())

    assert transport.calls == 1


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"model": "", "choices": []}, 200),
        ({"model": _MODEL, "choices": []}, 200),
        ({"model": _MODEL, "choices": [{"index": 1}]}, 200),
        ({"model": _MODEL, "choices": [{"index": 0, "message": {"content": ""}, "finish_reason": "stop"}]}, 200),
        ({"model": _MODEL, "choices": [{"index": 0, "message": {"content": "{}"}, "finish_reason": "length"}]}, 200),
        ({"model": _MODEL, "choices": [{"index": 0, "message": {"content": "{"}, "finish_reason": "stop"}]}, 200),
    ],
)
def test_invalid_provider_envelopes_fail_closed(payload: dict[str, JsonValue], status: int) -> None:
    transport = FakeTransport(SlowLlmHttpResponse(status, json.dumps(payload).encode()))
    adapter = SlowLlmDecisionModel(transport, SlowLlmDecisionSettings(_MODEL))

    error_type = SlowLlmHttpStatusError if status != 200 else SlowLlmResponseError
    with pytest.raises(error_type):
        adapter.decide(_frame())

    assert transport.calls == 1


def test_request_settings_and_invalid_url_fail_as_typed_request_errors() -> None:
    with pytest.raises(SlowLlmRequestError):
        _ = SlowLlmHttpSettings(" ", "https://provider.test")
    with pytest.raises(SlowLlmRequestError):
        _ = SlowLlmHttpSettings("secret", " ")
    with pytest.raises(SlowLlmRequestError):
        _ = SlowLlmHttpSettings("secret", "https://provider.test", 0)

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
        adapter = SlowLlmDecisionModel(
            OpenAICompatibleDecisionTransport(
                client,
                SlowLlmHttpSettings("secret", "not a URL"),
            ),
            SlowLlmDecisionSettings(_MODEL),
        )
        with pytest.raises(SlowLlmRequestError):
            adapter.decide(_frame())


def test_router_composition_calls_slow_adapter_once_without_execution() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_response_content({"decision": "select", "action_id": "inspect", "probability": 0.8, "confidence": 0.7}),
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        slow = SlowLlmDecisionModel(
            OpenAICompatibleDecisionTransport(
                client,
                SlowLlmHttpSettings("secret", "https://provider.test"),
            ),
            SlowLlmDecisionSettings(_MODEL),
        )
        fast = FakeTransport(SlowLlmHttpResponse(200, _response_content(
            {"decision": "abstain", "confidence": 0.2, "reason": "uncertain"}, model="fast-model"
        )))
        routed = DecisionRouter(
            DecisionRoutePolicy(fast_confidence_threshold=0.8, slow_confidence_threshold=0.7)
        ).route(
            _frame(),
            fast_model=SlowLlmDecisionModel(fast, SlowLlmDecisionSettings("fast-model")),
            slow_model=slow,
        )

    assert routed.route is DecisionRoute.SLOW_MODEL
    assert isinstance(routed.result, SelectedActionDecision)
    assert len(requests) == 1
