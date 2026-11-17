"""Unit tests for P1-03 outbox payload serialization and idempotency keys."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from app.memory_core.repositories.postgres_outbox import (
    AGGREGATE_TYPE,
    EVENT_TYPE_REMEMBERED,
    SCHEMA_VERSION,
    build_event_payload,
    build_idempotency_key,
)
from app.memory_core.unpermissioned_contracts import EvidenceRef
from tests.unit._p1_03_outbox_fixtures import make_memory


class TestPayloadSerialization:
    def test_full_payload_is_json_serializable(self) -> None:
        item = make_memory()
        payload = build_event_payload(
            memory_item=item, projection_target="redis",
        )
        encoded = json.dumps(payload)
        decoded = json.loads(encoded)
        assert decoded["event_type"] == EVENT_TYPE_REMEMBERED
        assert decoded["schema_version"] == SCHEMA_VERSION
        assert decoded["aggregate_type"] == AGGREGATE_TYPE
        assert decoded["aggregate_id"] == str(item.id)
        assert decoded["projection_target"] == "redis"
        assert isinstance(decoded["occurred_at"], str)

    def test_occurred_at_matches_memory_created_at_and_is_stable(self) -> None:
        """All targets for one aggregate share one occurred_at = created_at."""
        item = make_memory()
        payloads = [
            build_event_payload(memory_item=item, projection_target=t)
            for t in ("redis", "elasticsearch", "neo4j")
        ]
        occurred_values = {p["occurred_at"] for p in payloads}
        assert len(occurred_values) == 1, "occurred_at must be identical across targets"
        expected = payloads[0]["memory_snapshot"]["created_at"]
        assert occurred_values.pop() == expected

    def test_memory_snapshot_fields(self) -> None:
        item = make_memory()
        payload = build_event_payload(
            memory_item=item, projection_target="elasticsearch",
        )
        snap = payload["memory_snapshot"]
        assert snap["id"] == str(item.id)
        assert snap["content"] == item.content
        assert snap["status"] == "active"
        assert snap["project_id"] == str(item.project_id)
        assert snap["session_id"] == str(item.session_id)
        assert snap["agent_id"] == str(item.agent_id)
        assert snap["knowledge_base_id"] == str(item.knowledge_base_id)
        assert snap["metadata"] == {"tag": "important"}

    def test_naive_timestamps_normalize_to_explicit_utc(self) -> None:
        item = make_memory(
            created_at=datetime(2026, 7, 24, 10, 0, 0),
            updated_at=datetime(2026, 7, 24, 10, 0, 0),
            expires_at=datetime(2026, 12, 31, 23, 59, 59),
        )
        payload = build_event_payload(
            memory_item=item, projection_target="redis",
        )
        snap = payload["memory_snapshot"]
        assert "+00:00" in snap["created_at"]
        assert "+00:00" in snap["updated_at"]
        assert "+00:00" in snap["expires_at"]

    def test_aware_timestamps_convert_to_utc(self) -> None:
        aware = datetime(2026, 7, 24, 15, 0, 0, tzinfo=timezone.utc)
        item = make_memory(
            created_at=aware, updated_at=aware, expires_at=None,
        )
        payload = build_event_payload(
            memory_item=item, projection_target="redis",
        )
        snap = payload["memory_snapshot"]
        assert snap["created_at"].endswith("+00:00")
        assert snap["expires_at"] is None

    def test_evidence_round_trip_deterministic_order(self) -> None:
        item = make_memory()
        payload = build_event_payload(
            memory_item=item, projection_target="neo4j",
        )
        evidence = payload["memory_snapshot"]["evidence_refs"]
        assert len(evidence) == 2
        assert evidence[0]["source_id"] == "doc-1"
        assert evidence[0]["chunk_id"] == "c1"
        assert evidence[0]["confidence"] == 0.9
        assert evidence[0]["metadata"] == {"page": 1}
        assert evidence[1]["source_id"] == "doc-2"
        assert evidence[1]["chunk_id"] is None

    def test_provenance_round_trip(self) -> None:
        item = make_memory()
        payload = build_event_payload(
            memory_item=item, projection_target="redis",
        )
        prov = payload["memory_snapshot"]["provenance"]
        assert prov is not None
        assert prov["source_type"] == "document"
        assert prov["source_name"] == "spec.pdf"
        assert prov["source_uri"] == "file:///spec.pdf"
        assert prov["ingestion_job_id"] == str(item.provenance.ingestion_job_id)
        assert prov["version"] == 1
        assert prov["extracted_by"] == "agent-x"

    def test_optional_fields_are_json_null(self) -> None:
        item = make_memory(
            provenance=None, expires_at=None, project_id=None,
            session_id=None, agent_id=None, knowledge_base_id=None,
        )
        payload = build_event_payload(
            memory_item=item, projection_target="redis",
        )
        snap = payload["memory_snapshot"]
        assert snap["provenance"] is None
        assert snap["expires_at"] is None
        assert snap["project_id"] is None
        assert snap["session_id"] is None
        assert snap["agent_id"] is None
        assert snap["knowledge_base_id"] is None

    def test_does_not_mutate_caller_metadata(self) -> None:
        original_meta = {"key": "value", "nested": {"a": 1}}
        item = make_memory(metadata=original_meta)
        payload = build_event_payload(
            memory_item=item, projection_target="redis",
        )
        payload["memory_snapshot"]["metadata"]["key"] = "mutated"
        assert original_meta["key"] == "value"

    def test_does_not_mutate_caller_evidence(self) -> None:
        evidence = [
            EvidenceRef(
                source_id="doc", source_type="document",
                timestamp=datetime(2026, 7, 24, 10, 0, 0),
                metadata={"page": 5},
            ),
        ]
        item = make_memory(evidence_refs=evidence)
        payload = build_event_payload(
            memory_item=item, projection_target="redis",
        )
        payload["memory_snapshot"]["evidence_refs"][0]["metadata"]["page"] = 999
        assert evidence[0].metadata == {"page": 5}


class TestIdempotencyKeys:
    def test_deterministic_per_target(self) -> None:
        aggregate_id = uuid4()
        assert build_idempotency_key(aggregate_id, "redis") == (
            build_idempotency_key(aggregate_id, "redis")
        )

    def test_distinct_per_target(self) -> None:
        aggregate_id = uuid4()
        assert (
            build_idempotency_key(aggregate_id, "redis")
            != build_idempotency_key(aggregate_id, "elasticsearch")
        )

    def test_distinct_per_aggregate(self) -> None:
        assert (
            build_idempotency_key(uuid4(), "redis")
            != build_idempotency_key(uuid4(), "redis")
        )

    def test_expected_format(self) -> None:
        aggregate_id = uuid4()
        expected = f"memory:{aggregate_id}:remembered:v1:redis"
        assert build_idempotency_key(aggregate_id, "redis") == expected
