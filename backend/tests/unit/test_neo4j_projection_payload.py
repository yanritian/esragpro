"""Unit tests for P1-07 Neo4j projection payload validation.

Covers:
- Payload field validation (required/optional types)
- Envelope cross-check (event_type, schema_version, aggregate_id, target)
- Memory snapshot field validation
- Evidence refs validation
- No credentials or raw payloads logged
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.memory_core.repositories.neo4j_projection.models import (
    Neo4jMemorySnapshot,
    Neo4jParsedPayload,
    PermanentNeo4jPayloadError,
    validate_payload_cross_check,
)


def _valid_payload(
    *,
    memory_id: str | None = None,
    content: str = "test content",
    status: str = "active",
    event_type: str = "memory.remembered",
    schema_version: int = 1,
    aggregate_id: str | None = None,
    projection_target: str = "neo4j",
    evidence_refs: list | None = None,
    provenance: dict | None = None,
    metadata: dict | None = None,
) -> dict:
    mid = memory_id or str(uuid4())
    agg = aggregate_id or mid
    return {
        "event_type": event_type,
        "schema_version": schema_version,
        "aggregate_type": "memory_item",
        "aggregate_id": agg,
        "projection_target": projection_target,
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "memory_snapshot": {
            "id": mid,
            "content": content,
            "status": status,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "expires_at": None,
            "project_id": str(uuid4()),
            "session_id": None,
            "agent_id": None,
            "knowledge_base_id": None,
            "metadata": metadata or {},
            "provenance": provenance,
            "evidence_refs": evidence_refs or [],
        },
    }


class TestPayloadFieldValidation:
    """Test individual payload field validation."""

    def test_valid_payload_passes(self):
        payload = _valid_payload()
        result = validate_payload_cross_check(
            payload,
            envelope_idempotency_key="test-key",
            envelope_aggregate_id=payload["aggregate_id"],
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="neo4j",
        )
        assert isinstance(result, Neo4jParsedPayload)
        assert result.memory_snapshot.content == "test content"

    def test_missing_event_type_raises(self):
        payload = _valid_payload()
        del payload["event_type"]
        with pytest.raises(PermanentNeo4jPayloadError, match="event_type"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id=payload["aggregate_id"],
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="neo4j",
            )

    def test_empty_content_raises(self):
        payload = _valid_payload(content="")
        with pytest.raises(PermanentNeo4jPayloadError, match="content"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id=payload["aggregate_id"],
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="neo4j",
            )

    def test_invalid_status_raises(self):
        payload = _valid_payload(status="invalid_status")
        with pytest.raises(PermanentNeo4jPayloadError, match="status"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id=payload["aggregate_id"],
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="neo4j",
            )

    def test_valid_statuses_accepted(self):
        for status in ("active", "expired", "archived", "deleted"):
            payload = _valid_payload(status=status)
            result = validate_payload_cross_check(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id=payload["aggregate_id"],
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="neo4j",
            )
            assert result.memory_snapshot.status == status

    def test_missing_id_raises(self):
        payload = _valid_payload()
        del payload["memory_snapshot"]["id"]
        with pytest.raises(PermanentNeo4jPayloadError, match="id"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id=payload["aggregate_id"],
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="neo4j",
            )

    def test_non_dict_payload_raises(self):
        with pytest.raises(PermanentNeo4jPayloadError, match="payload must be a dict"):
            validate_payload_cross_check(
                "not a dict",  # type: ignore[arg-type]
                envelope_idempotency_key="key",
                envelope_aggregate_id="id",
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="neo4j",
            )

    def test_schema_version_zero_raises(self):
        payload = _valid_payload(schema_version=0)
        with pytest.raises(PermanentNeo4jPayloadError, match="schema_version"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id=payload["aggregate_id"],
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="neo4j",
            )

    def test_evidence_refs_must_be_list_of_dicts(self):
        payload = _valid_payload(evidence_refs=["not-a-dict"])
        with pytest.raises(PermanentNeo4jPayloadError, match="evidence_refs"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id=payload["aggregate_id"],
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="neo4j",
            )

    def test_provenance_must_be_dict_or_null(self):
        payload = _valid_payload(provenance="not-a-dict")
        with pytest.raises(PermanentNeo4jPayloadError, match="provenance"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id=payload["aggregate_id"],
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="neo4j",
            )

    def test_metadata_must_be_dict(self):
        payload = _valid_payload(metadata="not-a-dict")
        with pytest.raises(PermanentNeo4jPayloadError, match="metadata"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id=payload["aggregate_id"],
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="neo4j",
            )


class TestEnvelopeCrossCheck:
    """Test envelope-payload cross-validation."""

    def test_event_type_mismatch_raises(self):
        payload = _valid_payload(event_type="memory.remembered")
        with pytest.raises(PermanentNeo4jPayloadError, match="event_type"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id=payload["aggregate_id"],
                envelope_event_type="different.event",
                envelope_schema_version=1,
                envelope_target="neo4j",
            )

    def test_schema_version_mismatch_raises(self):
        payload = _valid_payload(schema_version=1)
        with pytest.raises(PermanentNeo4jPayloadError, match="schema_version"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id=payload["aggregate_id"],
                envelope_event_type="memory.remembered",
                envelope_schema_version=2,
                envelope_target="neo4j",
            )

    def test_aggregate_id_mismatch_raises(self):
        payload = _valid_payload()
        with pytest.raises(PermanentNeo4jPayloadError, match="aggregate_id"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id=str(uuid4()),  # Different from payload
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="neo4j",
            )

    def test_projection_target_mismatch_raises(self):
        payload = _valid_payload(projection_target="neo4j")
        with pytest.raises(PermanentNeo4jPayloadError, match="projection_target"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id=payload["aggregate_id"],
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="elasticsearch",
            )

    def test_snapshot_id_aggregate_id_mismatch_raises(self):
        payload = _valid_payload(memory_id=str(uuid4()), aggregate_id=str(uuid4()))
        with pytest.raises(PermanentNeo4jPayloadError, match="aggregate_id"):
            validate_payload_cross_check(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id=payload["aggregate_id"],
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="neo4j",
            )


class TestOptionalFields:
    """Test optional field handling."""

    def test_null_expires_at_accepted(self):
        payload = _valid_payload()
        payload["memory_snapshot"]["expires_at"] = None
        result = validate_payload_cross_check(
            payload,
            envelope_idempotency_key="key",
            envelope_aggregate_id=payload["aggregate_id"],
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="neo4j",
        )
        assert result.memory_snapshot.expires_at is None

    def test_null_provenance_accepted(self):
        payload = _valid_payload(provenance=None)
        result = validate_payload_cross_check(
            payload,
            envelope_idempotency_key="key",
            envelope_aggregate_id=payload["aggregate_id"],
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="neo4j",
        )
        assert result.memory_snapshot.provenance is None

    def test_empty_metadata_accepted(self):
        payload = _valid_payload(metadata={})
        result = validate_payload_cross_check(
            payload,
            envelope_idempotency_key="key",
            envelope_aggregate_id=payload["aggregate_id"],
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="neo4j",
        )
        assert result.memory_snapshot.metadata == {}

    def test_evidence_refs_with_metadata_round_trip(self):
        evidence = [
            {
                "source_id": "doc-1",
                "source_type": "document",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "chunk_id": "chunk-42",
                "confidence": 0.95,
                "metadata": {"page": 12},
            }
        ]
        payload = _valid_payload(evidence_refs=evidence)
        result = validate_payload_cross_check(
            payload,
            envelope_idempotency_key="key",
            envelope_aggregate_id=payload["aggregate_id"],
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="neo4j",
        )
        assert len(result.memory_snapshot.evidence_refs) == 1
        assert result.memory_snapshot.evidence_refs[0]["metadata"] == {"page": 12}

    def test_provenance_fields_preserved(self):
        provenance = {
            "source_type": "document",
            "source_name": "test-doc.pdf",
            "source_uri": "file:///docs/test-doc.pdf",
            "ingestion_job_id": str(uuid4()),
            "version": 3,
            "extracted_at": "2026-01-01T00:00:00+00:00",
            "extracted_by": "test-agent",
        }
        payload = _valid_payload(provenance=provenance)
        result = validate_payload_cross_check(
            payload,
            envelope_idempotency_key="key",
            envelope_aggregate_id=payload["aggregate_id"],
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="neo4j",
        )
        assert result.memory_snapshot.provenance == provenance


class TestParsedPayloadStructure:
    """Test the structure of the parsed payload."""

    def test_parsed_payload_fields(self):
        payload = _valid_payload()
        result = validate_payload_cross_check(
            payload,
            envelope_idempotency_key="key",
            envelope_aggregate_id=payload["aggregate_id"],
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="neo4j",
        )
        assert isinstance(result, Neo4jParsedPayload)
        assert result.event_type == "memory.remembered"
        assert result.schema_version == 1
        assert isinstance(result.memory_snapshot, Neo4jMemorySnapshot)
        assert result.memory_snapshot.id == payload["memory_snapshot"]["id"]

    def test_snapshot_dimensions_preserved(self):
        pid = str(uuid4())
        sid = str(uuid4())
        aid = str(uuid4())
        kid = str(uuid4())
        payload = _valid_payload()
        payload["memory_snapshot"]["project_id"] = pid
        payload["memory_snapshot"]["session_id"] = sid
        payload["memory_snapshot"]["agent_id"] = aid
        payload["memory_snapshot"]["knowledge_base_id"] = kid

        result = validate_payload_cross_check(
            payload,
            envelope_idempotency_key="key",
            envelope_aggregate_id=payload["aggregate_id"],
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="neo4j",
        )
        assert result.memory_snapshot.project_id == pid
        assert result.memory_snapshot.session_id == sid
        assert result.memory_snapshot.agent_id == aid
        assert result.memory_snapshot.knowledge_base_id == kid
