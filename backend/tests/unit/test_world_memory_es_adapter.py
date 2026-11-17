"""P6-D tests: Elasticsearch payload adapter for world-memory snapshots.

Covers all seven snapshot kinds, defensive copying, deterministic output,
malformed/missing kind, unsupported kind, and source envelope preservation
for the ES target.

Structure: Given/When/Then (BDD-style).
"""

from __future__ import annotations

import uuid

import pytest

from app.memory_core.projection_worker.contracts import ProjectionEvent
from app.memory_core.world_memory_projection import (
    ElasticsearchPayload,
    WorldMemoryProjectionAdapterError,
    to_elasticsearch_payload,
)
from app.memory_core.world_memory_serializer import (
    KIND_CONTEXT_PACK,
    KIND_FACT,
    KIND_HYPOTHESIS,
    KIND_OBSERVATION,
    KIND_REVOCATION,
    KIND_STATE,
    KIND_TRANSITION,
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
# Elasticsearch adapter — all seven kinds
# ===================================================================


class TestElasticsearchAdapterKinds:
    """ES adapter produces correct payloads for each snapshot kind."""

    def test_observation(self):
        """Given an observation snapshot, when adapted, ES payload carries all fields."""
        event = _make_event(_obs_snapshot())
        result = to_elasticsearch_payload(event)

        assert isinstance(result, ElasticsearchPayload)
        assert result.id == "obs-001"
        assert result.object_kind == KIND_OBSERVATION
        assert result.content == "sensor reading 42"
        assert result.evidence_ids == ("ev-1", "ev-2")
        assert result.timestamps["observed_at"] == "2026-01-01T00:00:00+00:00"
        assert result.timestamps["ingested_at"] == "2026-01-01T00:01:00+00:00"
        assert result.validity is None
        assert result.version == 1
        assert result.metadata == {"tag": "alpha"}

    def test_state(self):
        """Given a state snapshot, when adapted, evidence_ids come from observation_ids."""
        event = _make_event(_state_snapshot())
        result = to_elasticsearch_payload(event)

        assert result.id == "state-001"
        assert result.object_kind == KIND_STATE
        assert result.evidence_ids == ("obs-1", "obs-2")
        assert result.validity == {
            "start": "2026-01-01T00:00:00+00:00",
            "end": None,
        }
        assert result.supersedes == ("state-000",)
        assert result.fusion_version == 2
        assert result.projection_version == 1
        assert result.status == "active"

    def test_hypothesis(self):
        """Given a hypothesis snapshot, when adapted, evidence_ids come from observation_ids."""
        event = _make_event(_hypothesis_snapshot())
        result = to_elasticsearch_payload(event)

        assert result.id == "hyp-001"
        assert result.object_kind == KIND_HYPOTHESIS
        assert result.evidence_ids == ("obs-1",)
        assert result.contradicts == ("hyp-000",)
        assert result.status == "confirmed"

    def test_fact(self):
        """Given a fact snapshot, when adapted, ES payload carries evidence and hypothesis link."""
        event = _make_event(_fact_snapshot())
        result = to_elasticsearch_payload(event)

        assert result.id == "fact-001"
        assert result.object_kind == KIND_FACT
        assert result.evidence_ids == ("ev-1",)

    def test_context_pack(self):
        """Given a context-pack snapshot, when adapted, evidence_ids are collected from items."""
        event = _make_event(_context_pack_snapshot())
        result = to_elasticsearch_payload(event)

        assert result.id == "pack-001"
        assert result.object_kind == KIND_CONTEXT_PACK
        assert result.evidence_ids == ("ev-10", "ev-11")

    def test_transition_record(self):
        """Given a transition-record snapshot, when adapted, content comes from 'reason'."""
        event = _make_event(_transition_snapshot())
        result = to_elasticsearch_payload(event)

        assert result.id == "tr-001"
        assert result.object_kind == KIND_TRANSITION
        assert result.content == "status changed"
        assert result.timestamps["timestamp"] == "2026-01-01T00:00:00+00:00"

    def test_revocation_record(self):
        """Given a revocation-record snapshot, when adapted, content comes from 'reason'."""
        event = _make_event(_revocation_snapshot())
        result = to_elasticsearch_payload(event)

        assert result.id == "rev-001"
        assert result.object_kind == KIND_REVOCATION
        assert result.content == "evidence retracted"
        assert result.supersedes == ("fact-001",)

    def test_transition_falls_back_to_aggregate_id(self):
        """Given a transition snapshot with no 'id', when adapted, ES uses aggregate_id."""
        snap = _transition_snapshot()
        del snap["id"]
        agg = str(uuid.uuid4())
        event = _make_event(snap, aggregate_id=agg)
        result = to_elasticsearch_payload(event)

        assert result.id == agg


# ===================================================================
# Defensive copying (ES)
# ===================================================================


class TestElasticsearchDefensiveCopying:
    """ES adapter defensively copies mutable input."""

    def test_es_source_envelope_isolation(self):
        """Given an event, when ES payload is produced, mutating source_envelope is safe."""
        original_meta = {"tag": "alpha"}
        snap = _obs_snapshot(metadata=original_meta)
        event = _make_event(snap)
        result = to_elasticsearch_payload(event)

        result.source_envelope["world_memory_snapshot"]["metadata"]["tag"] = (
            "mutated"
        )

        fresh = event.payload
        assert fresh["world_memory_snapshot"]["metadata"]["tag"] == "alpha"

    def test_es_metadata_isolation(self):
        """Given an event, when ES payload is produced, mutating metadata is safe."""
        event = _make_event(_obs_snapshot())
        result = to_elasticsearch_payload(event)

        result.metadata["tag"] = "mutated"

        assert result.source_envelope["world_memory_snapshot"]["metadata"][
            "tag"
        ] == "alpha"


# ===================================================================
# Deterministic output (ES)
# ===================================================================


class TestElasticsearchDeterministic:
    """ES adapter produces identical output for identical input."""

    def test_es_deterministic(self):
        """Given the same event, when adapted twice, the results are equal."""
        event = _make_event(_state_snapshot())
        r1 = to_elasticsearch_payload(event)
        r2 = to_elasticsearch_payload(event)

        assert r1 == r2


# ===================================================================
# Malformed envelope / missing kind / unsupported kind (ES)
# ===================================================================


class TestElasticsearchMalformedInput:
    """ES adapter raises typed errors for malformed input."""

    def test_payload_not_dict(self):
        """Given non-dict snapshot, when adapted, an adapter error is raised."""
        bad_event = ProjectionEvent(
            outbox_id=uuid.uuid4(),
            aggregate_id=uuid.uuid4(),
            event_type="world.test.event",
            schema_version=1,
            projection_target="es-adapter-test",
            idempotency_key="test-key",
            payload={"world_memory_snapshot": "not-a-dict"},
            attempt_count=0,
            max_attempts=3,
        )

        with pytest.raises(WorldMemoryProjectionAdapterError) as exc_info:
            to_elasticsearch_payload(bad_event)

        assert exc_info.value.kind is None

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
            to_elasticsearch_payload(bad_event)

    def test_missing_kind(self):
        """Given snapshot without '_kind', when adapted, an adapter error is raised."""
        event = _make_event({"id": "x", "content": "no kind"})

        with pytest.raises(
            WorldMemoryProjectionAdapterError,
            match="_kind",
        ):
            to_elasticsearch_payload(event)

    def test_empty_kind(self):
        """Given snapshot with empty '_kind', when adapted, an adapter error is raised."""
        event = _make_event({"_kind": "", "id": "x"})

        with pytest.raises(
            WorldMemoryProjectionAdapterError,
            match="_kind",
        ):
            to_elasticsearch_payload(event)

    def test_unsupported_kind_es(self):
        """Given unknown kind, when adapted for ES, the adapter refuses with typed error."""
        event = _make_event(
            {"_kind": "unknown_kind", "id": "x", "content": "test"}
        )

        with pytest.raises(WorldMemoryProjectionAdapterError) as exc_info:
            to_elasticsearch_payload(event)

        assert exc_info.value.kind == "unknown_kind"

    def test_no_id_no_aggregate_id(self):
        """Given snapshot with no 'id', when adapted, ES falls back to aggregate_id."""
        event = _make_event({"_kind": KIND_OBSERVATION, "content": "no id"})
        result = to_elasticsearch_payload(event)
        assert result.id  # non-empty


# ===================================================================
# Source envelope preservation (ES)
# ===================================================================


class TestElasticsearchEnvelopePreservation:
    """ES adapter preserves the full source envelope."""

    def test_es_preserves_full_envelope(self):
        """Given an event, when adapted for ES, source_envelope contains full payload."""
        snap = _obs_snapshot()
        event = _make_event(snap)
        result = to_elasticsearch_payload(event)

        assert "world_memory_snapshot" in result.source_envelope
        assert result.source_envelope["world_memory_snapshot"]["id"] == "obs-001"
