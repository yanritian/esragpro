"""
Focused contract tests for the P6-G world-memory read-composition path.

Covers:
1. Request/response defaults for the P6-G fields (query, candidate_sources,
   latency_ms)
2. In-memory nonblank query: case-insensitive all-term matching returns only
   the matching authoritative envelope/item, leaves candidate_sources empty,
   and an unrelated query returns no_match
3. PostgreSQL/composed path via controlled async fakes: the returned envelope
   content is exactly the supplied authority envelope, elasticsearch/neo4j
   source traces serialize correctly, and latency_ms is preserved/nonnegative
4. A composed degraded source with no authoritative envelopes fails closed as
   no_match while returning its degraded trace (no exception, no fabricated
   item)

Envelope builders are redeclared locally — this module never imports from
another test module — and mirror the existing P4-W test style.
"""

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
def clear_store(monkeypatch):
    """Isolate reference-store tests from suite-level PostgreSQL session state."""
    monkeypatch.setattr(wmp, "_is_pg_authority_configured", lambda: False)
    wmp._get_store().clear()
    yield
    wmp._get_store().clear()


@pytest.fixture
def client():
    """Minimal FastAPI app for testing the router in isolation."""
    app = FastAPI()
    app.include_router(wmp.router)
    return TestClient(app)


# ============================================================================
# Helpers: build valid P7-B envelopes
# ============================================================================


def _observation_envelope(
    *,
    content: str = "Temperature is 23°C",
    evidence_ref: str = "doc-123",
) -> dict[str, Any]:
    """Build a valid observation envelope (same builder as the P4-W tests)."""
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
        projection_target="test",
    )


class _FakeStoredEnvelope:
    """Minimal stand-in for an authority ``StoredEnvelope`` exposing ``.envelope``."""

    def __init__(self, envelope: dict[str, Any]) -> None:
        self.envelope = envelope


# ============================================================================
# Test 1: P6-G request/response model defaults
# ============================================================================


class TestQueryModelDefaults:
    """P6-G request/response fields keep stable defaults."""

    def test_request_query_defaults_to_empty_string(self):
        assert wmp.WorldMemoryQueryRequest.model_fields["query"].default == ""

    def test_response_sources_and_latency_defaults(self):
        response = wmp.WorldMemoryQueryResponse(
            query_id="test-query-id",
            items=[],
            envelopes=[],
            token_budget=4000,
            token_used=0,
            status="no_match",
        )
        assert response.candidate_sources == []
        assert response.latency_ms == 0.0


# ============================================================================
# Test 2: In-memory nonblank query matching
# ============================================================================


