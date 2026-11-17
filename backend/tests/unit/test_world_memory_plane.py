"""
Focused contract tests for the P4-W world-memory cross-process contract.

Tests:
1. Write then read through separate client/backend boundaries
2. Round-trip preservation of kind, IDs, timestamps, evidence, envelope version
3. Refusal/empty response when query requires evidence but none is present
4. Malformed/unknown world-memory payload rejection
5. Idempotency: duplicate envelope is silently deduplicated
6. Conflict: same key, different payload is rejected
"""

from datetime import datetime, timezone
from uuid import uuid4, uuid5

import pytest
from fastapi.testclient import TestClient

from app.api import world_memory_plane as wmp
from app.api.world_memory_plane import (
    _get_store,
    router,
)
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
    KIND_FACT,
    KIND_OBSERVATION,
    KIND_STATE,
    P7_SCHEMA_VERSION,
    build_world_memory_envelope,
)


@pytest.fixture(autouse=True)
def clear_store():
    """Clear the reference store before and after each test."""
    _get_store().clear()
    yield
    _get_store().clear()


# Minimal FastAPI app for testing the router in isolation
@pytest.fixture
def client():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ============================================================================
# Helpers: build valid P7-B envelopes for each kind
# ============================================================================


def _observation_envelope(
    *,
    content: str = "Temperature is 23°C",
    evidence_ref: str = "doc-123",
    projection_target: str = "test",
):
    obs_id = uuid4()
    observed_at = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
    ingested_at = datetime(2026, 7, 31, 12, 0, 1, tzinfo=timezone.utc)
    obs = WorldObservation(
        id=obs_id,
        content=content,
        observed_at=observed_at,
        source="test-sensor",
        ingested_at=ingested_at,
        version=1,
        raw_evidence_ref=evidence_ref,
    )
    return build_world_memory_envelope(
        obs,
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target=projection_target,
    )


def _state_envelope(*, content: str = "Room is comfortable"):
    state_id = uuid4()
    obs_id = uuid4()
    state = WorldState(
        id=state_id,
        content=content,
        observation_ids=(obs_id,),
        validity=ValidityInterval(
            start=datetime(2026, 7, 31, 0, 0, 0, tzinfo=timezone.utc),
            end=None,
        ),
    )
    return build_world_memory_envelope(
        state,
        event_type=EVENT_STATE_FUSED,
        projection_target="test",
    )


def _hypothesis_envelope(*, content: str = "System is overheating"):
    hyp_id = uuid4()
    hyp = Hypothesis(
        id=hyp_id,
        content=content,
        observation_ids=(uuid4(),),
        status=HypothesisStatus.PENDING,
    )
    return build_world_memory_envelope(
        hyp,
        event_type=EVENT_HYPOTHESIS_VERIFIED,
        projection_target="test",
    )


def _fact_envelope(*, content: str = "System runs at 80°C"):
    fact_id = uuid4()
    hyp_id = uuid4()
    fact = VerifiedFact(
        id=fact_id,
        content=content,
        hypothesis_id=hyp_id,
        evidence_ids=(uuid4(),),
    )
    return build_world_memory_envelope(
        fact,
        event_type=EVENT_FACT_VERIFIED,
        projection_target="test",
    )


def _observation_without_evidence():
    obs_id = uuid4()
    obs = WorldObservation(
        id=obs_id,
        content="No evidence observation",
        observed_at=datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc),
        source="test-sensor",
        ingested_at=datetime(2026, 7, 31, 12, 0, 1, tzinfo=timezone.utc),
        raw_evidence_ref=None,  # no evidence
    )
    return build_world_memory_envelope(
        obs,
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="test",
    )


# ============================================================================
# Test 1: Write then read through separate boundaries
# ============================================================================


