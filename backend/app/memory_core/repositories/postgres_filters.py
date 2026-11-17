"""SQL predicate construction for P1-02 PostgreSQL repository.

Pure predicate builders: take typed filter inputs and return SQLAlchemy
boolean expressions. No I/O, no session, no engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.db.models.memory_plane import (
    EvidenceRef as EvidenceRefRow,
    MemoryItem as MemoryItemRow,
)
from app.memory_core.repositories.postgres_mapping import _ensure_utc_aware
from app.memory_core.unpermissioned_contracts import MemoryItemStatus


@dataclass(frozen=True, slots=True)
class MemoryQueryFilters:
    """Immutable typed filter inputs for memory queries."""

    project_id: Optional[UUID] = None
    session_id: Optional[UUID] = None
    agent_id: Optional[UUID] = None
    knowledge_base_id: Optional[UUID] = None
    status: Optional[MemoryItemStatus] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    query_text: Optional[str] = None
    require_evidence: bool = False
    min_confidence: float = 0.0


def _normalize_terms(query_text: Optional[str]) -> Optional[List[str]]:
    """Return whitespace-delimited lowered terms, or ``None`` when blank."""
    if query_text is None:
        return None
    stripped = query_text.strip()
    if not stripped:
        return None
    return stripped.lower().split()


def build_status_predicate(
    status: Optional[MemoryItemStatus],
) -> ColumnElement[bool]:
    """Return the status equality predicate (defaults to ACTIVE)."""
    effective = (
        status.value if status is not None else MemoryItemStatus.ACTIVE.value
    )
    return MemoryItemRow.status == effective


def build_expiration_predicate() -> ColumnElement[bool]:
    """Return the ``expires_at IS NULL OR expires_at > now`` predicate."""
    now_utc = datetime.now(timezone.utc)
    return or_(
        MemoryItemRow.expires_at.is_(None),
        MemoryItemRow.expires_at > now_utc,
    )


def build_dimension_predicates(
    filters: MemoryQueryFilters,
) -> List[ColumnElement[bool]]:
    """Return dimension equality predicates for non-``None`` filters."""
    predicates: List[ColumnElement[bool]] = []
    if filters.project_id is not None:
        predicates.append(MemoryItemRow.project_id == filters.project_id)
    if filters.session_id is not None:
        predicates.append(MemoryItemRow.session_id == filters.session_id)
    if filters.agent_id is not None:
        predicates.append(MemoryItemRow.agent_id == filters.agent_id)
    if filters.knowledge_base_id is not None:
        predicates.append(
            MemoryItemRow.knowledge_base_id == filters.knowledge_base_id
        )
    return predicates


def build_time_predicates(
    filters: MemoryQueryFilters,
) -> List[ColumnElement[bool]]:
    """Return ``created_at`` range predicates (start-inclusive, end-exclusive)."""
    predicates: List[ColumnElement[bool]] = []
    if filters.start_time is not None:
        predicates.append(
            MemoryItemRow.created_at >= _ensure_utc_aware(filters.start_time)
        )
    if filters.end_time is not None:
        predicates.append(
            MemoryItemRow.created_at < _ensure_utc_aware(filters.end_time)
        )
    return predicates


def build_text_predicates(
    filters: MemoryQueryFilters,
) -> List[ColumnElement[bool]]:
    """Return case-insensitive ``contains`` predicates for every query term."""
    predicates: List[ColumnElement[bool]] = []
    terms = _normalize_terms(filters.query_text)
    if terms is not None:
        lowered = func.lower(MemoryItemRow.content)
        for term in terms:
            predicates.append(lowered.contains(term))
    return predicates


def build_evidence_predicates(
    filters: MemoryQueryFilters,
) -> List[ColumnElement[bool]]:
    """Return ``EXISTS`` predicates for evidence and confidence filters."""
    predicates: List[ColumnElement[bool]] = []
    if filters.require_evidence:
        predicates.append(
            select(EvidenceRefRow.memory_item_id)
            .where(EvidenceRefRow.memory_item_id == MemoryItemRow.id)
            .exists()
        )
    if filters.min_confidence > 0.0:
        predicates.append(
            select(EvidenceRefRow.memory_item_id)
            .where(EvidenceRefRow.memory_item_id == MemoryItemRow.id)
            .where(EvidenceRefRow.confidence >= filters.min_confidence)
            .exists()
        )
    return predicates


def build_memory_predicates(
    filters: MemoryQueryFilters,
) -> List[ColumnElement[bool]]:
    """Return the complete ``WHERE`` predicate list for a memory query."""
    predicates: List[ColumnElement[bool]] = [
        build_status_predicate(filters.status),
        build_expiration_predicate(),
    ]
    predicates.extend(build_dimension_predicates(filters))
    predicates.extend(build_time_predicates(filters))
    predicates.extend(build_text_predicates(filters))
    predicates.extend(build_evidence_predicates(filters))
    return predicates
