"""P6-D tests: Neo4j payload adapter for world-memory snapshots.

Covers all seven snapshot kinds, defensive copying, deterministic output,
malformed/missing kind, unsupported kind, and source envelope preservation
for the Neo4j target.

Structure: Given/When/Then (BDD-style).
"""

from __future__ import annotations

import uuid

import pytest

from app.memory_core.projection_worker.contracts import ProjectionEvent
from app.memory_core.world_memory_projection import (
    Neo4jPayload,
    WorldMemoryProjectionAdapterError,
    to_neo4j_payload,
)
from tests.unit.projection_fixtures import (
    _context_pack_snapshot,
    _fact_snapshot,
    _hypothesis_snapshot,
    _make_event,
    _obs_snapshot,
    _revocation_snapshot,
    _state_snapshot,
    _transition_snapshot,
)

# ===================================================================
# Neo4j adapter — all seven kinds
# ===================================================================


class TestNeo4jAdapterKinds:
    """Neo4j adapter produces correct payloads for each snapshot kind."""

    def test_observation(self):
        """Given an observation snapshot, when adapted, primary node has WorldObservation label."""
        event = _make_event(_obs_snapshot())
        result = to_neo4j_payload(event)

        assert isinstance(result, Neo4jPayload)
        assert result.primary_node.labels == ("WorldObservation",)
        assert result.primary_node.identity == "obs-001"
        assert "_kind" not in result.primary_node.properties

    def test_state(self):
        """Given a state snapshot, when adapted, primary node has WorldState label and evidence rels."""
        event = _make_event(_state_snapshot())
        result = to_neo4j_payload(event)

        assert result.primary_node.labels == ("WorldState",)
        evidence_rels = [
            r for r in result.relationships if r.rel_type == "HAS_EVIDENCE"
        ]
        assert len(evidence_rels) == 2
        assert evidence_rels[0].to_identity == "obs-1"

    def test_hypothesis(self):
        """Given a hypothesis snapshot, when adapted, primary node has Hypothesis label."""
        event = _make_event(_hypothesis_snapshot())
        result = to_neo4j_payload(event)

        assert result.primary_node.labels == ("Hypothesis",)
        contra_rels = [
            r for r in result.relationships if r.rel_type == "CONTRADICTS"
        ]
        assert len(contra_rels) == 1
        assert contra_rels[0].to_identity == "hyp-000"

    def test_fact(self):
        """Given a fact snapshot, when adapted, primary node has VerifiedFact label."""
        event = _make_event(_fact_snapshot())
        result = to_neo4j_payload(event)

        assert result.primary_node.labels == ("VerifiedFact",)
        derived = [
            r for r in result.relationships if r.rel_type == "DERIVED_FROM"
        ]
        assert len(derived) == 1
        assert derived[0].to_identity == "hyp-001"

    def test_context_pack(self):
        """Given a context-pack snapshot, when adapted, related nodes carry items."""
        event = _make_event(_context_pack_snapshot())
        result = to_neo4j_payload(event)

        assert result.primary_node.labels == ("WorldContextPack",)
        assert len(result.related_nodes) == 2
        contains = [
            r for r in result.relationships if r.rel_type == "CONTAINS_ITEM"
        ]
        assert len(contains) == 2
        assert contains[0].to_identity == "item-1"
        assert contains[1].to_identity == "item-2"

    def test_transition_record(self):
        """Given a transition-record snapshot, when adapted, primary node has TransitionRecord label."""
        event = _make_event(_transition_snapshot())
        result = to_neo4j_payload(event)

        assert result.primary_node.labels == ("TransitionRecord",)

    def test_revocation_record(self):
        """Given a revocation-record snapshot, when adapted, primary node has RevocationRecord label."""
        event = _make_event(_revocation_snapshot())
        result = to_neo4j_payload(event)

        assert result.primary_node.labels == ("RevocationRecord",)
        sup_rels = [
            r for r in result.relationships if r.rel_type == "SUPERSEDES"
        ]
        assert len(sup_rels) == 1
        assert sup_rels[0].to_identity == "fact-001"


# ===================================================================
# Defensive copying (Neo4j)
# ===================================================================


