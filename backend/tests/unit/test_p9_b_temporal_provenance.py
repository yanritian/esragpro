from collections.abc import Iterator
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import world_memory_plane as wmp
from app.memory_core.world_memory import (
    Hypothesis,
    HypothesisStatus,
    ValidityInterval,
    VerifiedFact,
    WorldObservation,
    WorldState,
)
from app.memory_core.world_memory_serializer import (
    EVENT_FACT_VERIFIED,
    EVENT_HYPOTHESIS_VERIFIED,
    EVENT_OBSERVATION_INGESTED,
    EVENT_STATE_FUSED,
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


def test_scenario_context_when_observation_has_dual_time_preserves_temporal_trace(
    client: TestClient,
) -> None:
    observed_at = datetime(2026, 8, 2, 3, 30, tzinfo=timezone.utc)
    ingested_at = datetime(2026, 8, 2, 3, 30, 2, tzinfo=timezone.utc)
    observation = WorldObservation(
        id=uuid4(),
        content="Flood depth sensor reports 1.2m",
        observed_at=observed_at,
        ingested_at=ingested_at,
        source="flood-gauge-7",
        raw_evidence_ref="minio://rescue/flood-gauge-7/0300.json",
    )
    envelope = build_world_memory_envelope(
        observation,
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="test",
    )

    remember = client.post("/world-memory/remember", json={"envelope": envelope})
    assert remember.status_code == 201, remember.text

    response = client.post(
        "/world-memory/scenario-context",
        json={"scenario": "rescue"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    trace = body["items"][0]["temporal"]
    snapshot = envelope["world_memory_snapshot"]
    assert datetime.fromisoformat(trace["event_occurred_at"]) == datetime.fromisoformat(
        envelope["occurred_at"]
    )
    assert datetime.fromisoformat(trace["observed_at"]) == datetime.fromisoformat(
        snapshot["observed_at"]
    )
    assert datetime.fromisoformat(trace["ingested_at"]) == datetime.fromisoformat(
        snapshot["ingested_at"]
    )
    assert trace["validity_start"] is None
    assert trace["validity_end"] is None
    assert trace["created_at"] is None
    assert trace["verified_at"] is None


def test_scenario_context_when_state_has_validity_preserves_interval_without_fabrication(
    client: TestClient,
) -> None:
    validity_start = datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc)
    validity_end = datetime(2026, 8, 2, 4, 5, tzinfo=timezone.utc)
    state = WorldState(
        id=uuid4(),
        content="Bridge B is impassable",
        observation_ids=(uuid4(),),
        validity=ValidityInterval(start=validity_start, end=validity_end),
    )
    envelope = build_world_memory_envelope(
        state,
        event_type=EVENT_STATE_FUSED,
        projection_target="test",
    )

    remember = client.post("/world-memory/remember", json={"envelope": envelope})
    assert remember.status_code == 201, remember.text

    response = client.post(
        "/world-memory/scenario-context",
        json={"scenario": "autonomous_driving"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    trace = body["items"][0]["temporal"]
    snapshot = envelope["world_memory_snapshot"]
    assert datetime.fromisoformat(trace["event_occurred_at"]) == datetime.fromisoformat(
        envelope["occurred_at"]
    )
    assert trace["observed_at"] is None
    assert trace["ingested_at"] is None
    assert datetime.fromisoformat(trace["validity_start"]) == datetime.fromisoformat(
        snapshot["validity"]["start"]
    )
    assert datetime.fromisoformat(trace["validity_end"]) == datetime.fromisoformat(
        snapshot["validity"]["end"]
    )
    assert trace["created_at"] is None
    assert trace["verified_at"] is None


def test_query_preserves_hypothesis_and_fact_source_times(client: TestClient) -> None:
    created_at = datetime(2026, 8, 2, 4, 10, tzinfo=timezone.utc)
    verified_at = datetime(2026, 8, 2, 4, 12, tzinfo=timezone.utc)
    hypothesis = Hypothesis(
        id=uuid4(),
        content="Bridge B remains impassable",
        observation_ids=(uuid4(),),
        status=HypothesisStatus.VERIFIED,
        evidence_ids=(uuid4(),),
        created_at=created_at,
    )
    fact = VerifiedFact(
        id=uuid4(),
        content="Route C is the available evacuation route",
        hypothesis_id=hypothesis.id,
        evidence_ids=(uuid4(),),
        verified_at=verified_at,
    )

    for item, event_type in (
        (hypothesis, EVENT_HYPOTHESIS_VERIFIED),
        (fact, EVENT_FACT_VERIFIED),
    ):
        envelope = build_world_memory_envelope(
            item,
            event_type=event_type,
            projection_target="test",
        )
        remember = client.post("/world-memory/remember", json={"envelope": envelope})
        assert remember.status_code == 201, remember.text

    response = client.post(
        "/world-memory/query",
        json={"require_evidence": True},
    )

    assert response.status_code == 200, response.text
    items_by_kind = {item["kind"]: item for item in response.json()["items"]}
    hypothesis_trace = items_by_kind["hypothesis"]["temporal"]
    fact_trace = items_by_kind["fact"]["temporal"]

    assert datetime.fromisoformat(hypothesis_trace["event_occurred_at"]) == created_at
    assert datetime.fromisoformat(hypothesis_trace["created_at"]) == created_at
    assert hypothesis_trace["observed_at"] is None
    assert hypothesis_trace["ingested_at"] is None
    assert hypothesis_trace["validity_start"] is None
    assert hypothesis_trace["validity_end"] is None
    assert hypothesis_trace["verified_at"] is None

    assert datetime.fromisoformat(fact_trace["event_occurred_at"]) == verified_at
    assert datetime.fromisoformat(fact_trace["verified_at"]) == verified_at
    assert fact_trace["observed_at"] is None
    assert fact_trace["ingested_at"] is None
    assert fact_trace["validity_start"] is None
    assert fact_trace["validity_end"] is None
    assert fact_trace["created_at"] is None
