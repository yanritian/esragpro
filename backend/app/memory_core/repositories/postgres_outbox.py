"""PostgreSQL outbox helper for P1-03 memory projection events.

Adds one ``MemoryProjectionOutbox`` row per projection target inside the
caller-owned ``AsyncSession``. The helper never commits, rolls back, or
creates an engine. Callers retain full transaction control.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.memory_plane import (
    MemoryProjectionOutbox as OutboxRow,
)
from app.memory_core.unpermissioned_contracts import (
    EvidenceRef,
    MemoryItem,
    MemoryItemStatus,
    ProvenanceInfo,
)

EVENT_TYPE_REMEMBERED = "memory.remembered"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "memory_item"

DEFAULT_PROJECTION_TARGETS: tuple[str, ...] = ("redis", "elasticsearch", "neo4j", "minio")

_TARGET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")
_IDEMPOTENCY_TEMPLATE = "memory:{aggregate_id}:remembered:v1:{projection_target}"


def validate_projection_targets(targets: Sequence[str]) -> tuple[str, ...]:
    """Validate and return an immutable sequence of projection targets.

    Rejects blank, duplicate, unsafe, or over-50-character names while
    allowing any lowercase-alphanumeric target that fits the pattern.
    """
    if not isinstance(targets, Sequence) or isinstance(targets, str):
        raise TypeError("targets must be a sequence of strings")
    if len(targets) == 0:
        raise ValueError("targets must contain at least one entry")
    seen: set[str] = set()
    validated: list[str] = []
    for target in targets:
        if not isinstance(target, str):
            raise TypeError("target entries must be strings")
        stripped = target.strip()
        if not stripped:
            raise ValueError("target must not be blank")
        if not _TARGET_PATTERN.match(stripped):
            raise ValueError(f"invalid projection target: {target!r}")
        if stripped in seen:
            raise ValueError(f"duplicate projection target: {stripped!r}")
        seen.add(stripped)
        validated.append(stripped)
    return tuple(validated)


def _format_utc_iso(value: datetime | None) -> str | None:
    """Return an explicit-UTC ISO-8601 string, or ``None`` for null inputs."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


def _format_uuid(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _copy_evidence(evidence: Sequence[EvidenceRef]) -> list[dict[str, Any]]:
    """Serialize evidence refs in deterministic position order.

    The input is already ordered by position by the caller; this function
    copies each field without mutating the source.
    """
    result: list[dict[str, Any]] = []
    for ref in evidence:
        result.append(
            {
                "source_id": ref.source_id,
                "source_type": ref.source_type,
                "timestamp": _format_utc_iso(ref.timestamp),
                "chunk_id": ref.chunk_id,
                "confidence": ref.confidence,
                "metadata": dict(ref.metadata) if ref.metadata else {},
            }
        )
    return result


def _copy_provenance(provenance: ProvenanceInfo | None) -> dict[str, Any] | None:
    if provenance is None:
        return None
    return {
        "source_type": provenance.source_type,
        "source_name": provenance.source_name,
        "source_uri": provenance.source_uri,
        "ingestion_job_id": _format_uuid(provenance.ingestion_job_id),
        "version": provenance.version,
        "extracted_at": _format_utc_iso(provenance.extracted_at),
        "extracted_by": provenance.extracted_by,
    }


def build_event_payload(
    *,
    memory_item: MemoryItem,
    projection_target: str,
) -> dict[str, Any]:
    """Construct a versioned, JSON-safe projection event payload.

    UUIDs serialize as strings, datetimes as explicit-UTC ISO-8601, and
    optional values remain JSON null. Caller-owned dictionaries and
    evidence sequences are copied, never mutated.
    """
    snapshot: dict[str, Any] = {
        "id": _format_uuid(memory_item.id),
        "content": memory_item.content,
        "status": (
            memory_item.status.value
            if isinstance(memory_item.status, MemoryItemStatus)
            else str(memory_item.status)
        ),
        "created_at": _format_utc_iso(memory_item.created_at),
        "updated_at": _format_utc_iso(memory_item.updated_at),
        "expires_at": _format_utc_iso(memory_item.expires_at),
        "project_id": _format_uuid(memory_item.project_id),
        "session_id": _format_uuid(memory_item.session_id),
        "agent_id": _format_uuid(memory_item.agent_id),
        "knowledge_base_id": _format_uuid(memory_item.knowledge_base_id),
        "provenance": _copy_provenance(memory_item.provenance),
        "metadata": dict(memory_item.metadata) if memory_item.metadata else {},
        "evidence_refs": _copy_evidence(memory_item.evidence_refs),
    }
    return {
        "event_type": EVENT_TYPE_REMEMBERED,
        "schema_version": SCHEMA_VERSION,
        "aggregate_type": AGGREGATE_TYPE,
        "aggregate_id": _format_uuid(memory_item.id),
        "projection_target": projection_target,
        "occurred_at": _format_utc_iso(memory_item.created_at),
        "memory_snapshot": snapshot,
    }


def build_idempotency_key(
    aggregate_id: UUID,
    projection_target: str,
) -> str:
    """Deterministic per-target idempotency key."""
    return _IDEMPOTENCY_TEMPLATE.format(
        aggregate_id=str(aggregate_id),
        projection_target=projection_target,
    )


async def enqueue_remembered_events(
    session: AsyncSession,
    *,
    memory_item: MemoryItem,
    targets: Sequence[str] | None = None,
) -> list[OutboxRow]:
    """Add one outbox row per target inside the caller-owned session.

    Flushes the new rows so they participate in the same transactional
    boundary as the memory snapshot. Never commits or rolls back.
    """
    resolved_targets = (
        DEFAULT_PROJECTION_TARGETS if targets is None else validate_projection_targets(targets)
    )
    rows: list[OutboxRow] = []
    for target in resolved_targets:
        payload = build_event_payload(
            memory_item=memory_item,
            projection_target=target,
        )
        row = OutboxRow(
            id=uuid4(),
            aggregate_id=memory_item.id,
            event_type=EVENT_TYPE_REMEMBERED,
            schema_version=SCHEMA_VERSION,
            projection_target=target,
            idempotency_key=build_idempotency_key(memory_item.id, target),
            payload=payload,
            state="pending",
            attempt_count=0,
            max_attempts=8,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


__all__ = [
    "AGGREGATE_TYPE",
    "DEFAULT_PROJECTION_TARGETS",
    "EVENT_TYPE_REMEMBERED",
    "SCHEMA_VERSION",
    "build_event_payload",
    "build_idempotency_key",
    "enqueue_remembered_events",
    "validate_projection_targets",
]