class TestNeo4jDefensiveCopying:
    """Neo4j adapter defensively copies mutable input."""

    def test_neo4j_source_envelope_isolation(self):
        """Given an event, when Neo4j payload is produced, mutating source_envelope is safe."""
        event = _make_event(_obs_snapshot())
        result = to_neo4j_payload(event)

        result.source_envelope["world_memory_snapshot"]["id"] = "mutated"

        fresh = event.payload
        assert fresh["world_memory_snapshot"]["id"] == "obs-001"

    def test_neo4j_properties_isolation(self):
        """Given an event, when Neo4j payload is produced, mutating node properties is safe."""
        event = _make_event(_obs_snapshot())
        result = to_neo4j_payload(event)

        result.primary_node.properties["content"] = "mutated"

        assert (
            result.source_envelope["world_memory_snapshot"]["content"]
            == "sensor reading 42"
        )


# ===================================================================
# Deterministic output (Neo4j)
# ===================================================================


class TestNeo4jDeterministic:
    """Neo4j adapter produces identical output for identical input."""

    def test_neo4j_deterministic(self):
        """Given the same event, when adapted twice, the results are equal."""
        event = _make_event(_fact_snapshot())
        r1 = to_neo4j_payload(event)
        r2 = to_neo4j_payload(event)

        assert r1 == r2


# ===================================================================
# Malformed envelope / missing kind / unsupported kind (Neo4j)
# ===================================================================


class TestNeo4jMalformedInput:
    """Neo4j adapter raises typed errors for malformed input."""

    def test_missing_world_memory_snapshot(self):
        """Given payload without 'world_memory_snapshot', when adapted, error raised."""
        bad_event = ProjectionEvent(
            outbox_id=uuid.uuid4(),
            aggregate_id=uuid.uuid4(),
            event_type="world.test.event",
            schema_version=1,
            projection_target="es-adapter-test",
            idempotency_key="test-key",
            payload={"other_key": "value"},
            attempt_count=0,
            max_attempts=3,
        )

        with pytest.raises(
            WorldMemoryProjectionAdapterError,
            match="world_memory_snapshot",
        ):
            to_neo4j_payload(bad_event)

    def test_missing_kind(self):
        """Given snapshot without '_kind', when adapted, an adapter error is raised."""
        event = _make_event({"id": "x", "content": "no kind"})

        with pytest.raises(
            WorldMemoryProjectionAdapterError,
            match="_kind",
        ):
            to_neo4j_payload(event)

    def test_empty_kind(self):
        """Given snapshot with empty '_kind', when adapted, an adapter error is raised."""
        event = _make_event({"_kind": "", "id": "x"})

        with pytest.raises(
            WorldMemoryProjectionAdapterError,
            match="_kind",
        ):
            to_neo4j_payload(event)

    def test_unsupported_kind_neo4j(self):
        """Given unknown kind, when adapted for Neo4j, adapter error is raised."""
        event = _make_event(
            {"_kind": "unknown_kind", "id": "x", "content": "test"}
        )

        with pytest.raises(
            WorldMemoryProjectionAdapterError,
            match="unsupported snapshot kind",
        ) as exc_info:
            to_neo4j_payload(event)

        assert exc_info.value.kind == "unknown_kind"

    def test_error_has_kind_attribute(self):
        """Given a kind-specific error, when raised, the error carries the kind."""
        event = _make_event({"_kind": "bogus", "id": "x"})

        with pytest.raises(WorldMemoryProjectionAdapterError) as exc_info:
            to_neo4j_payload(event)

        assert exc_info.value.kind == "bogus"


# ===================================================================
# Source envelope preservation (Neo4j)
# ===================================================================


class TestNeo4jEnvelopePreservation:
    """Neo4j adapter preserves the full source envelope."""

    def test_neo4j_preserves_full_envelope(self):
        """Given an event, when adapted for Neo4j, source_envelope contains full payload."""
        snap = _state_snapshot()
        event = _make_event(snap)
        result = to_neo4j_payload(event)

        assert "world_memory_snapshot" in result.source_envelope
        assert result.source_envelope["world_memory_snapshot"]["id"] == "state-001"