class TestWriteThenRead:
    """Write an envelope, then read it back through the query endpoint."""

    def test_observation_write_then_read(self, client):
        envelope = _observation_envelope()

        # Write
        resp = client.post("/world-memory/remember", json={"envelope": envelope})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["success"] is True
        assert body["kind"] == KIND_OBSERVATION

        # Read
        query_resp = client.post(
            "/world-memory/query",
            json={"kind": KIND_OBSERVATION, "max_items": 10},
        )
        assert query_resp.status_code == 200, query_resp.text
        qbody = query_resp.json()
        assert qbody["status"] == "ok"
        assert len(qbody["items"]) == 1
        assert qbody["items"][0]["kind"] == KIND_OBSERVATION
        assert qbody["items"][0]["content"] == "Temperature is 23°C"

    def test_multiple_kinds_write_then_read(self, client):
        # Store one of each kind
        obs_env = _observation_envelope(content="obs content")
        state_env = _state_envelope(content="state content")

        r1 = client.post("/world-memory/remember", json={"envelope": obs_env})
        r2 = client.post("/world-memory/remember", json={"envelope": state_env})
        assert r1.status_code == 201
        assert r2.status_code == 201

        # Query all — should return both
        q = client.post("/world-memory/query", json={})
        assert q.status_code == 200
        qbody = q.json()
        assert qbody["status"] == "ok"
        assert len(qbody["items"]) == 2
        kinds = {item["kind"] for item in qbody["items"]}
        assert KIND_OBSERVATION in kinds
        assert KIND_STATE in kinds

    def test_query_by_kind_filters(self, client):
        client.post("/world-memory/remember", json={"envelope": _observation_envelope()})
        client.post("/world-memory/remember", json={"envelope": _state_envelope()})

        # Query only observations
        q = client.post("/world-memory/query", json={"kind": KIND_OBSERVATION})
        qbody = q.json()
        assert len(qbody["items"]) == 1
        assert qbody["items"][0]["kind"] == KIND_OBSERVATION

        # Query only states
        q2 = client.post("/world-memory/query", json={"kind": KIND_STATE})
        qbody2 = q2.json()
        assert len(qbody2["items"]) == 1
        assert qbody2["items"][0]["kind"] == KIND_STATE


# ============================================================================
# Test 2: Round-trip preservation
# ============================================================================


class TestRoundTripPreservation:
    """Verify that kind, IDs, timestamps, evidence, and envelope fields survive."""

    def test_observation_round_trip_preserves_fields(self, client):
        envelope = _observation_envelope(content="Precise measurement")

        resp = client.post("/world-memory/remember", json={"envelope": envelope})
        assert resp.status_code == 201

        q = client.post("/world-memory/query", json={"kind": KIND_OBSERVATION})
        qbody = q.json()

        # Verify envelope round-trip
        assert len(qbody["envelopes"]) == 1
        returned_env = qbody["envelopes"][0]
        assert returned_env["event_type"] == EVENT_OBSERVATION_INGESTED
        assert returned_env["schema_version"] == P7_SCHEMA_VERSION
        assert returned_env["aggregate_type"] == "world_memory"
        assert returned_env["idempotency_key"] == envelope["idempotency_key"]

        # Verify snapshot round-trip
        snapshot = returned_env["world_memory_snapshot"]
        assert snapshot["_kind"] == KIND_OBSERVATION
        assert snapshot["content"] == "Precise measurement"
        assert snapshot["raw_evidence_ref"] == "doc-123"
        # Timestamps preserved as ISO-8601 strings
        assert "2026-07-31" in snapshot["observed_at"]
        assert "2026-07-31" in snapshot["ingested_at"]
        # UUID preserved
        assert snapshot["id"] == envelope["world_memory_snapshot"]["id"]

    def test_fact_round_trip_preserves_evidence_ids(self, client):
        envelope = _fact_envelope(content="Verified fact with evidence")

        resp = client.post("/world-memory/remember", json={"envelope": envelope})
        assert resp.status_code == 201

        q = client.post("/world-memory/query", json={"kind": KIND_FACT})
        qbody = q.json()
        assert qbody["status"] == "ok"

        # The context item should have evidence_ids
        item = qbody["items"][0]
        assert item["kind"] == KIND_FACT
        assert len(item["evidence_ids"]) > 0

        # The envelope should preserve evidence_ids in the snapshot
        snapshot = qbody["envelopes"][0]["world_memory_snapshot"]
        assert len(snapshot["evidence_ids"]) > 0

    def test_envelope_version_preserved(self, client):
        envelope = _observation_envelope()
        assert envelope["schema_version"] == P7_SCHEMA_VERSION

        client.post("/world-memory/remember", json={"envelope": envelope})

        q = client.post("/world-memory/query", json={})
        qbody = q.json()
        assert qbody["envelopes"][0]["schema_version"] == P7_SCHEMA_VERSION


