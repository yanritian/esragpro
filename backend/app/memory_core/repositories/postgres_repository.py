"""PostgreSQL implementation of ``UnpermissionedMemoryRepositoryPort`` (P1-02).

The repository receives an external ``AsyncSession`` and never commits or
rolls back. Callers own the transaction boundary so that P1-03 can
atomically write a memory row and its outbox event.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.memory_plane import (
    EvidenceRef as EvidenceRefRow,
    MemoryItem as MemoryItemRow,
)
from app.memory_core.repositories.postgres_filters import (
    MemoryQueryFilters,
    build_memory_predicates,
)
from app.memory_core.repositories.postgres_mapping import (
    _ensure_utc_aware,
    domain_to_evidence_row,
    memory_row_to_domain,
    provenance_to_dict,
)
from app.memory_core.repositories.postgres_outbox import (
    enqueue_remembered_events,
    validate_projection_targets,
)
from app.memory_core.unpermissioned_contracts import (
    EvidenceRef,
    MemoryItem,
    MemoryItemStatus,
    ProvenanceInfo,
)
from app.memory_core.unpermissioned_repository import (
    UnpermissionedMemoryRepositoryPort,
)


class PostgresMemoryRepository(UnpermissionedMemoryRepositoryPort):
    """Async PostgreSQL adapter implementing the unpermissioned port.

    Mutations ``flush`` but never ``commit`` or ``rollback``. The caller
    owns the transaction boundary. ``remember()`` also appends one
    ``memory_projection_outbox`` row per projection target inside the
    same caller-owned transaction.
    """

    def __init__(
        self,
        session: AsyncSession,
        projection_targets: Sequence[str] | None = None,
    ) -> None:
        self._session = session
        if projection_targets is None:
            self._projection_targets: tuple[str, ...] | None = None
        else:
            self._projection_targets = validate_projection_targets(
                projection_targets
            )

    async def remember(
        self,
        content: str,
        evidence_refs: List[EvidenceRef],
        provenance: Optional[ProvenanceInfo] = None,
        project_id: Optional[UUID] = None,
        session_id: Optional[UUID] = None,
        agent_id: Optional[UUID] = None,
        knowledge_base_id: Optional[UUID] = None,
        expires_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryItem:
        row = MemoryItemRow(
            content=content,
            status=MemoryItemStatus.ACTIVE.value,
            provenance=provenance_to_dict(provenance),
            project_id=project_id,
            session_id=session_id,
            agent_id=agent_id,
            knowledge_base_id=knowledge_base_id,
            expires_at=_ensure_utc_aware(expires_at),
            metadata_=dict(metadata) if metadata else {},
        )
        self._session.add(row)
        await self._session.flush()

        evidence_rows: List[EvidenceRefRow] = []
        for position, evidence in enumerate(evidence_refs):
            ev_row = domain_to_evidence_row(row.id, position, evidence)
            self._session.add(ev_row)
            evidence_rows.append(ev_row)
        if evidence_rows:
            await self._session.flush()

        await self._session.refresh(row)
        loaded_evidence: List[EvidenceRefRow] = []
        if evidence_rows:
            result = await self._session.execute(
                select(EvidenceRefRow)
                .where(EvidenceRefRow.memory_item_id == row.id)
                .order_by(EvidenceRefRow.position)
            )
            loaded_evidence = list(result.scalars().all())

        domain_item = memory_row_to_domain(row, loaded_evidence)
        # Enqueue projection events inside the same caller-owned
        # transaction. Any failure here propagates so the caller can
        # roll back memory + evidence + outbox atomically.
        await enqueue_remembered_events(
            self._session,
            memory_item=domain_item,
            targets=self._projection_targets,
        )
        return domain_item

    async def get_by_id(self, memory_id: UUID) -> Optional[MemoryItem]:
        result = await self._session.execute(
            select(MemoryItemRow).where(MemoryItemRow.id == memory_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        ev_result = await self._session.execute(
            select(EvidenceRefRow)
            .where(EvidenceRefRow.memory_item_id == row.id)
            .order_by(EvidenceRefRow.position)
        )
        return memory_row_to_domain(row, list(ev_result.scalars().all()))

    async def query(
        self,
        query_text: Optional[str] = None,
        project_id: Optional[UUID] = None,
        session_id: Optional[UUID] = None,
        agent_id: Optional[UUID] = None,
        knowledge_base_id: Optional[UUID] = None,
        status: Optional[MemoryItemStatus] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        require_evidence: bool = False,
        min_confidence: float = 0.0,
        max_items: int = 100,
    ) -> List[MemoryItem]:
        if max_items <= 0:
            return []

        filters = MemoryQueryFilters(
            project_id=project_id,
            session_id=session_id,
            agent_id=agent_id,
            knowledge_base_id=knowledge_base_id,
            status=status,
            start_time=start_time,
            end_time=end_time,
            query_text=query_text,
            require_evidence=require_evidence,
            min_confidence=min_confidence,
        )
        predicates = build_memory_predicates(filters)
        stmt = (
            select(MemoryItemRow)
            .where(*predicates)
            .order_by(MemoryItemRow.created_at.desc(), MemoryItemRow.id.asc())
            .limit(max_items)
        )

        result = await self._session.execute(stmt)
        rows = list(result.scalars().all())
        if not rows:
            return []
        return await self._hydrate(rows)

    async def update_status(
        self,
        memory_id: UUID,
        status: MemoryItemStatus,
    ) -> Optional[MemoryItem]:
        result = await self._session.execute(
            select(MemoryItemRow).where(MemoryItemRow.id == memory_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.status = status.value
        # Database-generated timestamp, not Python wall-clock.
        row.updated_at = func.now()
        await self._session.flush()
        await self._session.refresh(row)

        ev_result = await self._session.execute(
            select(EvidenceRefRow)
            .where(EvidenceRefRow.memory_item_id == row.id)
            .order_by(EvidenceRefRow.position)
        )
        return memory_row_to_domain(row, list(ev_result.scalars().all()))

    async def count(
        self,
        project_id: Optional[UUID] = None,
        session_id: Optional[UUID] = None,
        agent_id: Optional[UUID] = None,
        knowledge_base_id: Optional[UUID] = None,
        status: Optional[MemoryItemStatus] = None,
    ) -> int:
        filters = MemoryQueryFilters(
            project_id=project_id,
            session_id=session_id,
            agent_id=agent_id,
            knowledge_base_id=knowledge_base_id,
            status=status,
        )
        predicates = build_memory_predicates(filters)
        stmt = (
            select(func.count())
            .select_from(MemoryItemRow)
            .where(*predicates)
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def _hydrate(
        self, rows: List[MemoryItemRow]
    ) -> List[MemoryItem]:
        ids = [row.id for row in rows]
        if not ids:
            return []
        ev_result = await self._session.execute(
            select(EvidenceRefRow)
            .where(EvidenceRefRow.memory_item_id.in_(ids))
            .order_by(EvidenceRefRow.memory_item_id, EvidenceRefRow.position)
        )
        evidence_by_item: Dict[UUID, List[EvidenceRefRow]] = {}
        for ev in ev_result.scalars().all():
            evidence_by_item.setdefault(ev.memory_item_id, []).append(ev)
        return [
            memory_row_to_domain(row, evidence_by_item.get(row.id, []))
            for row in rows
        ]
