"""World-memory projection outbox repository (P6-B).

Derives per-target projection rows from a committed P6-A authority
envelope, maps them to the existing ``ProjectionEvent`` contract, and
provides minimal claim/lease/complete/retry/dead-letter transitions for
a future worker — without calling ES, Neo4j, Redis, or MinIO itself.

Transaction contract
--------------------
All methods flush but never commit or roll back. Callers own the
transaction boundary so that a rollback leaves neither the authority
row nor any projection row.

Idempotency
-----------
The database unique constraint on ``idempotency_key`` is the final
guard. An application-side pre-check performs exact-duplicate fast
path and early conflict detection. Conflicting reuse of the same key
raises :class:`WorldMemoryProjectionConflict`.

Fan-out
-------
Each call to :meth:`enqueue_for_targets` creates one row per target.
Targets must be validated and unique. The deterministic idempotency
key pattern is ``world-projection:{authority_id}:v{version}:{target}``.

Legacy isolation
----------------
This module never imports, references, or writes to
``memory_projection_outbox`` or ``MemoryItem``. The two outbox tables
are fully independent.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID, uuid5

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.world_memory import (
    WorldMemoryEnvelope,
)
from app.db.models.world_memory import (
    WorldMemoryProjectionOutbox as ProjectionOutboxRow,
)
from app.memory_core.projection_worker.contracts import ProjectionEvent

# Namespace UUID for deterministic idempotency keys (RFC 4122 namespace).
_PROJECTION_NAMESPACE = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")

# Idempotency key template — deterministic per (authority_id, version, target).
_IDEMPOTENCY_TEMPLATE = (
    "world-projection:{authority_id}:v{version}:{target}"
)

# Projection target validation — same grammar as P1-03.
_TARGET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")

# Default projection targets for world-memory envelopes.
DEFAULT_WORLD_PROJECTION_TARGETS: tuple[str, ...] = (
    "elasticsearch",
    "neo4j",
)

# Maximum lease duration cap to prevent unbounded leases.
_MAX_LEASE_DURATION = timedelta(hours=1)


class WorldMemoryProjectionConflict(Exception):
    """Raised when a projection idempotency key is reused with a different payload."""

    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(
            f"Projection idempotency key {idempotency_key!r} reused with conflicting payload"
        )


@dataclass(frozen=True)
class StoredProjectionRow:
    """A persisted projection outbox row with full envelope payload."""

    id: UUID
    authority_envelope_id: UUID
    event_type: str
    schema_version: int
    aggregate_id: UUID
    projection_target: str
    idempotency_key: str
    envelope: dict[str, Any]
    state: str
    attempt_count: int
    max_attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    processed_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


def _compute_idempotency_key(
    authority_id: UUID,
    schema_version: int,
    target: str,
) -> str:
    """Deterministic idempotency key for a projection row."""
    return _IDEMPOTENCY_TEMPLATE.format(
        authority_id=str(authority_id),
        version=schema_version,
        target=target,
    )


def _compute_deterministic_id(
    authority_id: UUID,
    schema_version: int,
    target: str,
) -> UUID:
    """Deterministic UUID5 for a projection row (for application-side pre-check)."""
    key = _compute_idempotency_key(authority_id, schema_version, target)
    return uuid5(_PROJECTION_NAMESPACE, key)


def validate_targets(targets: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Validate projection targets: non-empty, unique, grammar-checked."""
    if not targets:
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


