"""Unit tests for ORM-to-domain memory row mapping.

Separated from ``test_postgres_memory_repository.py`` to keep each unit
module below the 250 non-comment line gate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.db.models.memory_plane import (
    EvidenceRef as EvidenceRefRow,
    MemoryItem as MemoryItemRow,
)
from app.memory_core.repositories.postgres_mapping import (
    memory_row_to_domain,
)
from app.memory_core.unpermissioned_contracts import (
    MemoryItemStatus,
)


class TestMemoryRowToDomain:
    """ORM-to-domain mapping preserves all fields."""

    def test_full_round_trip(self) -> None:
        mi_id = uuid4()
        project_id = uuid4()
        session_id = uuid4()
        agent_id = uuid4()
        kb_id = uuid4()
        now_aware = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
        expires_aware = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        job_id = uuid4()

        row = MemoryItemRow(
            id=mi_id,
            content="Test memory content",
            status="active",
            provenance={
                "source_type": "document",
                "source_name": "spec.pdf",
                "source_uri": None,
                "ingestion_job_id": str(job_id),
                "version": 1,
                "extracted_at": now_aware.isoformat(),
                "extracted_by": "ingestor",
            },
            project_id=project_id,
            session_id=session_id,
            agent_id=agent_id,
            knowledge_base_id=kb_id,
            expires_at=expires_aware,
            metadata_={"key": "value"},
            created_at=now_aware,
            updated_at=now_aware,
        )
        ev_ts = datetime(2026, 7, 24, 10, 0, 0, tzinfo=timezone.utc)
        ev_rows = [
            EvidenceRefRow(
                id=uuid4(),
                memory_item_id=mi_id,
                position=0,
                source_id="src-1",
                source_type="document",
                timestamp=ev_ts,
                chunk_id="c1",
                confidence=0.9,
                metadata_={"page": 1},
            ),
            EvidenceRefRow(
                id=uuid4(),
                memory_item_id=mi_id,
                position=1,
                source_id="src-2",
                source_type="document",
                timestamp=ev_ts,
                chunk_id=None,
                confidence=0.7,
                metadata_={},
            ),
        ]

        domain = memory_row_to_domain(row, ev_rows)
        assert domain.id == mi_id
        assert domain.content == "Test memory content"
        assert domain.status == MemoryItemStatus.ACTIVE
        assert domain.project_id == project_id
        assert domain.session_id == session_id
        assert domain.agent_id == agent_id
        assert domain.knowledge_base_id == kb_id
        assert domain.metadata == {"key": "value"}
        assert domain.created_at.tzinfo is None
        assert domain.updated_at.tzinfo is None
        assert domain.expires_at is not None
        assert domain.expires_at.tzinfo is None
        assert domain.provenance is not None
        assert domain.provenance.source_type == "document"
        assert domain.provenance.source_name == "spec.pdf"
        assert domain.provenance.ingestion_job_id == job_id
        assert domain.provenance.extracted_by == "ingestor"
        assert len(domain.evidence_refs) == 2
        assert domain.evidence_refs[0].source_id == "src-1"
        assert domain.evidence_refs[1].source_id == "src-2"

    def test_minimal_row(self) -> None:
        mi_id = uuid4()
        now_aware = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
        row = MemoryItemRow(
            id=mi_id,
            content="Simple",
            status="active",
            provenance=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            expires_at=None,
            metadata_={},
            created_at=now_aware,
            updated_at=now_aware,
        )
        domain = memory_row_to_domain(row, [])
        assert domain.provenance is None
        assert domain.expires_at is None
        assert domain.evidence_refs == []
        assert domain.metadata == {}
        assert domain.project_id is None

    def test_status_preserved(self) -> None:
        mi_id = uuid4()
        now_aware = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
        for status_val in ("active", "expired", "archived", "deleted"):
            row = MemoryItemRow(
                id=mi_id,
                content="Test",
                status=status_val,
                provenance=None,
                created_at=now_aware,
                updated_at=now_aware,
            )
            domain = memory_row_to_domain(row, [])
            assert domain.status == MemoryItemStatus(status_val)
