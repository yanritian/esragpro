"""Typed projection metadata and target commands (P6-E).

Immutable contracts emitted by the world-memory handler boundary. A
:class:`ProjectionMetadata` captures the event-scoped identity and
idempotency fields from :class:`ProjectionEvent`. Target-specific
commands (:class:`ElasticsearchCommand`, :class:`Neo4jCommand`) pair
that metadata with the corresponding P6-D payload.

Commands are frozen and slots-based. The ``payload`` field holds the
accepted P6-D typed payload — never the raw envelope.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from ..projection_worker.contracts import ProjectionEvent
from .payloads import ElasticsearchPayload, Neo4jPayload


@dataclass(frozen=True, slots=True)
class ProjectionMetadata:
    """Immutable projection-event metadata carried by every command.

    Mirrors the identity and idempotency fields of
    :class:`ProjectionEvent`. Handlers derive this from the incoming
    event; writers must not modify it.
    """

    outbox_id: UUID
    aggregate_id: UUID
    event_type: str
    schema_version: int
    projection_target: str
    idempotency_key: str
    authority_envelope_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ElasticsearchCommand:
    """Typed command for the Elasticsearch writer.

    Pairs :class:`ProjectionMetadata` with the P6-D
    :class:`ElasticsearchPayload`. The writer must use
    ``metadata.idempotency_key`` (or ``payload.id``) for upsert
    deduplication.
    """

    metadata: ProjectionMetadata
    payload: ElasticsearchPayload
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Neo4jCommand:
    """Typed command for the Neo4j writer.

    Pairs :class:`ProjectionMetadata` with the P6-D
    :class:`Neo4jPayload`. The writer must honour
    ``metadata.idempotency_key`` when composing transactional writes.
    """

    metadata: ProjectionMetadata
    payload: Neo4jPayload


def metadata_from_event(event: ProjectionEvent) -> ProjectionMetadata:
    """Build :class:`ProjectionMetadata` from a :class:`ProjectionEvent`.

    Copies the six identity / idempotency fields verbatim. The source
    event is never mutated.
    """
    return ProjectionMetadata(
        outbox_id=event.outbox_id,
        aggregate_id=event.aggregate_id,
        event_type=event.event_type,
        schema_version=event.schema_version,
        projection_target=event.projection_target,
        idempotency_key=event.idempotency_key,
        authority_envelope_id=event.authority_envelope_id,
    )


__all__ = [
    "ElasticsearchCommand",
    "Neo4jCommand",
    "ProjectionMetadata",
    "metadata_from_event",
]