def _row_to_stored(row: ProjectionOutboxRow) -> StoredProjectionRow:
    """Map an ORM row to a :class:`StoredProjectionRow`."""
    return StoredProjectionRow(
        id=row.id,
        authority_envelope_id=row.authority_envelope_id,
        event_type=row.event_type,
        schema_version=row.schema_version,
        aggregate_id=row.aggregate_id,
        projection_target=row.projection_target,
        idempotency_key=row.idempotency_key,
        envelope=dict(row.envelope),
        state=row.state,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        available_at=row.available_at,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        processed_at=row.processed_at,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _payloads_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Canonical-JSON equality for envelope comparison."""
    return _canonicalize(a) == _canonicalize(b)


def _canonicalize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _canonicalize(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_canonicalize(item) for item in obj]
    return obj


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _rowcount(result: object) -> int:
    """Extract rowcount from a SQLAlchemy CursorResult.

    Runtime-boundary helper: the base ``Result`` type stub omits
    ``rowcount`` which only exists on ``CursorResult`` after DML
    statements.  Narrow via ``isinstance`` so type-checkers see the
    correct attribute without suppressions.
    """
    if isinstance(result, CursorResult):
        return result.rowcount or 0
    return 0


class WorldMemoryProjectionOutboxRepository:
    """Async repository for the world-memory projection outbox (P6-B).

    All methods flush but never commit or roll back. Callers own the
    transaction boundary.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def enqueue_for_targets(
        self,
        authority_row: WorldMemoryEnvelope,
        *,
        targets: tuple[str, ...] | list[str],
    ) -> list[StoredProjectionRow]:
        """Create one projection row per target from an authority envelope.

        The authority row must already be flushed to the session (so its
        ``id`` is assigned). The envelope payload stored in each
        projection row is the complete validated envelope from the
        authority — replay-safe and self-contained.

        Exact idempotent enqueue (same key, same payload) returns the
        existing row. Conflicting reuse raises
        :class:`WorldMemoryProjectionConflict`.

        Flushes but never commits.
        """
        validated_targets = validate_targets(targets)
        envelope_payload = dict(authority_row.envelope)
        stored_rows: list[StoredProjectionRow] = []

        for target in validated_targets:
            idem_key = _compute_idempotency_key(
                authority_row.id,
                authority_row.schema_version,
                target,
            )
            deterministic_id = _compute_deterministic_id(
                authority_row.id,
                authority_row.schema_version,
                target,
            )

            # Application-side pre-check
            existing = await self._lookup_by_key(idem_key)
            if existing is not None:
                if _payloads_equal(existing.envelope, envelope_payload):
                    stored_rows.append(existing)
                    continue
                raise WorldMemoryProjectionConflict(idem_key)

            row = ProjectionOutboxRow(
                id=deterministic_id,
                authority_envelope_id=authority_row.id,
                event_type=authority_row.event_type,
                schema_version=authority_row.schema_version,
                aggregate_id=authority_row.aggregate_id,
                projection_target=target,
                idempotency_key=idem_key,
                envelope=copy.deepcopy(envelope_payload),
                state="pending",
                attempt_count=0,
                max_attempts=8,
            )
            self._session.add(row)
            try:
                await self._session.flush()
            except IntegrityError:
                raise WorldMemoryProjectionConflict(idem_key) from None
            await self._session.refresh(row)
            stored_rows.append(_row_to_stored(row))

        return stored_rows

    async def enqueue_single(
        self,
        authority_row: WorldMemoryEnvelope,
        *,
        target: str,
    ) -> StoredProjectionRow:
        """Convenience: enqueue for a single target."""
        rows = await self.enqueue_for_targets(
            authority_row, targets=[target]
        )
        return rows[0]

    # ------------------------------------------------------------------
    # ProjectionEvent mapping
    # ------------------------------------------------------------------

    def map_to_projection_event(
        self,
        row: StoredProjectionRow,
    ) -> ProjectionEvent:
        """Map a stored projection row to an immutable ProjectionEvent.

        Uses the P8-A ``map_to_projection_event`` logic inlined here to
        avoid circular imports. The payload is the full envelope dict
        so downstream handlers can extract world-memory snapshot data.
        """
        payload = copy.deepcopy(row.envelope)
        return ProjectionEvent(
            outbox_id=row.id,
            aggregate_id=row.aggregate_id,
            event_type=row.event_type,
            schema_version=row.schema_version,
            projection_target=row.projection_target,
            idempotency_key=row.idempotency_key,
            payload=payload,
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
            authority_envelope_id=row.authority_envelope_id,
        )

    # ------------------------------------------------------------------
    # Claim / Lease
    # ------------------------------------------------------------------

    async def claim_batch(
        self,
        *,
        worker_identity: str,
        batch_size: int = 10,
        lease_duration: timedelta = timedelta(seconds=30),
        target_filter: str | None = None,
    ) -> list[StoredProjectionRow]:
        """Claim pending or lease-expired rows for processing.

        Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` to safely claim rows
        in concurrent worker scenarios. Transitions claimed rows to
        ``processing`` with a fencing token (``worker_identity:uuid``)
        and lease expiry.

        Flushes but never commits.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if lease_duration > _MAX_LEASE_DURATION:
            raise ValueError(
                f"lease_duration exceeds maximum of {_MAX_LEASE_DURATION}"
            )

        now = _now_utc()

        # First: recover lease-expired processing rows back to pending
        await self._recover_expired_leases(now)

        stmt = select(ProjectionOutboxRow).where(
            ProjectionOutboxRow.state == "pending",
            ProjectionOutboxRow.available_at <= now,
            ProjectionOutboxRow.attempt_count < ProjectionOutboxRow.max_attempts,
        )
        if target_filter is not None:
            stmt = stmt.where(
                ProjectionOutboxRow.projection_target == target_filter
            )
        stmt = stmt.order_by(
            ProjectionOutboxRow.available_at.asc(),
            ProjectionOutboxRow.created_at.asc(),
            ProjectionOutboxRow.id.asc(),
        ).limit(batch_size).with_for_update(skip_locked=True)

        result = await self._session.execute(stmt)
        rows = list(result.scalars().all())

        claimed: list[StoredProjectionRow] = []
        for row in rows:
            import uuid as _uuid_mod
            fencing_token = f"{worker_identity}:{_uuid_mod.uuid4()}"
            row.state = "processing"
            row.lease_owner = fencing_token
            row.lease_expires_at = now + lease_duration
            row.attempt_count += 1
            row.updated_at = now
            claimed.append(_row_to_stored(row))

        if claimed:
            await self._session.flush()
        return claimed

    async def _recover_expired_leases(self, now: datetime) -> int:
        """Reset lease-expired processing rows back to pending.

        Exhausted rows (attempt_count >= max_attempts) are dead-lettered.
        Also recovers legacy/stale pending rows that are exhausted to
        dead_letter to prevent permanent unclaimable state.

        Returns the count of recovered rows (pending + dead-lettered).
        """
        # First: recover rows that still have retries left
        stmt_recover = (
            update(ProjectionOutboxRow)
            .where(
                ProjectionOutboxRow.state == "processing",
                ProjectionOutboxRow.lease_expires_at <= now,
                ProjectionOutboxRow.attempt_count < ProjectionOutboxRow.max_attempts,
            )
            .values(
                state="pending",
                lease_owner=None,
                lease_expires_at=None,
                available_at=now,
                updated_at=now,
            )
        )
        result_recover = await self._session.execute(stmt_recover)

        # Second: dead-letter processing rows that have exhausted retries
        stmt_exhausted = (
            update(ProjectionOutboxRow)
            .where(
                ProjectionOutboxRow.state == "processing",
                ProjectionOutboxRow.lease_expires_at <= now,
                ProjectionOutboxRow.attempt_count >= ProjectionOutboxRow.max_attempts,
            )
            .values(
                state="dead_letter",
                lease_owner=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        result_exhausted = await self._session.execute(stmt_exhausted)

        # Third: dead-letter legacy/stale pending rows that are exhausted
        # This handles rows that somehow ended up in pending state with
        # attempt_count >= max_attempts (e.g., from a failed recovery or
        # manual intervention)
        stmt_pending_exhausted = (
            update(ProjectionOutboxRow)
            .where(
                ProjectionOutboxRow.state == "pending",
                ProjectionOutboxRow.attempt_count >= ProjectionOutboxRow.max_attempts,
            )
            .values(
                state="dead_letter",
                lease_owner=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        result_pending_exhausted = await self._session.execute(stmt_pending_exhausted)

        await self._session.flush()
        return (
            _rowcount(result_recover)
            + _rowcount(result_exhausted)
            + _rowcount(result_pending_exhausted)
        )

    # ------------------------------------------------------------------
    # Terminal transitions
    # ------------------------------------------------------------------

    async def mark_completed(
        self,
        outbox_id: UUID,
        *,
        fencing_token: str,
    ) -> bool:
        """Mark a row as completed.

        Returns True if the transition succeeded, False if the row was
        not in the expected state (lease lost or already completed).
        """
        now = _now_utc()
        stmt = (
            update(ProjectionOutboxRow)
            .where(
                ProjectionOutboxRow.id == outbox_id,
                ProjectionOutboxRow.state == "processing",
                ProjectionOutboxRow.lease_owner == fencing_token,
                ProjectionOutboxRow.lease_expires_at > now,
            )
            .values(
                state="completed",
                processed_at=now,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return _rowcount(result) > 0

    async def mark_failed(
        self,
        outbox_id: UUID,
        *,
        fencing_token: str,
        error: str,
        permanent: bool = False,
        retry_delay: timedelta | None = None,
    ) -> Literal["pending", "dead_letter", "lease_lost"]:
        """Mark a row as failed.

        Returns the disposition: ``"dead_lettered"``, ``"retried"``, or
        ``"lease_lost"``.

        Permanent failures go directly to dead_letter. Retryable failures
        return to pending only if attempts remain; otherwise dead-letter.
        """
        now = _now_utc()

        # First, fetch the row to check attempt exhaustion
        stmt_fetch = select(ProjectionOutboxRow).where(
            ProjectionOutboxRow.id == outbox_id,
            ProjectionOutboxRow.state == "processing",
            ProjectionOutboxRow.lease_owner == fencing_token,
            ProjectionOutboxRow.lease_expires_at > now,
        )
        result_fetch = await self._session.execute(stmt_fetch)
        row = result_fetch.scalar_one_or_none()

        if row is None:
            # Lease lost or already completed
            return "lease_lost"

        # Determine final disposition based on permanent flag and exhaustion
        is_exhausted = row.attempt_count >= row.max_attempts

        if permanent or is_exhausted:
            new_state = "dead_letter"
        else:
            new_state = "pending"

        values: dict[str, Any] = {
            "state": new_state,
            "last_error": error[:4000] if error else None,
            "lease_owner": None,
            "lease_expires_at": None,
            "updated_at": now,
        }

        if new_state == "pending":
            # Retryable with attempts remaining
            if retry_delay is not None:
                values["available_at"] = now + retry_delay
            else:
                values["available_at"] = now

        stmt = (
            update(ProjectionOutboxRow)
            .where(
                ProjectionOutboxRow.id == outbox_id,
                ProjectionOutboxRow.state == "processing",
                ProjectionOutboxRow.lease_owner == fencing_token,
                ProjectionOutboxRow.lease_expires_at > now,
            )
            .values(**values)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()

        if _rowcount(result) == 0:
            return "lease_lost"
        return "dead_letter" if new_state == "dead_letter" else "pending"

    async def requeue_dead_letter(
        self,
        outbox_id: UUID,
    ) -> bool:
        """Reset a dead-letter row back to pending for retry.

        Returns True if the transition succeeded.
        """
        now = _now_utc()
        stmt = (
            update(ProjectionOutboxRow)
            .where(
                ProjectionOutboxRow.id == outbox_id,
                ProjectionOutboxRow.state == "dead_letter",
            )
            .values(
                state="pending",
                attempt_count=0,
                available_at=now,
                lease_owner=None,
                lease_expires_at=None,
                last_error=None,
                updated_at=now,
            )
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return _rowcount(result) > 0

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def lookup_by_authority(
        self,
        authority_envelope_id: UUID,
    ) -> list[StoredProjectionRow]:
        """Return all projection rows for a given authority envelope."""
        stmt = (
            select(ProjectionOutboxRow)
            .where(
                ProjectionOutboxRow.authority_envelope_id == authority_envelope_id
            )
            .order_by(ProjectionOutboxRow.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return [_row_to_stored(r) for r in result.scalars().all()]

    async def _lookup_by_key(
        self, idempotency_key: str
    ) -> StoredProjectionRow | None:
        """Look up a projection row by its idempotency key."""
        stmt = select(ProjectionOutboxRow).where(
            ProjectionOutboxRow.idempotency_key == idempotency_key
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return _row_to_stored(row)

    async def count_by_state(self, state: str) -> int:
        """Return count of rows in a given state."""
        from sqlalchemy import func

        stmt = (
            select(func.count())
            .select_from(ProjectionOutboxRow)
            .where(ProjectionOutboxRow.state == state)
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())


__all__ = [
    "DEFAULT_WORLD_PROJECTION_TARGETS",
    "StoredProjectionRow",
    "WorldMemoryProjectionConflict",
    "WorldMemoryProjectionOutboxRepository",
    "validate_targets",
]