# ============================================================================
# Test 3: Refusal when evidence required but missing
# ============================================================================


class TestEvidenceRefusal:
    """Fail-closed: when require_evidence=True and no evidence exists, refuse."""

    def test_empty_store_with_require_evidence_returns_no_match(self, client):
        q = client.post(
            "/world-memory/query",
            json={"require_evidence": True},
        )
        qbody = q.json()
        assert qbody["status"] == "no_match"
        assert qbody["refusal_reason"] == "no_matching_items"
        assert len(qbody["items"]) == 0

    def test_observation_without_evidence_refused(self, client):
        # Store an observation WITHOUT evidence
        env = _observation_without_evidence()
        resp = client.post("/world-memory/remember", json={"envelope": env})
        assert resp.status_code == 201

        # Query with require_evidence=True — should be refused
        q = client.post(
            "/world-memory/query",
            json={"require_evidence": True},
        )
        qbody = q.json()
        assert qbody["status"] == "refused"
        assert qbody["refusal_reason"] == "insufficient_evidence"
        assert len(qbody["items"]) == 0

    def test_observation_with_evidence_accepted(self, client):
        # Store an observation WITH evidence
        env = _observation_envelope(evidence_ref="doc-456")
        resp = client.post("/world-memory/remember", json={"envelope": env})
        assert resp.status_code == 201

        # Query with require_evidence=True — should succeed
        q = client.post(
            "/world-memory/query",
            json={"require_evidence": True},
        )
        qbody = q.json()
        assert qbody["status"] == "ok"
        assert len(qbody["items"]) == 1

    def test_mixed_evidence_only_returns_evidence_backed(self, client):
        # Store one with evidence, one without
        client.post("/world-memory/remember", json={"envelope": _observation_envelope()})
        client.post("/world-memory/remember", json={"envelope": _observation_without_evidence()})

        # Without require_evidence — both returned
        q_all = client.post("/world-memory/query", json={})
        assert len(q_all.json()["items"]) == 2

        # With require_evidence — only the evidence-backed one
        q_ev = client.post("/world-memory/query", json={"require_evidence": True})
        assert len(q_ev.json()["items"]) == 1

    def test_require_evidence_finds_item_beyond_non_evidence_prefix(self, client):
        """Regression: require_evidence must not miss evidence-backed items.

        Previously the store applied max_items *before* the evidence filter,
        so evidence-backed items beyond the first max_items non-evidence
        results were silently dropped and the query returned "refused" even
        though evidence existed in the store.
        """
        # Store 3 items WITHOUT evidence, then 1 WITH evidence
        for i in range(3):
            env = _observation_without_evidence()
            # Give each a unique content to avoid idempotency dedup
            env["world_memory_snapshot"]["content"] = f"no-evidence-{i}"
            env["idempotency_key"] = f"no-evidence-key-{i}"
            client.post("/world-memory/remember", json={"envelope": env})

        client.post(
            "/world-memory/remember",
            json={"envelope": _observation_envelope(evidence_ref="doc-deep")},
        )

        # With max_items=2 and require_evidence=True, the evidence-backed item
        # at position 4 must still be found (not capped by max_items=2).
        q = client.post(
            "/world-memory/query",
            json={"require_evidence": True, "max_items": 2},
        )
        qbody = q.json()
        assert qbody["status"] == "ok", (
            f"Expected 'ok' but got '{qbody['status']}' — "
            "evidence-backed item was missed by pre-filter capping"
        )
        assert len(qbody["items"]) == 1


# ============================================================================
# Test 3b: Observation evidence is readable in the response (P10-S)
# ============================================================================


