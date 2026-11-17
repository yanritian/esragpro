"""Unit tests for MinIO projection payload validation (P1-08).

Covers:
- Payload validation (valid/invalid cases)
- Content hash determinism (based on memory_snapshot.content)
- Object key derivation
- Error classification
- Evidence bytes are the original content, not serialized refs
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.memory_core.repositories.minio_projection.models import (
    MinioEvidencePayload,
    PermanentMinioPayloadError,
    compute_content_hash,
    validate_evidence_payload,
)


def _make_valid_payload(
    *,
    memory_id: str | None = None,
    content: str = "test content",
    evidence_refs: list | None = None,
) -> dict:
    """Build a valid projection event payload."""
    mid = memory_id or str(uuid4())
    refs = (
        evidence_refs
        if evidence_refs is not None
        else [
            {
                "source_id": "doc-123",
                "source_type": "document",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "chunk_id": "chunk-0",
                "confidence": 0.95,
                "metadata": {"page": 1},
            }
        ]
    )
    return {
        "event_type": "memory.remembered",
        "schema_version": 1,
        "aggregate_type": "memory_item",
        "aggregate_id": mid,
        "projection_target": "minio",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "memory_snapshot": {
            "id": mid,
            "content": content,
            "status": "active",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "expires_at": None,
            "project_id": None,
            "session_id": None,
            "agent_id": None,
            "knowledge_base_id": None,
            "provenance": None,
            "metadata": {},
            "evidence_refs": refs,
        },
    }


class TestComputeContentHash:
    """Test content hash computation."""

    def test_deterministic(self):
        """Same input produces same hash."""
        data = b"test evidence"
        hash1 = compute_content_hash(data)
        hash2 = compute_content_hash(data)
        assert hash1 == hash2

    def test_different_input(self):
        """Different input produces different hash."""
        hash1 = compute_content_hash(b"data1")
        hash2 = compute_content_hash(b"data2")
        assert hash1 != hash2

    def test_sha256_format(self):
        """Hash is a 64-character hex string."""
        hash_value = compute_content_hash(b"test")
        assert len(hash_value) == 64
        assert all(c in "0123456789abcdef" for c in hash_value)


class TestMinioEvidencePayload:
    """Test MinioEvidencePayload dataclass."""

    def test_object_key_format(self):
        """Object key follows expected format."""
        memory_id = uuid4()
        content_hash = "a" * 64
        payload = MinioEvidencePayload(
            memory_id=memory_id,
            evidence_bytes=b"test",
            content_hash=content_hash,
            source_id="doc-1",
            source_type="document",
            timestamp="2026-01-01T00:00:00+00:00",
            chunk_id=None,
            confidence=1.0,
            metadata={},
        )
        key = payload.object_key()
        assert key == f"memory/{memory_id}/evidence/{content_hash[:16]}"

    def test_object_key_deterministic(self):
        """Same memory_id + content_hash produces same key."""
        memory_id = uuid4()
        content_hash = "b" * 64
        payload = MinioEvidencePayload(
            memory_id=memory_id,
            evidence_bytes=b"test",
            content_hash=content_hash,
            source_id="doc-1",
            source_type="document",
            timestamp="2026-01-01T00:00:00+00:00",
            chunk_id=None,
            confidence=1.0,
            metadata={},
        )
        key1 = payload.object_key()
        key2 = payload.object_key()
        assert key1 == key2


class TestValidateEvidencePayload:
    """Test validate_evidence_payload function."""

    def test_valid_payload(self):
        """Valid payload passes validation."""
        memory_id = str(uuid4())
        payload = _make_valid_payload(memory_id=memory_id)
        result = validate_evidence_payload(
            payload,
            envelope_idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
            envelope_aggregate_id=memory_id,
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="minio",
        )
        assert isinstance(result, MinioEvidencePayload)
        assert str(result.memory_id) == memory_id
        assert len(result.evidence_bytes) > 0
        assert len(result.content_hash) == 64

    def test_evidence_bytes_is_snapshot_content(self):
        """The object body is memory_snapshot.content, not serialized ref."""
        memory_id = str(uuid4())
        content = "This is the original evidence content"
        payload = _make_valid_payload(memory_id=memory_id, content=content)
        result = validate_evidence_payload(
            payload,
            envelope_idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
            envelope_aggregate_id=memory_id,
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="minio",
        )
        assert result.evidence_bytes == content.encode("utf-8")
        assert result.content_hash == compute_content_hash(content.encode("utf-8"))

    def test_evidence_bytes_not_serialized_ref(self):
        """Evidence bytes must NOT be the JSON-serialized evidence ref."""
        memory_id = str(uuid4())
        content = "original content text"
        refs = [
            {
                "source_id": "doc-123",
                "source_type": "document",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "chunk_id": "chunk-0",
                "confidence": 0.95,
                "metadata": {"page": 1},
            }
        ]
        payload = _make_valid_payload(memory_id=memory_id, content=content, evidence_refs=refs)
        result = validate_evidence_payload(
            payload,
            envelope_idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
            envelope_aggregate_id=memory_id,
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="minio",
        )
        # Evidence bytes should be the content, not the ref
        ref_json = json.dumps(refs[0], sort_keys=True).encode("utf-8")
        assert result.evidence_bytes != ref_json
        assert result.evidence_bytes == content.encode("utf-8")

    def test_invalid_event_type(self):
        """Wrong event_type raises error."""
        memory_id = str(uuid4())
        payload = _make_valid_payload(memory_id=memory_id)
        with pytest.raises(PermanentMinioPayloadError, match="event_type"):
            validate_evidence_payload(
                payload,
                envelope_idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
                envelope_aggregate_id=memory_id,
                envelope_event_type="wrong.type",
                envelope_schema_version=1,
                envelope_target="minio",
            )

    def test_invalid_schema_version(self):
        """Wrong schema_version raises error."""
        memory_id = str(uuid4())
        payload = _make_valid_payload(memory_id=memory_id)
        with pytest.raises(PermanentMinioPayloadError, match="schema_version"):
            validate_evidence_payload(
                payload,
                envelope_idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
                envelope_aggregate_id=memory_id,
                envelope_event_type="memory.remembered",
                envelope_schema_version=2,
                envelope_target="minio",
            )

    def test_invalid_target(self):
        """Wrong projection_target raises error."""
        memory_id = str(uuid4())
        payload = _make_valid_payload(memory_id=memory_id)
        with pytest.raises(PermanentMinioPayloadError, match="projection_target"):
            validate_evidence_payload(
                payload,
                envelope_idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
                envelope_aggregate_id=memory_id,
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="redis",
            )

    def test_missing_memory_snapshot(self):
        """Missing memory_snapshot raises error."""
        payload = {"event_type": "memory.remembered"}
        with pytest.raises(PermanentMinioPayloadError, match="memory_snapshot"):
            validate_evidence_payload(
                payload,
                envelope_idempotency_key="key",
                envelope_aggregate_id="id",
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="minio",
            )

    def test_missing_content(self):
        """Missing memory_snapshot.content raises error."""
        memory_id = str(uuid4())
        payload = _make_valid_payload(memory_id=memory_id)
        del payload["memory_snapshot"]["content"]
        with pytest.raises(PermanentMinioPayloadError, match="content"):
            validate_evidence_payload(
                payload,
                envelope_idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
                envelope_aggregate_id=memory_id,
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="minio",
            )

    def test_empty_evidence_refs(self):
        """Empty evidence_refs raises error."""
        memory_id = str(uuid4())
        payload = _make_valid_payload(memory_id=memory_id, evidence_refs=[])
        with pytest.raises(PermanentMinioPayloadError, match="evidence_refs is empty"):
            validate_evidence_payload(
                payload,
                envelope_idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
                envelope_aggregate_id=memory_id,
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="minio",
            )

    def test_aggregate_id_mismatch(self):
        """Mismatched aggregate_id raises error."""
        memory_id = str(uuid4())
        wrong_id = str(uuid4())
        payload = _make_valid_payload(memory_id=memory_id)
        with pytest.raises(PermanentMinioPayloadError, match="does not match"):
            validate_evidence_payload(
                payload,
                envelope_idempotency_key=f"memory:{wrong_id}:remembered:v1:minio",
                envelope_aggregate_id=wrong_id,
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="minio",
            )

    def test_invalid_confidence_range(self):
        """Confidence out of [0, 1] raises error."""
        memory_id = str(uuid4())
        payload = _make_valid_payload(
            memory_id=memory_id,
            evidence_refs=[
                {
                    "source_id": "doc-1",
                    "source_type": "document",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "chunk_id": None,
                    "confidence": 1.5,  # Out of range
                    "metadata": {},
                }
            ],
        )
        with pytest.raises(PermanentMinioPayloadError, match="out of range"):
            validate_evidence_payload(
                payload,
                envelope_idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
                envelope_aggregate_id=memory_id,
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="minio",
            )

    def test_evidence_bytes_deterministic(self):
        """Same content produces same bytes and hash."""
        memory_id = str(uuid4())
        payload = _make_valid_payload(memory_id=memory_id, content="same content")
        result1 = validate_evidence_payload(
            payload,
            envelope_idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
            envelope_aggregate_id=memory_id,
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="minio",
        )
        result2 = validate_evidence_payload(
            payload,
            envelope_idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
            envelope_aggregate_id=memory_id,
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="minio",
        )
        assert result1.evidence_bytes == result2.evidence_bytes
        assert result1.content_hash == result2.content_hash

    def test_source_identity_fields_preserved(self):
        """Source identity fields from first ref are preserved for metadata."""
        memory_id = str(uuid4())
        payload = _make_valid_payload(memory_id=memory_id)
        result = validate_evidence_payload(
            payload,
            envelope_idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
            envelope_aggregate_id=memory_id,
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="minio",
        )
        assert result.source_id == "doc-123"
        assert result.source_type == "document"
        assert result.chunk_id == "chunk-0"
        assert result.confidence == 0.95
        assert result.metadata == {"page": 1}
