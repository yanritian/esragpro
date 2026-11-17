"""Deterministic PostgreSQL-to-Redis rebuild service (P1-05).

Reads active, unexpired memory snapshots from PostgreSQL in bounded
pages and re-uses the same Redis projection core with the same
deterministic idempotency keys. Does NOT create outbox rows.

Evidence is hydrated in bounded per-page queries (not per-row).
Partial Redis loss is repairable: if item HASHes remain but session
ZSETs are missing, rebuild restores the session index without extending
item TTL (the writer Lua handles this).
Redis infrastructure failure aborts as RebuildInfrastructureError, not
silently into per-item counters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from redis.exceptions import ResponseError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.memory_plane import (
    EvidenceRef as EvidenceRefRow,
    MemoryItem as MemoryItemRow,
)
from app.memory_core.repositories.postgres_mapping import memory_row_to_domain
from app.memory_core.repositories.postgres_outbox import (
    build_event_payload,
    build_idempotency_key,
)
from app.memory_core.repositories.redis_hot.writer import RedisHotStateWriter

logger = logging.getLogger(__name__)

_REBUILD_PAGE_SIZE = 200


class RebuildInfrastructureError(Exception):
    """Redis infrastructure failure during rebuild."""


@dataclass
class RebuildCounters:
    scanned: int = 0
    projected: int = 0
    skipped_expired: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def record_error(self, memory_id: UUID, label: str) -> None:
        self.failed += 1
        if len(self.errors) < 20:
            self.errors.append(f"{memory_id}:{label}")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _is_infra_error(exc: BaseException) -> bool:
    """Return True if the exception is a Redis infrastructure error.

    Classification order:
    1. Connection/timeout/OSError — Python builtins for transport-layer
       failures.
    2. Any redis-py ``ResponseError`` subclass — server-side operational
       failures (NoScriptError, ExecAbortError, ClusterDownError,
       TryAgainError, ReadOnlyError, OutOfMemoryError, MasterDownError,
       NoPermissionError, ModuleError, AskError, MovedError, etc.).
       These are infrastructure errors, not permanent payload errors.
    3. redis-py ``ConnectionError`` (shadows the builtin) and its
       subclasses — AuthenticationError, AuthorizationError,
       BusyLoadingError, MaxConnectionsError, ExternalAuthProviderError.
       These are transport-layer failures from the Redis client.
    4. redis-py ``TimeoutError`` (shadows the builtin) — network timeout.
    5. Name-based fallback for any redis-py exception whose name signals
       a transport issue (Connection*, Timeout*, Busy*, Loading*, Pool*).

    Permanent payload errors (``PermanentProjectionError``, ``ValueError``,
    etc.) are NOT infrastructure errors.
    """
    # 1. Python builtins — transport-layer failures.
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    # 2. Any redis-py ResponseError subclass (the canonical server error
    #    base class — covers NoScriptError, ExecAbortError, ClusterDownError,
    #    TryAgainError, ReadOnlyError, OutOfMemoryError, MasterDownError,
    #    NoPermissionError, ModuleError, AskError, MovedError, and any
    #    future ResponseError subclass).
    if isinstance(exc, ResponseError):
        return True
    # 3. redis-py ConnectionError (shadows builtin) and subclasses:
    #    AuthenticationError, AuthorizationError, BusyLoadingError,
    #    MaxConnectionsError, ExternalAuthProviderError. These are all
    #    transport-layer failures from the Redis client.
    if isinstance(exc, (RedisConnectionError, RedisTimeoutError)):
        return True
    # 4. Name-based fallback for any other redis-py transport exception
    #    not caught above (defensive; should be unreachable with the
    #    isinstance checks above, but retained for forward compatibility).
    mod = type(exc).__module__ or ""
    name = type(exc).__name__
    if "redis" in mod.lower() or "redis" in name.lower():
        if any(kw in name.lower() for kw in ("connection", "timeout", "busy", "loading", "pool")):
            return True
    return False


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


async def rebuild_from_postgres(
    *,
    session: AsyncSession,
    writer: RedisHotStateWriter,
    page_size: int = _REBUILD_PAGE_SIZE,
) -> RebuildCounters:
    """Rebuild Redis hot state from PostgreSQL.

    Reads active, unexpired memory_items in stable bounded pages.
    Hydrates evidence per-page in bounded queries.
    Does not create outbox rows. Does not clear Redis first.
    Returns scanned/projected/skipped/failed counters.
    Raises RebuildInfrastructureError on Redis infrastructure failures.
    """
    if page_size <= 0:
        raise ValueError("page_size must be positive")

    counters = RebuildCounters()
    now = _now_utc()
    last_id: Optional[UUID] = None

    while True:
        predicates = [
            MemoryItemRow.status == "active",
            or_(
                MemoryItemRow.expires_at.is_(None),
                MemoryItemRow.expires_at > now,
            ),
        ]
        if last_id is not None:
            predicates.append(MemoryItemRow.id > last_id)

        stmt = (
            select(MemoryItemRow)
            .where(and_(*predicates))
            .order_by(MemoryItemRow.id.asc())
            .limit(page_size)
        )
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
                payload = build_event_payload(
                    memory_item=domain,
                    projection_target="redis",
                )
                idem_key = build_idempotency_key(domain.id, "redis")
                outcome = await writer.project(
                    payload,
                    envelope_idempotency_key=idem_key,
                    envelope_aggregate_id=str(domain.id),
                    envelope_event_type="memory.remembered",
                    envelope_schema_version=1,
                    envelope_target="redis",
                )
                if outcome in ("written", "idempotent"):
                    counters.projected += 1
                elif outcome == "expired":
                    counters.skipped_expired += 1
            except Exception as exc:
                if _is_infra_error(exc):
                    raise RebuildInfrastructureError(
                        "redis rebuild: infrastructure failure",
                    ) from exc
                label = exc.__class__.__name__
                counters.record_error(row.id, label)
                logger.warning(
                    "redis rebuild: failed for item: %s",
                    label,
                )

        if len(rows) < page_size:
            break

    return counters


__all__ = ["RebuildCounters", "RebuildInfrastructureError", "rebuild_from_postgres"]