class TestObservationEvidenceReadable:
    """Evidence-backed observations surface a stable evidence ID (P10-S).

    Regression for the P10-M evidence gap: an evidence-required query on a
    positive observation corpus returned items whose ``evidence_ids`` were all
    empty, so the response could not prove the items were evidence-backed.
    """

    def test_evidence_backed_observation_reports_evidence_id(self, client):
        client.post(
            "/world-memory/remember",
            json={"envelope": _observation_envelope(evidence_ref="doc-456")},
        )
        q = client.post(
            "/world-memory/query",
            json={"require_evidence": True},
        )
        qbody = q.json()
        assert qbody["status"] == "ok"
        assert len(qbody["items"]) == 1
        item = qbody["items"][0]
        assert item["kind"] == KIND_OBSERVATION
        assert len(item["evidence_ids"]) == 1
        # The evidence ID is a deterministic UUID derived from the raw ref.
        assert item["evidence_ids"][0] == str(
            uuid5(wmp._EVIDENCE_REF_NAMESPACE, "doc-456")
        )

    def test_evidence_less_observation_reports_no_evidence_id(self, client):
        client.post(
            "/world-memory/remember",
            json={"envelope": _observation_without_evidence()},
        )
        # Without require_evidence the item is returned and its evidence_ids
        # are honestly empty (an observation without any raw_evidence_ref).
        q = client.post("/world-memory/query", json={})
        qbody = q.json()
        assert qbody["status"] == "ok"
        assert len(qbody["items"]) == 1
        assert qbody["items"][0]["evidence_ids"] == []

    def test_deterministic_evidence_id_is_stable(self, client):
        """The derived evidence UUID is reproducible, not random per request."""
        client.post(
            "/world-memory/remember",
            json={"envelope": _observation_envelope(evidence_ref="doc-stable")},
        )
        first = client.post("/world-memory/query", json={"require_evidence": True}).json()
        second = client.post("/world-memory/query", json={"require_evidence": True}).json()
        assert first["items"][0]["evidence_ids"] == second["items"][0]["evidence_ids"]

    def test_whitespace_only_raw_evidence_ref_is_not_evidence(self, client):
        """A whitespace-only raw_evidence_ref is not evidence (P10-S).

        The public evidence-ID mapper rejects a whitespace-only raw reference,
        so the evidence-required filter must also reject it instead of emitting
        an item with an empty evidence-ID list.
        """
        client.post(
            "/world-memory/remember",
            json={"envelope": _observation_envelope(evidence_ref="   ")},
        )

        # require_evidence refuses the whitespace-only reference fail-closed.
        q = client.post("/world-memory/query", json={"require_evidence": True})
        qbody = q.json()
        assert qbody["status"] == "refused"
        assert qbody["refusal_reason"] == "insufficient_evidence"
        assert len(qbody["items"]) == 0

        # Without require_evidence the item is returned and its evidence_ids
        # are honestly empty.
        q_all = client.post("/world-memory/query", json={})
        qbody_all = q_all.json()
        assert qbody_all["status"] == "ok"
        assert len(qbody_all["items"]) == 1
        assert qbody_all["items"][0]["evidence_ids"] == []


# ============================================================================
# Test 4: Malformed / unknown payload rejection
# ============================================================================


