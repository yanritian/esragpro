from collections.abc import Iterator
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import world_memory_plane as wmp
from app.memory_core.repositories.world_memory_authority import StoredEnvelope
from app.memory_core.world_memory import WorldObservation
from app.memory_core.world_memory_serializer import (
    EVENT_OBSERVATION_INGESTED,
    build_world_memory_envelope,
)


@pytest.fixture(autouse=True)
def clear_store() -> Iterator[None]:
    wmp._get_store().clear()
    yield
    wmp._get_store().clear()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(wmp.router)
    return TestClient(app)


def test_query_event_time_lte_includes_the_boundary_and_excludes_later_events(
    client: TestClient,
) -> None:
    older_event_time = datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)
    older_envelope = build_world_memory_envelope(
        WorldObservation(
            id=uuid4(),
            content="Historic road closure",
            observed_at=older_event_time,
            ingested_at=older_event_time,
            source="rescue-sensor",
            raw_evidence_ref="sensor://road-closure/0900",
        ),
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="test",
    )
    newer_event_time = datetime(2026, 8, 2, 9, 5, tzinfo=timezone.utc)
    newer_envelope = build_world_memory_envelope(
        WorldObservation(
            id=uuid4(),
            content="Later road clearance",
            observed_at=newer_event_time,
            ingested_at=newer_event_time,
            source="rescue-sensor",
            raw_evidence_ref="sensor://road-closure/0905",
        ),
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="test",
    )

    for envelope in (older_envelope, newer_envelope):
        remember = client.post("/world-memory/remember", json={"envelope": envelope})
        assert remember.status_code == 201, remember.text

    response = client.post(
        "/world-memory/query",
        json={
            "require_evidence": True,
            "event_time_lte": older_envelope["occurred_at"],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert [item["content"] for item in body["items"]] == ["Historic road closure"]
    assert body["envelopes"] == [older_envelope]


def test_query_event_time_lte_refuses_when_only_cutoff_event_lacks_evidence(
    client: TestClient,
) -> None:
    cutoff_time = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    unverified_envelope = build_world_memory_envelope(
        WorldObservation(
            id=uuid4(),
            content="Unverified bridge status",
            observed_at=cutoff_time,
            ingested_at=cutoff_time,
            source="rescue-sensor",
            raw_evidence_ref=None,
        ),
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="test",
    )
    later_envelope = build_world_memory_envelope(
        WorldObservation(
            id=uuid4(),
            content="Verified later bridge status",
            observed_at=datetime(2026, 8, 2, 10, 5, tzinfo=timezone.utc),
            ingested_at=datetime(2026, 8, 2, 10, 5, tzinfo=timezone.utc),
            source="rescue-sensor",
            raw_evidence_ref="sensor://bridge/1005",
        ),
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="test",
    )

    for envelope in (unverified_envelope, later_envelope):
        remember = client.post("/world-memory/remember", json={"envelope": envelope})
        assert remember.status_code == 201, remember.text

    response = client.post(
        "/world-memory/query",
        json={
            "require_evidence": True,
            "event_time_lte": unverified_envelope["occurred_at"],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "refused"
    assert response.json()["refusal_reason"] == "insufficient_evidence"
    assert response.json()["items"] == []
    assert response.json()["envelopes"] == []


def test_query_refuses_historical_text_when_the_in_memory_seam_has_no_authority_corpus(
    client: TestClient,
) -> None:
    response = client.post(
        "/world-memory/query",
        json={
            "query": "road closure",
            "event_time_lte": "2026-08-02T10:00:00Z",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "refused"
    assert body["refusal_reason"] == "event_history_authority_unavailable"
    assert body["items"] == []
    assert body["envelopes"] == []
    assert body["candidate_sources"] == [
        {
            "source": "elasticsearch_event_history",
            "status": "refused",
            "candidate_count": 0,
            "latency_ms": 0.0,
            "error": "event_history_authority_unavailable",
        }
    ]


def test_query_returns_authority_rehydrated_historical_text_after_the_gate_passes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occurred_at = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    envelope = build_world_memory_envelope(
        WorldObservation(
            id=uuid4(),
            content="Historic road closure",
            observed_at=occurred_at,
            ingested_at=occurred_at,
            source="rescue-sensor",
            raw_evidence_ref="sensor://road-closure/1000",
        ),
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="elasticsearch",
    )
    stored = StoredEnvelope(
        id=uuid4(),
        event_type=envelope["event_type"],
        schema_version=envelope["schema_version"],
        aggregate_id=uuid4(),
        projection_target=envelope["projection_target"],
        idempotency_key=envelope["idempotency_key"],
        occurred_at=occurred_at,
        kind="observation",
        envelope=envelope,
        created_at=occurred_at,
    )

    async def event_history_query(**kwargs):
        assert kwargs["query"] == "road closure"
        assert kwargs["event_time_lte"] == occurred_at
        return (
            [stored],
            [
                wmp.WorldMemorySourceTrace(
                    source="elasticsearch_event_history",
                    status="ok",
                    candidate_count=1,
                    latency_ms=4.0,
                )
            ],
            5.0,
            None,
        )

    monkeypatch.setattr(wmp, "_is_pg_authority_configured", lambda: True)
    monkeypatch.setattr(wmp, "_pg_event_history_query", event_history_query)

    response = client.post(
        "/world-memory/query",
        json={
            "query": "road closure",
            "event_time_lte": occurred_at.isoformat(),
            "require_evidence": True,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert [item["content"] for item in body["items"]] == ["Historic road closure"]
    assert body["envelopes"] == [envelope]
    assert body["candidate_sources"][0]["source"] == "elasticsearch_event_history"


def test_query_refuses_historical_text_when_all_ranked_candidates_fail_the_evidence_gate(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def event_history_query(**kwargs):
        assert kwargs["require_evidence"] is True
        return (
            [],
            [
                wmp.WorldMemorySourceTrace(
                    source="elasticsearch_event_history",
                    status="refused",
                    candidate_count=1,
                    latency_ms=2.0,
                    error="insufficient_evidence",
                )
            ],
            3.0,
            "insufficient_evidence",
        )

    monkeypatch.setattr(wmp, "_is_pg_authority_configured", lambda: True)
    monkeypatch.setattr(wmp, "_pg_event_history_query", event_history_query)

    response = client.post(
        "/world-memory/query",
        json={
            "query": "road closure",
            "event_time_lte": "2026-08-02T10:00:00Z",
            "require_evidence": True,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "refused"
    assert body["refusal_reason"] == "insufficient_evidence"
    assert body["items"] == []
    assert body["envelopes"] == []


def test_scenario_context_forwards_event_time_lte_and_keeps_evidence_fail_closed(
    client: TestClient,
) -> None:
    cutoff_time = datetime(2026, 8, 2, 11, 0, tzinfo=timezone.utc)
    unverified_envelope = build_world_memory_envelope(
        WorldObservation(
            id=uuid4(),
            content="Unverified evacuation route",
            observed_at=cutoff_time,
            ingested_at=cutoff_time,
            source="rescue-sensor",
            raw_evidence_ref=None,
        ),
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="test",
    )
    later_envelope = build_world_memory_envelope(
        WorldObservation(
            id=uuid4(),
            content="Verified later evacuation route",
            observed_at=datetime(2026, 8, 2, 11, 5, tzinfo=timezone.utc),
            ingested_at=datetime(2026, 8, 2, 11, 5, tzinfo=timezone.utc),
            source="rescue-sensor",
            raw_evidence_ref="sensor://route/1105",
        ),
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="test",
    )

    for envelope in (unverified_envelope, later_envelope):
        remember = client.post("/world-memory/remember", json={"envelope": envelope})
        assert remember.status_code == 201, remember.text

    response = client.post(
        "/world-memory/scenario-context",
        json={
            "scenario": "rescue",
            "event_time_lte": unverified_envelope["occurred_at"],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "refused"
    assert response.json()["refusal_reason"] == "insufficient_evidence"
    assert response.json()["items"] == []
    assert response.json()["envelopes"] == []
