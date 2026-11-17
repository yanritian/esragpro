"""Pure mapping between domain contracts and ORM rows (P1-02).

No I/O, no session, no engine. Every function is a pure transform so
unit tests can exercise round-trip fidelity without a database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, overload
from uuid import UUID

from app.db.models.memory_plane import (
    EvidenceRef as EvidenceRefRow,
    MemoryItem as MemoryItemRow,
)
from app.memory_core.unpermissioned_contracts import (
    EvidenceRef as DomainEvidenceRef,
    MemoryItem as DomainMemoryItem,
    MemoryItemStatus,
    ProvenanceInfo,
)


@overload
def _ensure_utc_aware(value: datetime) -> datetime: ...
@overload
def _ensure_utc_aware(value: None) -> None: ...
@overload
def _ensure_utc_aware(value: Optional[datetime]) -> Optional[datetime]: ...
def _ensure_utc_aware(value: Optional[datetime]) -> Optional[datetime]:
    """Return a timezone-aware UTC datetime.

    Naive inputs are assumed to be UTC; aware inputs are converted to UTC.
    ``None`` passes through unchanged.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)


def _strip_to_naive_utc(value: datetime) -> datetime:
    """Return a naive UTC datetime from an aware or naive input."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

_PROVENANCE_KEYS = (
    "source_type",
    "source_name",
    "source_uri",
    "ingestion_job_id",
    "version",
    "extracted_at",
    "extracted_by",
)


def provenance_to_dict(provenance: Optional[ProvenanceInfo]) -> Optional[Dict[str, Any]]:
    """Serialize a domain ``ProvenanceInfo`` to a JSONB-compatible dict."""
    if provenance is None:
        return None
    return {
        "source_type": provenance.source_type,
        "source_name": provenance.source_name,
        "source_uri": provenance.source_uri,
        "ingestion_job_id": (
            str(provenance.ingestion_job_id) if provenance.ingestion_job_id else None
        ),
        "version": provenance.version,
        "extracted_at": _ensure_utc_aware(provenance.extracted_at).isoformat(),
        "extracted_by": provenance.extracted_by,
    }


def dict_to_provenance(data: Optional[Dict[str, Any]]) -> Optional[ProvenanceInfo]:
    """Deserialize a JSONB dict back to ``ProvenanceInfo``."""
    if not data:
        return None
    raw_extracted = data.get("extracted_at")
    if isinstance(raw_extracted, str):
        extracted_at = datetime.fromisoformat(raw_extracted)
        if extracted_at.tzinfo is not None:
            extracted_at = extracted_at.astimezone(timezone.utc).replace(tzinfo=None)
    elif isinstance(raw_extracted, datetime):
        extracted_at = _strip_to_naive_utc(raw_extracted)
    else:
        extracted_at = datetime.now(timezone.utc).replace(tzinfo=None)

    raw_job_id = data.get("ingestion_job_id")
    ingestion_job_id: Optional[UUID] = None
    if raw_job_id:
        ingestion_job_id = UUID(str(raw_job_id))

    return ProvenanceInfo(
        source_type=str(data.get("source_type", "")),
        source_name=str(data.get("source_name", "")),
        source_uri=data.get("source_uri"),
        ingestion_job_id=ingestion_job_id,
        version=int(data.get("version", 1)),
        extracted_at=extracted_at,
        extracted_by=data.get("extracted_by"),
    )


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def domain_to_evidence_row(
    memory_item_id: UUID,
    position: int,
    evidence: DomainEvidenceRef,
) -> EvidenceRefRow:
    """Build an ORM ``EvidenceRef`` row from a domain evidence ref."""
    return EvidenceRefRow(
        memory_item_id=memory_item_id,
        position=position,
        source_id=evidence.source_id,
        source_type=evidence.source_type,
        timestamp=_ensure_utc_aware(evidence.timestamp),
        chunk_id=evidence.chunk_id,
        confidence=evidence.confidence,
        metadata_=dict(evidence.metadata) if evidence.metadata else {},
    )


def evidence_row_to_domain(row: EvidenceRefRow) -> DomainEvidenceRef:
    """Build a domain ``EvidenceRef`` from an ORM row."""
    return DomainEvidenceRef(
        source_id=row.source_id,
        source_type=row.source_type,
        timestamp=_strip_to_naive_utc(row.timestamp),
        chunk_id=row.chunk_id,
        confidence=row.confidence,
        metadata=dict(row.metadata_) if row.metadata_ else {},
    )


def evidence_rows_to_domain(rows: Sequence[EvidenceRefRow]) -> List[DomainEvidenceRef]:
    """Map ordered ORM evidence rows to domain evidence refs."""
    ordered = sorted(rows, key=lambda r: r.position)
    return [evidence_row_to_domain(r) for r in ordered]


# ---------------------------------------------------------------------------
# Memory item
# ---------------------------------------------------------------------------


def memory_row_to_domain(
    row: MemoryItemRow,
    evidence_rows: Sequence[EvidenceRefRow],
) -> DomainMemoryItem:
    """Build a complete domain ``MemoryItem`` from ORM rows."""
    return DomainMemoryItem(
        id=row.id,
        content=row.content,
        evidence_refs=evidence_rows_to_domain(evidence_rows),
        provenance=dict_to_provenance(row.provenance),
        status=MemoryItemStatus(row.status),
        created_at=_strip_to_naive_utc(row.created_at),
        updated_at=_strip_to_naive_utc(row.updated_at),
        expires_at=(
            _strip_to_naive_utc(row.expires_at) if row.expires_at else None
        ),
        metadata=dict(row.metadata_) if row.metadata_ else {},
        project_id=row.project_id,
        session_id=row.session_id,
        agent_id=row.agent_id,
        knowledge_base_id=row.knowledge_base_id,
    )
