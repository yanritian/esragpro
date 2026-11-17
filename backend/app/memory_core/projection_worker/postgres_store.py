"""PostgreSQL store for projection outbox (P1-04).

Facade combining claim and transition operations.
All timestamps are timezone-aware (PostgreSQL TIMESTAMPTZ).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable
from uuid import UUID, uuid4

from sqlalchemy import select, update, func, and_, or_, case
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.memory_plane import (
    MemoryProjectionOutbox as OutboxRow,
)
from app.memory_core.projection_worker.contracts import (
    ClaimedEvent,
    ProjectionEvent,
    FailureDisposition,
)
from app.memory_core.projection_worker.postgres_transitions import (
    ProjectionTransitionStore,
)

_MAX_WORKER_IDENTITY_LENGTH = 200


def _validate_worker_identity(identity: str) -> str:
    """Validate and normalize worker identity."""
    if not identity or not identity.strip():
        raise ValueError("worker_identity must be non-empty")

    normalized = identity.strip()

    if len(normalized) > _MAX_WORKER_IDENTITY_LENGTH:
        raise ValueError(
            f"worker_identity too long ({len(normalized)} chars), "
            f"max {_MAX_WORKER_IDENTITY_LENGTH}"
        )

    return normalized


class ProjectionOutboxStore:
    """PostgreSQL store for projection outbox operations.

    Combines claim operations with state transitions.
    All timestamps are timezone-aware (PostgreSQL TIMESTAMPTZ).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        worker_identity: str,
    ) -> None:
        self._factory = session_factory
        self._worker_identity = _validate_worker_identity(worker_identity)
        self._transitions = ProjectionTransitionStore(session_factory)

    async def claim_batch(
        self,
        batch_size: int,
        lease_duration: timedelta,
        target_filter: Iterable[str] | None = None,
    ) -> tuple[list[ClaimedEvent], int]:
        """Claim a batch of due events.

        Returns:
            Tuple of (claimed_events, exhausted_recovered_count)

        The claim transaction:
        1. Sweeps expired exhausted processing rows to dead_letter
           using FOR UPDATE SKIP LOCKED to avoid blocking
        2. Selects eligible pending/processing rows with SKIP LOCKED
        3. Updates state, attempt_count, lease fields using database time
        4. Commits before returning

        All timestamps use PostgreSQL func.now() to ensure consistency.
        """
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")

        target_set = None
        if target_filter is not None:
            target_set = set(target_filter)

        async with self._factory() as session:
            # Get database time (timezone-aware)
            now_result = await session.execute(select(func.now()))
            db_now: datetime = now_result.scalar_one()
            lease_expiry = db_now + lease_duration

            # Step 1: Sweep expired exhausted processing rows
            # Use bounded FOR UPDATE SKIP LOCKED to avoid unbounded transaction
            exhausted_recovered = 0

            # Select and lock expired exhausted rows
            exhausted_query = (
                select(OutboxRow)
                .where(
                    and_(
                        OutboxRow.state == "processing",
                        OutboxRow.lease_expires_at <= db_now,
                        OutboxRow.attempt_count >= OutboxRow.max_attempts,
                    )
                )
            )

            # Apply target filter to exhausted sweep as well
            if target_set is not None:
                exhausted_query = exhausted_query.where(
                    OutboxRow.projection_target.in_(target_set)
                )

            exhausted_query = exhausted_query.order_by(OutboxRow.id).limit(
                batch_size
            ).with_for_update(skip_locked=True)

            exhausted_result = await session.execute(exhausted_query)
            exhausted_rows = list(exhausted_result.scalars().all())

            if exhausted_rows:
                # Update only the locked rows
                exhausted_ids = [row.id for row in exhausted_rows]
                await session.execute(
                    update(OutboxRow)
                    .where(OutboxRow.id.in_(exhausted_ids))
                    .values(
                        state="dead_letter",
                        lease_owner=None,
                        lease_expires_at=None,
                        last_error="lease expired after final attempt",
                        updated_at=func.now(),
                    )
                )
                exhausted_recovered = len(exhausted_rows)

            # Step 2: Build query for eligible rows
            # Pending: available_at <= now, attempt_count < max_attempts
            # Processing: lease expired, attempt_count < max_attempts
            eligible_query = (
                select(OutboxRow)
                .where(
                    or_(
                        and_(
                            OutboxRow.state == "pending",
                            OutboxRow.available_at <= db_now,
                            OutboxRow.attempt_count < OutboxRow.max_attempts,
                        ),
                        and_(
                            OutboxRow.state == "processing",
                            OutboxRow.lease_expires_at <= db_now,
                            OutboxRow.lease_expires_at.isnot(None),
                            OutboxRow.attempt_count < OutboxRow.max_attempts,
                        ),
                    )
                )
            )

            # Apply target filter if provided
            if target_set is not None:
                eligible_query = eligible_query.where(
                    OutboxRow.projection_target.in_(target_set)
                )

            # Order by due time using CASE:
            # - processing rows: use lease_expires_at
            # - pending rows: use available_at
            # Then by created_at, then ID
            due_time = case(
                (OutboxRow.state == "processing", OutboxRow.lease_expires_at),
                else_=OutboxRow.available_at,
            )
            eligible_query = eligible_query.order_by(
                due_time,
                OutboxRow.created_at,
                OutboxRow.id,
            )

            # Apply lock and limit
            eligible_query = eligible_query.with_for_update(
                skip_locked=True
            ).limit(batch_size)

            result = await session.execute(eligible_query)
            rows = list(result.scalars().all())

            if not rows:
                await session.commit()
                return [], exhausted_recovered

            # Step 3: Update claimed rows using database time
            claimed_events: list[ClaimedEvent] = []
            for row in rows:
                # Generate fresh fencing token for each claim
                token = f"{self._worker_identity}:{uuid4()}"

                # Increment attempt count
                new_attempt = row.attempt_count + 1

                # Update row - use func.now() for all timestamps
                row.state = "processing"
                row.attempt_count = new_attempt
                row.lease_owner = token
                row.lease_expires_at = lease_expiry
                row.updated_at = db_now  # Will be overwritten by session flush

                # Create detached event envelope
                event = ProjectionEvent(
                    outbox_id=row.id,
                    aggregate_id=row.aggregate_id,
                    event_type=row.event_type,
                    schema_version=row.schema_version,
                    projection_target=row.projection_target,
                    idempotency_key=row.idempotency_key,
                    payload=row.payload,  # Defensive copy happens in __post_init__
                    attempt_count=new_attempt,
                    max_attempts=row.max_attempts,
                )

                claimed = ClaimedEvent(
                    event=event,
                    fencing_token=token,
                    lease_expires_at=lease_expiry,
                )
                claimed_events.append(claimed)

            await session.commit()

        return claimed_events, exhausted_recovered

    async def mark_completed(
        self,
        outbox_id: UUID,
        fencing_token: str,
    ) -> bool:
        """Mark event as completed. Delegates to transition store."""
        return await self._transitions.mark_completed(outbox_id, fencing_token)

    async def mark_failed(
        self,
        outbox_id: UUID,
        fencing_token: str,
        error: str | None,
        permanent: bool,
        retry_delay: timedelta | None = None,
    ) -> FailureDisposition:
        """Mark event as failed. Delegates to transition store."""
        return await self._transitions.mark_failed(
            outbox_id, fencing_token, error, permanent, retry_delay
        )

    async def requeue_dead_letter(self, outbox_id: UUID) -> bool:
        """Requeue a single dead-letter event. Delegates to transition store."""
        return await self._transitions.requeue_dead_letter(outbox_id)


__all__ = ["ProjectionOutboxStore"]