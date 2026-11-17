from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import world_memory_plane as wmp
from app.memory_core.world_memory import WorldObservation
from app.memory_core.world_memory_serializer import (
    EVENT_OBSERVATION_INGESTED,
    build_world_memory_envelope,
)


@pytest.fixture(autouse=True)
def clear_store():
    wmp._get_store().clear()
    yield
    wmp._get_store().clear()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(wmp.router)
    return TestClient(app)


def _observation_without_evidence() -> dict[str, Any]:
    observation = WorldObservation(
        id=uuid4(),
        content="Unverified rescue context",
        observed_at=datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc),
        source="test-sensor",
        ingested_at=datetime(2026, 7, 31, 12, 0, 1, tzinfo=timezone.utc),
        raw_evidence_ref=None,
    )
    return build_world_memory_envelope(
        observation,
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="test",
    )


@pytest.mark.parametrize("scenario", ["rescue", "embodied", "autonomous_driving"])
def test_scenario_profiles_force_evidence_and_preserve_query_payload(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    raw_envelope = {"event_type": "world.observation.ingested", "raw": "unchanged"}
    query_response = wmp.WorldMemoryQueryResponse(
        query_id="query-id-is-not-exposed",
        items=[
            wmp.WorldMemoryContextItemResponse(
                kind="hypothesis",
                id="hypothesis-id",
                content="A typed hypothesis",
                evidence_ids=["evidence-id"],
                temporal=wmp.WorldMemoryTemporalTrace(
                    event_occurred_at=datetime(
                        2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc
                    ),
                    observed_at=None,
                    ingested_at=None,
                    validity_start=None,
                    validity_end=None,
                    created_at=None,
                    verified_at=None,
                ),
            )
        ],
        envelopes=[raw_envelope],
        token_budget=123,
        token_used=17,
        status="ok",
        refusal_reason=None,
        candidate_sources=[
            wmp.WorldMemorySourceTrace(
                source="elasticsearch",
                status="ok",
                candidate_count=2,
                latency_ms=4.5,
                error=None,
            )
        ],
        latency_ms=6.75,
    )
    captured: list[wmp.WorldMemoryQueryRequest] = []

    async def fake_world_memory_query(
        request: wmp.WorldMemoryQueryRequest,
    ) -> wmp.WorldMemoryQueryResponse:
        captured.append(request)
        return query_response

    monkeypatch.setattr(wmp, "world_memory_query", fake_world_memory_query)

    response = client.post(
        "/world-memory/scenario-context",
        json={
            "scenario": scenario,
            "query": "rescue context",
            "aggregate_id": None,
            "max_items": 12,
            "token_budget": 123,
        },
    )

    assert response.status_code == 200, response.text
    assert len(captured) == 1
    assert captured[0].query == "rescue context"
    assert captured[0].max_items == 12
    assert captured[0].token_budget == 123
    assert captured[0].require_evidence is True
    body = response.json()
    assert body == {
        "scenario": scenario,
        "passive": True,
        "read_only": True,
        "items": [
            {
                "kind": "hypothesis",
                "id": "hypothesis-id",
                "content": "A typed hypothesis",
                "evidence_ids": ["evidence-id"],
                "temporal": {
                    "event_occurred_at": "2026-07-31T12:00:00Z",
                    "observed_at": None,
                    "ingested_at": None,
                    "validity_start": None,
                    "validity_end": None,
                    "created_at": None,
                    "verified_at": None,
                },
            }
        ],
        "envelopes": [raw_envelope],
        "status": "ok",
        "refusal_reason": None,
        "candidate_sources": [
            {
                "source": "elasticsearch",
                "status": "ok",
                "candidate_count": 2,
                "latency_ms": 4.5,
                "error": None,
            }
        ],
        "latency_ms": 6.75,
    }
    assert set(body) == {
        "scenario",
        "passive",
        "read_only",
        "items",
        "envelopes",
        "status",
        "refusal_reason",
        "candidate_sources",
        "latency_ms",
    }


def test_scenario_context_preserves_evidence_refusal(client: TestClient) -> None:
    envelope = _observation_without_evidence()
    remember = client.post("/world-memory/remember", json={"envelope": envelope})
    assert remember.status_code == 201, remember.text

    response = client.post(
        "/world-memory/scenario-context",
        json={"scenario": "rescue"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "refused"
    assert response.json()["refusal_reason"] == "insufficient_evidence"
    assert response.json()["items"] == []
    assert response.json()["envelopes"] == []


def test_scenario_context_rejects_unknown_scenario(client: TestClient) -> None:
    response = client.post(
        "/world-memory/scenario-context",
        json={"scenario": "search_and_rescue"},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("extra_field", ["require_evidence", "unexpected"])
def test_scenario_context_rejects_unknown_request_fields(
    client: TestClient,
    extra_field: str,
) -> None:
    response = client.post(
        "/world-memory/scenario-context",
        json={"scenario": "rescue", extra_field: True},
    )

    assert response.status_code == 422
