"""Unit tests for P1-02 PostgresMemoryRepository mapping layer.

These tests exercise the pure mapping functions between domain
contracts and ORM rows. No database is required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.memory_core.repositories.postgres_mapping import (
    _ensure_utc_aware,
    _strip_to_naive_utc,
    dict_to_provenance,
    domain_to_evidence_row,
    evidence_row_to_domain,
    evidence_rows_to_domain,
    provenance_to_dict,
)
from app.memory_core.unpermissioned_contracts import (
    EvidenceRef,
    ProvenanceInfo,
)


class TestUtcNormalization:
    """UTC-aware timestamp normalization (contracts compatibility)."""

    def test_ensure_utc_aware_none(self) -> None:
        assert _ensure_utc_aware(None) is None

    def test_ensure_utc_aware_naive(self) -> None:
        naive = datetime(2026, 7, 24, 12, 0, 0)
        result = _ensure_utc_aware(naive)
        assert result.tzinfo == timezone.utc
        assert result.year == 2026

    def test_ensure_utc_aware_already_aware(self) -> None:
        aware = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
        result = _ensure_utc_aware(aware)
        assert result.tzinfo == timezone.utc
        assert result == aware

    def test_strip_to_naive_utc_from_aware(self) -> None:
        aware = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
        result = _strip_to_naive_utc(aware)
        assert result.tzinfo is None
        assert result == datetime(2026, 7, 24, 12, 0, 0)

    def test_strip_to_naive_utc_from_naive(self) -> None:
        naive = datetime(2026, 7, 24, 12, 0, 0)
        result = _strip_to_naive_utc(naive)
        assert result.tzinfo is None
        assert result == naive


class TestProvenanceRoundTrip:
    """Provenance serialization/deserialization including optional fields."""

    def test_full_provenance_round_trip(self) -> None:
        job_id = uuid4()
        extracted = datetime(2026, 7, 24, 10, 0, 0)
        original = ProvenanceInfo(
            source_type="document",
            source_name="test.pdf",
            source_uri="file:///test.pdf",
            ingestion_job_id=job_id,
            version=3,
            extracted_at=extracted,
            extracted_by="agent-1",
        )
        as_dict = provenance_to_dict(original)
        assert as_dict is not None
        restored = dict_to_provenance(as_dict)
        assert restored is not None
        assert restored.source_type == "document"
        assert restored.source_name == "test.pdf"
        assert restored.source_uri == "file:///test.pdf"
        assert restored.ingestion_job_id == job_id
        assert restored.version == 3
        assert restored.extracted_at == extracted
        assert restored.extracted_by == "agent-1"

    def test_minimal_provenance_round_trip(self) -> None:
        original = ProvenanceInfo(
            source_type="conversation",
            source_name="chat-1",
        )
        as_dict = provenance_to_dict(original)
        restored = dict_to_provenance(as_dict)
        assert restored is not None
        assert restored.source_type == "conversation"
        assert restored.source_name == "chat-1"
        assert restored.source_uri is None
        assert restored.ingestion_job_id is None
        assert restored.version == 1
        assert restored.extracted_by is None

    def test_none_provenance(self) -> None:
        assert provenance_to_dict(None) is None
        assert dict_to_provenance(None) is None
        assert dict_to_provenance({}) is None

    def test_provenance_with_aware_extracted_at(self) -> None:
        aware_ts = datetime(2026, 7, 24, 10, 0, 0, tzinfo=timezone.utc)
        original = ProvenanceInfo(
            source_type="tool_call",
            source_name="search",
            extracted_at=aware_ts,
        )
        as_dict = provenance_to_dict(original)
        restored = dict_to_provenance(as_dict)
        assert restored is not None
        assert restored.extracted_at.tzinfo is None
        assert restored.extracted_at == datetime(2026, 7, 24, 10, 0, 0)


class TestEvidenceRoundTrip:
    """Evidence mapping preserves all fields and deterministic position."""

    def test_evidence_round_trip(self) -> None:
        mi_id = uuid4()
        ts = datetime(2026, 7, 24, 10, 0, 0)
        original = EvidenceRef(
            source_id="doc-1",
            source_type="document",
            timestamp=ts,
            chunk_id="chunk-5",
            confidence=0.85,
            metadata={"page": 3},
        )
        row = domain_to_evidence_row(mi_id, 2, original)
        assert row.memory_item_id == mi_id
        assert row.position == 2
        assert row.source_id == "doc-1"
        assert row.chunk_id == "chunk-5"
        assert row.confidence == 0.85
        assert row.metadata_ == {"page": 3}
        assert row.timestamp.tzinfo == timezone.utc

        restored = evidence_row_to_domain(row)
        assert restored.source_id == "doc-1"
        assert restored.timestamp == ts
        assert restored.chunk_id == "chunk-5"
        assert restored.confidence == 0.85
        assert restored.metadata == {"page": 3}

    def test_evidence_deterministic_position(self) -> None:
        mi_id = uuid4()
        ts = datetime(2026, 7, 24, 10, 0, 0)
        ev0 = EvidenceRef(source_id="a", source_type="document", timestamp=ts)
        ev1 = EvidenceRef(source_id="b", source_type="document", timestamp=ts)
        ev2 = EvidenceRef(source_id="c", source_type="document", timestamp=ts)

        rows = [
            domain_to_evidence_row(mi_id, 2, ev2),
            domain_to_evidence_row(mi_id, 0, ev0),
            domain_to_evidence_row(mi_id, 1, ev1),
        ]
        restored = evidence_rows_to_domain(rows)
        assert [r.source_id for r in restored] == ["a", "b", "c"]

    def test_evidence_minimal_fields(self) -> None:
        mi_id = uuid4()
        ts = datetime(2026, 7, 24, 10, 0, 0)
        original = EvidenceRef(
            source_id="doc-x",
            source_type="observation",
            timestamp=ts,
        )
        row = domain_to_evidence_row(mi_id, 0, original)
        assert row.chunk_id is None
        assert row.confidence == 1.0
        assert row.metadata_ == {}

        restored = evidence_row_to_domain(row)
        assert restored.chunk_id is None
        assert restored.confidence == 1.0
        assert restored.metadata == {}
