"""P9-D4 aggregate-scoped valid-time state snapshot (FastAPI contract).

The state-at operation is a separate read from event-history text search: it
resolves the authoritative ``WorldState`` envelope for one aggregate at a
requested valid-time instant using the P7 ``ValidityInterval`` model and the
``WorldState.supersedes`` links. It never consults ES/Neo4j candidates,
latest-object lookup, or an LLM.

These tests run against the in-memory reference seam (the same seam the
existing ``/world-memory/query`` contract uses when PostgreSQL is not
configured) plus the shared resolution helper
``resolve_state_at_candidates``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import world_memory_plane as wmp
from app.memory_core.repositories.world_memory_authority import (
    StoredEnvelope,
    resolve_state_at_candidates,
)
from app.memory_core.world_memory import ValidityInterval, WorldState
from app.memory_core.world_memory_serializer import (
    EVENT_STATE_FUSED,
    build_world_memory_envelope,
)

T10 = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
T11 = datetime(2026, 8, 2, 11, 0, tzinfo=timezone.utc)
T1130 = datetime(2026, 8, 2, 11, 30, tzinfo=timezone.utc)
T12 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
T13 = datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc)
T15 = datetime(2026, 8, 2, 15, 0, tzinfo=timezone.utc)


def make_state_envelope(
    *,
    aggregate_id: UUID,
    content: str,
    validity_start: datetime,
    validity_end: datetime | None = None,
    observation_ids: tuple[UUID, ...] = (),
    supersedes: tuple[UUID, ...] = (),
    state_id: UUID | None = None,
) -> dict[str, Any]:
    """Build a valid P7-B state envelope scoped to *aggregate_id*."""
    state = WorldState(
        id=state_id or uuid4(),
        content=content,
        observation_ids=observation_ids,
        validity=ValidityInterval(start=validity_start, end=validity_end),
        supersedes=supersedes,
    )
    envelope = build_world_memory_envelope(
        state,
        event_type=EVENT_STATE_FUSED,
        projection_target="test",
    )
    envelope["aggregate_id"] = str(aggregate_id)
    return envelope


def stored_for(envelope: dict[str, Any]) -> StoredEnvelope:
    """Wrap a state envelope in an authority :class:`StoredEnvelope`."""
    return StoredEnvelope(
        id=uuid4(),
        event_type=envelope["event_type"],
        schema_version=envelope["schema_version"],
        aggregate_id=UUID(envelope["aggregate_id"]),
        projection_target=envelope["projection_target"],
        idempotency_key=envelope["idempotency_key"],
        occurred_at=datetime.fromisoformat(envelope["occurred_at"]),
        kind="state",
        envelope=envelope,
        created_at=datetime.now(timezone.utc),
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


# ============================================================================
# Validity boundaries (start inclusive, end exclusive, open-ended)
# ============================================================================


def test_state_at_start_is_inclusive_end_is_exclusive_and_open_ended_match(
    client: TestClient,
) -> None:
    aggregate_id = uuid4()
    closed = make_state_envelope(
        aggregate_id=aggregate_id,
        content="closed state",
        validity_start=T10,
        validity_end=T11,
        observation_ids=(uuid4(),),
    )
    open_ended = make_state_envelope(
        aggregate_id=aggregate_id,
        content="open state",
        validity_start=T12,
        validity_end=None,
        observation_ids=(uuid4(),),
    )
    for envelope in (closed, open_ended):
        remember = client.post("/world-memory/remember", json={"envelope": envelope})
        assert remember.status_code == 201, remember.text

    # Start is inclusive: 10:00 lies inside [10:00, 11:00).
    at_start = client.post(
        "/world-memory/state-at",
        json={"aggregate_id": str(aggregate_id), "valid_at": "2026-08-02T10:00:00Z"},
    )
    assert at_start.status_code == 200, at_start.text
    body = at_start.json()
    assert body["status"] == "ok"
    assert body["items"][0]["content"] == "closed state"
    assert body["envelopes"] == [closed]

    # End is exclusive: 11:00 is NOT inside [10:00, 11:00); the open state
    # starts at 12:00, so no state matches.
    at_end = client.post(
        "/world-memory/state-at",
        json={"aggregate_id": str(aggregate_id), "valid_at": "2026-08-02T11:00:00Z"},
    )
    assert at_end.status_code == 200, at_end.text
    assert at_end.json()["status"] == "no_match"

    # Open-ended interval matches any later instant.
    far_future = client.post(
        "/world-memory/state-at",
        json={"aggregate_id": str(aggregate_id), "valid_at": "2026-08-02T15:00:00Z"},
    )
    assert far_future.status_code == 200, far_future.text
    body = far_future.json()
    assert body["status"] == "ok"
    assert body["items"][0]["content"] == "open state"
    assert body["envelopes"] == [open_ended]


def test_state_at_naive_timestamp_and_malformed_uuid_fail_closed(
    client: TestClient,
) -> None:
    aggregate_id = uuid4()

    # Naive timestamp has no timezone -> rejected at the API boundary.
    naive = client.post(
        "/world-memory/state-at",
        json={"aggregate_id": str(aggregate_id), "valid_at": "2026-08-02T10:00:00"},
    )
    assert naive.status_code == 422, naive.text

    # Malformed UUID -> rejected at the API boundary.
    malformed = client.post(
        "/world-memory/state-at",
        json={"aggregate_id": "not-a-uuid", "valid_at": "2026-08-02T10:00:00Z"},
    )
    assert malformed.status_code == 422, malformed.text

    # Missing valid_at -> rejected at the API boundary.
    missing = client.post(
        "/world-memory/state-at",
        json={"aggregate_id": str(aggregate_id)},
    )
    assert missing.status_code == 422, missing.text


# ============================================================================
# Aggregate isolation
# ============================================================================


def test_state_at_aggregate_isolation(client: TestClient) -> None:
    aggregate_a = uuid4()
    aggregate_b = uuid4()
    state_a = make_state_envelope(
        aggregate_id=aggregate_a,
        content="state A",
        validity_start=T10,
        validity_end=T12,
        observation_ids=(uuid4(),),
    )
    state_b = make_state_envelope(
        aggregate_id=aggregate_b,
        content="state B",
        validity_start=T10,
        validity_end=T12,
        observation_ids=(uuid4(),),
    )
    for envelope in (state_a, state_b):
        remember = client.post("/world-memory/remember", json={"envelope": envelope})
        assert remember.status_code == 201, remember.text

    response = client.post(
        "/world-memory/state-at",
        json={"aggregate_id": str(aggregate_a), "valid_at": "2026-08-02T10:30:00Z"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert [item["content"] for item in body["items"]] == ["state A"]
    assert body["envelopes"] == [state_a]


# ============================================================================
# Exact authority semantics: single match / no-match
# ============================================================================


def test_state_at_single_match_returns_exact_envelope_and_typed_item(
    client: TestClient,
) -> None:
    aggregate_id = uuid4()
    observation_id = uuid4()
    state_id = uuid4()
    envelope = make_state_envelope(
        aggregate_id=aggregate_id,
        state_id=state_id,
        content="Bridge B is impassable",
        validity_start=T10,
        validity_end=T12,
        observation_ids=(observation_id,),
    )
    remember = client.post("/world-memory/remember", json={"envelope": envelope})
    assert remember.status_code == 201, remember.text

    response = client.post(
        "/world-memory/state-at",
        json={"aggregate_id": str(aggregate_id), "valid_at": "2026-08-02T10:30:00Z"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["refusal_reason"] is None
    assert body["envelopes"] == [envelope]

    item = body["items"][0]
    assert item["kind"] == "state"
    assert item["id"] == str(state_id)
    assert item["content"] == "Bridge B is impassable"
    # States carry evidence indirectly through observation_ids.
    assert item["evidence_ids"] == [str(observation_id)]
    assert datetime.fromisoformat(item["temporal"]["validity_start"]) == T10
    assert datetime.fromisoformat(item["temporal"]["validity_end"]) == T12


def test_state_at_no_match_is_typed_and_empty(client: TestClient) -> None:
    response = client.post(
        "/world-memory/state-at",
        json={"aggregate_id": str(uuid4()), "valid_at": "2026-08-02T10:00:00Z"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "no_match"
    assert body["refusal_reason"] == "no_matching_items"
    assert body["items"] == []
    assert body["envelopes"] == []


# ============================================================================
# Conflict safety: superseded removal and ambiguity refusal
# ============================================================================


def test_state_at_superseded_overlapping_state_is_removed(client: TestClient) -> None:
    aggregate_id = uuid4()
    superseded_id = uuid4()
    superseding_id = uuid4()
    superseded = make_state_envelope(
        aggregate_id=aggregate_id,
        state_id=superseded_id,
        content="old state",
        validity_start=T10,
        validity_end=T12,
        observation_ids=(uuid4(),),
    )
    superseding = make_state_envelope(
        aggregate_id=aggregate_id,
        state_id=superseding_id,
        content="new state",
        validity_start=T11,
        validity_end=T13,
        observation_ids=(uuid4(),),
        supersedes=(superseded_id,),
    )
    for envelope in (superseded, superseding):
        remember = client.post("/world-memory/remember", json={"envelope": envelope})
        assert remember.status_code == 201, remember.text

    response = client.post(
        "/world-memory/state-at",
        json={"aggregate_id": str(aggregate_id), "valid_at": "2026-08-02T11:30:00Z"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert [item["content"] for item in body["items"]] == ["new state"]
    assert body["envelopes"] == [superseding]


def test_state_at_two_overlapping_non_superseded_states_are_ambiguous(
    client: TestClient,
) -> None:
    aggregate_id = uuid4()
    conflict_a = make_state_envelope(
        aggregate_id=aggregate_id,
        content="conflict A",
        validity_start=T10,
        validity_end=T12,
        observation_ids=(uuid4(),),
    )
    conflict_b = make_state_envelope(
        aggregate_id=aggregate_id,
        content="conflict B",
        validity_start=T11,
        validity_end=T13,
        observation_ids=(uuid4(),),
    )
    for envelope in (conflict_a, conflict_b):
        remember = client.post("/world-memory/remember", json={"envelope": envelope})
        assert remember.status_code == 201, remember.text

    response = client.post(
        "/world-memory/state-at",
        json={"aggregate_id": str(aggregate_id), "valid_at": "2026-08-02T11:30:00Z"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "refused"
    assert body["refusal_reason"] == "ambiguous_valid_time_state"
    assert body["items"] == []
    assert body["envelopes"] == []


# ============================================================================
# Evidence safety
# ============================================================================


def test_state_at_require_evidence_refuses_matching_state_without_evidence(
    client: TestClient,
) -> None:
    aggregate_id = uuid4()
    no_evidence = make_state_envelope(
        aggregate_id=aggregate_id,
        content="no evidence state",
        validity_start=T10,
        validity_end=T12,
        observation_ids=(),
    )
    remember = client.post("/world-memory/remember", json={"envelope": no_evidence})
    assert remember.status_code == 201, remember.text

    response = client.post(
        "/world-memory/state-at",
        json={
            "aggregate_id": str(aggregate_id),
            "valid_at": "2026-08-02T10:30:00Z",
            "require_evidence": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "refused"
    assert body["refusal_reason"] == "insufficient_evidence"
    assert body["items"] == []
    assert body["envelopes"] == []


def test_state_at_require_evidence_accepts_observation_backed_state(
    client: TestClient,
) -> None:
    aggregate_id = uuid4()
    evidenced = make_state_envelope(
        aggregate_id=aggregate_id,
        content="evidenced state",
        validity_start=T10,
        validity_end=T12,
        observation_ids=(uuid4(),),
    )
    remember = client.post("/world-memory/remember", json={"envelope": evidenced})
    assert remember.status_code == 201, remember.text

    response = client.post(
        "/world-memory/state-at",
        json={
            "aggregate_id": str(aggregate_id),
            "valid_at": "2026-08-02T10:30:00Z",
            "require_evidence": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["envelopes"] == [evidenced]


# ============================================================================
# Unconfigured-authority fail-closed behavior
# ============================================================================


def test_state_at_unconfigured_authority_fails_closed(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(**kwargs):
        raise RuntimeError("world_memory_plane: session factory unavailable after init")

    monkeypatch.setattr(wmp, "_is_pg_authority_configured", lambda: True)
    monkeypatch.setattr(wmp, "_pg_state_at", boom)

    response = client.post(
        "/world-memory/state-at",
        json={"aggregate_id": str(uuid4()), "valid_at": "2026-08-02T10:00:00Z"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "refused"
    assert body["refusal_reason"] == "state_at_authority_unavailable"
    assert body["items"] == []
    assert body["envelopes"] == []


# ============================================================================
# Shared resolution helper (authority repository seam)
# ============================================================================


def test_resolve_state_at_candidates_removes_superseded_state() -> None:
    aggregate_id = uuid4()
    superseded_id = uuid4()
    superseding_id = uuid4()
    superseded = make_state_envelope(
        aggregate_id=aggregate_id,
        state_id=superseded_id,
        content="old state",
        validity_start=T10,
        validity_end=T12,
        observation_ids=(uuid4(),),
    )
    superseding = make_state_envelope(
        aggregate_id=aggregate_id,
        state_id=superseding_id,
        content="new state",
        validity_start=T11,
        validity_end=T13,
        observation_ids=(uuid4(),),
        supersedes=(superseded_id,),
    )

    resolved = resolve_state_at_candidates(
        [stored_for(superseded), stored_for(superseding)],
        valid_at=T1130,
    )

    assert [r.envelope["world_memory_snapshot"]["content"] for r in resolved] == [
        "new state"
    ]


def test_resolve_state_at_candidates_keeps_conflicting_non_superseded_states() -> None:
    aggregate_id = uuid4()
    conflict_a = make_state_envelope(
        aggregate_id=aggregate_id,
        content="conflict A",
        validity_start=T10,
        validity_end=T12,
        observation_ids=(uuid4(),),
    )
    conflict_b = make_state_envelope(
        aggregate_id=aggregate_id,
        content="conflict B",
        validity_start=T11,
        validity_end=T13,
        observation_ids=(uuid4(),),
    )

    resolved = resolve_state_at_candidates(
        [stored_for(conflict_a), stored_for(conflict_b)],
        valid_at=T1130,
    )

    assert len(resolved) == 2


def test_resolve_state_at_candidates_skips_malformed_validity_data() -> None:
    aggregate_id = uuid4()
    malformed = make_state_envelope(
        aggregate_id=aggregate_id,
        content="malformed state",
        validity_start=T10,
        validity_end=T12,
        observation_ids=(uuid4(),),
    )
    malformed["world_memory_snapshot"]["validity"] = {
        "start": "not-a-timestamp",
        "end": None,
    }

    resolved = resolve_state_at_candidates(
        [stored_for(malformed)],
        valid_at=T1130,
    )

    assert resolved == []


def test_resolve_state_at_candidates_excludes_out_of_interval_states() -> None:
    aggregate_id = uuid4()
    before = make_state_envelope(
        aggregate_id=aggregate_id,
        content="before interval",
        validity_start=T10,
        validity_end=T11,
        observation_ids=(uuid4(),),
    )
    after = make_state_envelope(
        aggregate_id=aggregate_id,
        content="after interval",
        validity_start=T12,
        validity_end=T13,
        observation_ids=(uuid4(),),
    )

    resolved = resolve_state_at_candidates(
        [stored_for(before), stored_for(after)],
        valid_at=T15,
    )

    assert resolved == []