class TestInMemoryNonBlankQuery:
    """Reference-seam all-term matching with empty candidate traces."""

    def test_reference_store_ignores_configured_pg_session(self, client, monkeypatch):
        """A configured session must not redirect this module's reference-store seam."""
        from app.memory_core import memory_plane_session

        monkeypatch.setattr(memory_plane_session, "is_configured", lambda: True)

        async def unexpected_pg_remember(_envelope):
            raise AssertionError("reference-store test unexpectedly selected PostgreSQL")

        monkeypatch.setattr(wmp, "_pg_remember", unexpected_pg_remember)
        envelope = _observation_envelope(content="Temperature is 23°C")

        resp = client.post("/world-memory/remember", json={"envelope": envelope})

        assert resp.status_code == 201, resp.text
        assert wmp._get_store().count() == 1

    def test_case_insensitive_single_term_matches(self, client):
        envelope = _observation_envelope(content="Temperature is 23°C")
        resp = client.post("/world-memory/remember", json={"envelope": envelope})
        assert resp.status_code == 201, resp.text

        q = client.post("/world-memory/query", json={"query": "TEMPERATURE"})
        assert q.status_code == 200, q.text
        body = q.json()
        assert body["status"] == "ok"
        assert len(body["items"]) == 1
        assert body["items"][0]["content"] == "Temperature is 23°C"
        assert body["candidate_sources"] == []
        assert body["latency_ms"] == 0.0

    def test_all_terms_must_match(self, client):
        envelope = _observation_envelope(content="Temperature is 23°C")
        client.post("/world-memory/remember", json={"envelope": envelope})

        ok = client.post("/world-memory/query", json={"query": "temperature 23"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "ok"
        assert len(ok.json()["items"]) == 1

        miss = client.post(
            "/world-memory/query",
            json={"query": "temperature pressure"},
        )
        assert miss.status_code == 200, miss.text
        assert miss.json()["status"] == "no_match"
        assert miss.json()["refusal_reason"] == "no_matching_items"
        assert miss.json()["items"] == []

    def test_only_matching_envelope_returned(self, client):
        match_env = _observation_envelope(content="Coolant leak in pump")
        other_env = _observation_envelope(content="Lights on floor three")
        client.post("/world-memory/remember", json={"envelope": match_env})
        client.post("/world-memory/remember", json={"envelope": other_env})

        q = client.post("/world-memory/query", json={"query": "coolant pump"})
        assert q.status_code == 200, q.text
        body = q.json()
        assert body["status"] == "ok"
        assert len(body["envelopes"]) == 1
        assert body["envelopes"][0]["idempotency_key"] == match_env["idempotency_key"]
        assert body["envelopes"][0] != other_env

    def test_unrelated_query_returns_no_match_with_empty_traces(self, client):
        envelope = _observation_envelope(content="Temperature is 23°C")
        client.post("/world-memory/remember", json={"envelope": envelope})

        q = client.post("/world-memory/query", json={"query": "racketball rules"})
        assert q.status_code == 200, q.text
        body = q.json()
        assert body["status"] == "no_match"
        assert body["refusal_reason"] == "no_matching_items"
        assert body["items"] == []
        assert body["envelopes"] == []
        assert body["candidate_sources"] == []
        assert body["latency_ms"] == 0.0


# ============================================================================
# Test 3: Composed PG path with controlled async fakes
# ============================================================================


class TestComposedPgQuery:
    """Monkeypatched PG authority + composed coordinator seam."""

    def test_composed_returns_supplied_envelope_and_traces(self, client, monkeypatch):
        supplied = _observation_envelope(content="Turbine bearing temperature rising")
        fake_envelopes = [_FakeStoredEnvelope(supplied)]
        fake_traces = [
            wmp.WorldMemorySourceTrace(
                source="elasticsearch",
                status="ok",
                candidate_count=2,
                latency_ms=10.5,
            ),
            wmp.WorldMemorySourceTrace(
                source="neo4j",
                status="ok",
                candidate_count=1,
                latency_ms=4.25,
            ),
        ]
        fake_latency = 15.5

        async def fake_pg_query_composed(**kwargs):
            return fake_envelopes, fake_traces, fake_latency

        monkeypatch.setattr(wmp, "_is_pg_authority_configured", lambda: True)
        monkeypatch.setattr(wmp, "_pg_query_composed", fake_pg_query_composed)

        q = client.post("/world-memory/query", json={"query": "turbine bearing"})
        assert q.status_code == 200, q.text
        body = q.json()

        # Envelope content is exactly the supplied authority envelope.
        assert body["status"] == "ok"
        assert len(body["envelopes"]) == 1
        assert body["envelopes"][0] == supplied

        # Candidate source traces serialize elasticsearch/neo4j fields.
        assert body["candidate_sources"] == [
            {
                "source": "elasticsearch",
                "status": "ok",
                "candidate_count": 2,
                "latency_ms": 10.5,
                "error": None,
            },
            {
                "source": "neo4j",
                "status": "ok",
                "candidate_count": 1,
                "latency_ms": 4.25,
                "error": None,
            },
        ]

        # latency_ms is preserved and nonnegative.
        assert body["latency_ms"] == fake_latency
        assert body["latency_ms"] >= 0

    def test_degraded_source_with_no_envelopes_fails_closed(self, client, monkeypatch):
        degraded_traces = [
            wmp.WorldMemorySourceTrace(
                source="elasticsearch",
                status="degraded",
                candidate_count=0,
                latency_ms=2.0,
                error="elasticsearch projection unavailable",
            )
        ]

        async def fake_pg_query_composed(**kwargs):
            return [], degraded_traces, 2.0

        monkeypatch.setattr(wmp, "_is_pg_authority_configured", lambda: True)
        monkeypatch.setattr(wmp, "_pg_query_composed", fake_pg_query_composed)

        q = client.post("/world-memory/query", json={"query": "turbine bearing"})
        assert q.status_code == 200, q.text
        body = q.json()

        # Fails closed: no_match, no fabricated items, no exception.
        assert body["status"] == "no_match"
        assert body["refusal_reason"] == "no_matching_items"
        assert body["items"] == []
        assert body["envelopes"] == []

        # The degraded trace is still reported.
        assert len(body["candidate_sources"]) == 1
        trace = body["candidate_sources"][0]
        assert trace["source"] == "elasticsearch"
        assert trace["status"] == "degraded"
        assert trace["candidate_count"] == 0
        assert trace["error"] == "elasticsearch projection unavailable"
        assert body["latency_ms"] == 2.0
