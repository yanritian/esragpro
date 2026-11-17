"""Shared fixture builder for P1-03 outbox unit tests."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from app.memory_core.unpermissioned_contracts import (
    EvidenceRef,
    MemoryItem,
    MemoryItemStatus,
    ProvenanceInfo,
)


def make_memory(**overrides) -> MemoryItem:
    """Build a fully populated ``MemoryItem`` for unit tests."""
    defaults = {
        "id": uuid4(),
        "content": "hello world",
        "status": MemoryItemStatus.ACTIVE,
        "evidence_refs": [
            EvidenceRef(
                source_id="doc-1", source_type="document",
                timestamp=datetime(2026, 7, 24, 10, 0, 0),
                chunk_id="c1", confidence=0.9, metadata={"page": 1},
            ),
            EvidenceRef(
                source_id="doc-2", source_type="document",
                timestamp=datetime(2026, 7, 24, 10, 5, 0),
                confidence=0.75,
            ),
        ],
        "provenance": ProvenanceInfo(
            source_type="document", source_name="spec.pdf",
            source_uri="file:///spec.pdf",
            ingestion_job_id=uuid4(),
            extracted_at=datetime(2026, 7, 24, 9, 0, 0),
            extracted_by="agent-x",
        ),
        "project_id": uuid4(),
        "session_id": uuid4(),
        "agent_id": uuid4(),
        "knowledge_base_id": uuid4(),
        "created_at": datetime(2026, 7, 24, 10, 0, 0),
        "updated_at": datetime(2026, 7, 24, 10, 0, 0),
        "expires_at": datetime(2026, 12, 31, 23, 59, 59),
        "metadata": {"tag": "important"},
    }
    defaults.update(overrides)
    return MemoryItem(**defaults)
