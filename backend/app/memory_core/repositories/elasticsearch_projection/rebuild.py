"""Bounded PostgreSQL scan and idempotent rebuild counters (P1-06).

Reads all memory snapshots from PostgreSQL in bounded pages and re-uses
the Elasticsearch upsert path with the same deterministic idempotency
keys. Does NOT create outbox rows. Elasticsearch is a durable projection
of the full PostgreSQL state; search-time filtering decides what callers
see.

Requires an embedding provider to generate vectors for the content.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.memory_plane import (
    EvidenceRef as EvidenceRefRow,
    MemoryItem as MemoryItemRow,
)
from app.memory_core.repositories.elasticsearch_projection.index import (
    ElasticsearchProjectionIndex,
    EmbeddingProvider,
    is_retryable_es_error,
)
from app.memory_core.repositories.postgres_mapping import memory_row_to_domain
from app.memory_core.repositories.postgres_outbox import build_idempotency_key

logger = logging.getLogger(__name__)

_REBUILD_PAGE_SIZE = 200


class ElasticsearchRebuildInfrastructureError(Exception):
    """Elasticsearch infrastructure failure during rebuild."""


@dataclass
class ElasticsearchRebuildCounters:
    """Counters for an Elasticsearch rebuild run."""

    scanned: int = 0
    projected: int = 0
    skipped_expired: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def record_error(self, memory_id: UUID, label: str) -> None:
        self.failed += 1
        if len(self.errors) < 20:
            self.errors.append(f"{memory_id}:{label}")


async def _hydrate_evidence_batch(
    session: AsyncSession,
    memory_ids: list[UUID],
) -> dict[UUID, list[EvidenceRefRow]]:
    """Hydrate evidence refs for a batch of memory IDs in one query."""
    if not memory_ids:
        return {}
    stmt = (
        select(EvidenceRefRow)
        .where(EvidenceRefRow.memory_item_id.in_(memory_ids))
        .order_by(EvidenceRefRow.memory_item_id, EvidenceRefRow.position)
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    grouped: dict[UUID, list[EvidenceRefRow]] = {mid: [] for mid in memory_ids}
    for r in rows:
        grouped[r.memory_item_id].append(r)
    return grouped


def _serialize_evidence(evidence_rows: list[EvidenceRefRow]) -> list[dict[str, Any]]:
    """Serialize evidence rows to ES-compatible dicts."""
    result = []
    for r in evidence_rows:
        ts = r.timestamp
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        result.append(
            {
                "source_id": r.source_id,
                "source_type": r.source_type,
                "timestamp": ts.isoformat() if ts else None,
                "chunk_id": r.chunk_id,
                "confidence": r.confidence,
                "metadata": dict(r.metadata_) if r.metadata_ else {},
            }
        )
    return result


async def rebuild_from_postgres(
    *,
    session: AsyncSession,
    index: ElasticsearchProjectionIndex,
    embedding_provider: EmbeddingProvider,
    page_size: int = _REBUILD_PAGE_SIZE,
) -> ElasticsearchRebuildCounters:
    """Rebuild Elasticsearch index from PostgreSQL.

    Reads ALL memory_items in stable bounded pages, regardless of status
    or expiry. Elasticsearch is a durable projection of the full
    PostgreSQL state; the search layer (``SearchFilters``) decides what
    is returned to callers. Does not create outbox rows. Does not clear
    the index first.
    """
    if page_size <= 0:
        raise ValueError("page_size must be positive")

    counters = ElasticsearchRebuildCounters()
    last_id: Optional[UUID] = None

    while True:
        predicates: list[Any] = []
        if last_id is not None:
            predicates.append(MemoryItemRow.id > last_id)

        stmt = select(MemoryItemRow)
        if predicates:
            stmt = stmt.where(and_(*predicates))
        stmt = stmt.order_by(MemoryItemRow.id.asc()).limit(page_size)
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
        if not rows:
            break

        memory_ids = [r.id for r in rows]
        evidence_map = await _hydrate_evidence_batch(session, memory_ids)

        for row in rows:
            counters.scanned += 1
            last_id = row.id

            try:
                ev_rows = evidence_map.get(row.id, [])
                domain = memory_row_to_domain(row, ev_rows)
                embedding = await embedding_provider.embed(row.content)
                idem_key = build_idempotency_key(domain.id, "elasticsearch")

                created_at = row.created_at
                if created_at and created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                updated_at = row.updated_at
                if updated_at and updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                expires_at = row.expires_at
                if expires_at and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)

                await index.upsert(
                    memory_id=str(domain.id),
                    content=domain.content,
                    embedding=embedding,
                    status=domain.status.value,
                    created_at=created_at.isoformat() if created_at else None,
                    updated_at=updated_at.isoformat() if updated_at else None,
                    expires_at=expires_at.isoformat() if expires_at else None,
                    project_id=str(domain.project_id) if domain.project_id else None,
                    session_id=str(domain.session_id) if domain.session_id else None,
                    agent_id=str(domain.agent_id) if domain.agent_id else None,
                    knowledge_base_id=(
                        str(domain.knowledge_base_id) if domain.knowledge_base_id else None
                    ),
                    event_type="memory.remembered",
                    schema_version=1,
                    idempotency_key=idem_key,
                    metadata=dict(domain.metadata) if domain.metadata else {},
                    provenance=(
                        {
                            "source_type": domain.provenance.source_type,
                            "source_name": domain.provenance.source_name,
                            "source_uri": domain.provenance.source_uri,
                            "ingestion_job_id": (
                                str(domain.provenance.ingestion_job_id)
                                if domain.provenance.ingestion_job_id
                                else None
                            ),
                            "version": domain.provenance.version,
                            "extracted_at": (
                                domain.provenance.extracted_at.isoformat()
                                if domain.provenance.extracted_at
                                else None
                            ),
                            "extracted_by": domain.provenance.extracted_by,
                        }
                        if domain.provenance
                        else None
                    ),
                    evidence_refs=_serialize_evidence(ev_rows),
                )
                counters.projected += 1
            except Exception as exc:
                if is_retryable_es_error(exc):
                    raise ElasticsearchRebuildInfrastructureError(
                        "elasticsearch rebuild: infrastructure failure",
                    ) from exc
                label = exc.__class__.__name__
                counters.record_error(row.id, label)
                logger.warning(
                    "elasticsearch rebuild: failed for item: %s",
                    label,
                )

        if len(rows) < page_size:
            break

    return counters


__all__ = [
    "ElasticsearchRebuildCounters",
    "ElasticsearchRebuildInfrastructureError",
    "rebuild_from_postgres",
]