class TestMalformedPayload:
    """Reject malformed envelopes and unknown kinds."""

    def test_missing_required_fields_returns_422(self, client):
        resp = client.post(
            "/world-memory/remember",
            json={"envelope": {"event_type": "world.observation.ingested"}},
        )
        assert resp.status_code == 422

    def test_wrong_schema_version_returns_422(self, client):
        env = _observation_envelope()
        env["schema_version"] = 999
        resp = client.post("/world-memory/remember", json={"envelope": env})
        assert resp.status_code == 422

    def test_wrong_aggregate_type_returns_422(self, client):
        env = _observation_envelope()
        env["aggregate_type"] = "wrong_type"
        resp = client.post("/world-memory/remember", json={"envelope": env})
        assert resp.status_code == 422

    def test_unknown_kind_returns_422(self, client):
        env = _observation_envelope()
        env["world_memory_snapshot"]["_kind"] = "unknown_kind"
        resp = client.post("/world-memory/remember", json={"envelope": env})
        assert resp.status_code == 422

    def test_invalid_aggregate_id_uuid_returns_422(self, client):
        env = _observation_envelope()
        env["aggregate_id"] = "not-a-uuid"
        resp = client.post("/world-memory/remember", json={"envelope": env})
        assert resp.status_code == 422

    def test_invalid_occurred_at_returns_422(self, client):
        env = _observation_envelope()
        env["occurred_at"] = "not-a-datetime"
        resp = client.post("/world-memory/remember", json={"envelope": env})
        assert resp.status_code == 422

    def test_invalid_query_aggregate_id_returns_422(self, client):
        q = client.post(
            "/world-memory/query",
            json={"aggregate_id": "not-a-uuid"},
        )
        assert q.status_code == 422

    def test_empty_envelope_dict_returns_422(self, client):
        resp = client.post("/world-memory/remember", json={"envelope": {}})
        assert resp.status_code == 422

    def test_envelope_not_a_dict_returns_422(self, client):
        resp = client.post("/world-memory/remember", json={"envelope": "not-a-dict"})
        assert resp.status_code == 422


# ============================================================================
# Test 5: Idempotency
# ============================================================================


class TestIdempotency:
    """Duplicate envelopes are deduplicated; conflicts are rejected."""

    def test_exact_duplicate_is_idempotent(self, client):
        env = _observation_envelope()

        r1 = client.post("/world-memory/remember", json={"envelope": env})
        assert r1.status_code == 201

        # Same envelope again — should succeed (idempotent)
        r2 = client.post("/world-memory/remember", json={"envelope": env})
        assert r2.status_code == 201

        # Store should have exactly 1 item
        assert _get_store().count() == 1

    def test_conflicting_idempotency_key_returns_409(self, client):
        env1 = _observation_envelope(content="first")
        # Build a second envelope with the same idempotency key but different content
        env2 = _observation_envelope(content="second")
        # Force same idempotency key
        env2["idempotency_key"] = env1["idempotency_key"]
        # Change the snapshot so it's a conflict
        env2["world_memory_snapshot"]["content"] = "different content"

        r1 = client.post("/world-memory/remember", json={"envelope": env1})
        assert r1.status_code == 201

        r2 = client.post("/world-memory/remember", json={"envelope": env2})
        assert r2.status_code == 409


# ============================================================================
# Test 6: Token budget
# ============================================================================


class TestTokenBudget:
    """Token budget limits the number of returned items."""

    def test_token_budget_truncates_items(self, client):
        # Store several observations with known content lengths
        for i in range(5):
            env = _observation_envelope(content=f"Observation number {i} " * 10)
            client.post("/world-memory/remember", json={"envelope": env})

        # Query with very small budget
        q = client.post(
            "/world-memory/query",
            json={"token_budget": 50},
        )
        qbody = q.json()
        assert qbody["token_used"] <= 50
        assert len(qbody["items"]) < 5  # Some items truncated


# ============================================================================
# Test 7: Response shape alignment (MCP schema compatibility)
# ============================================================================


class TestResponseShape:
    """Verify response shapes match the Rust schema expectations."""

    def test_remember_response_has_required_fields(self, client):
        env = _observation_envelope()
        resp = client.post("/world-memory/remember", json={"envelope": env})
        body = resp.json()

        # These fields must be present for the Rust schema to deserialize
        assert "success" in body
        assert "idempotency_key" in body
        assert "aggregate_id" in body
        assert "kind" in body
        assert "event_type" in body
        assert "message" in body

    def test_query_response_has_required_fields(self, client):
        env = _observation_envelope()
        client.post("/world-memory/remember", json={"envelope": env})

        q = client.post("/world-memory/query", json={})
        body = q.json()

        # These fields must be present for the Rust schema to deserialize
        assert "query_id" in body
        assert "items" in body
        assert "envelopes" in body
        assert "token_budget" in body
        assert "token_used" in body
        assert "status" in body
        # refusal_reason may be null but must be present
        assert "refusal_reason" in body

        # Items must have the right shape
        if body["items"]:
            item = body["items"][0]
            assert "kind" in item
            assert "id" in item
            assert "content" in item
            assert "evidence_ids" in item
