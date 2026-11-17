"""PostgreSQL state transition operations for projection outbox (P1-04).

Implements completion/failure/requeue operations using PostgreSQL time.
All timestamps are timezone-aware.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import update, func, case, literal_column
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.memory_plane import (
    MemoryProjectionOutbox as OutboxRow,
)
from app.memory_core.projection_worker.contracts import FailureDisposition

_MAX_ERROR_LENGTH = 4000


class ProjectionTransitionStore:
    """PostgreSQL store for projection outbox state transitions.

    Handles completion, failure, and requeue operations.
    All timestamps are timezone-aware (PostgreSQL TIMESTAMPTZ).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._factory = session_factory

    async def mark_completed(
        self,
        outbox_id: UUID,
        fencing_token: str,
    ) -> bool:
        """Mark event as completed.

        Guarded by: row ID, state=processing, exact token, unexpired lease.
        Uses PostgreSQL func.now() for all timestamps.
        Uses single atomic UPDATE with RETURNING.

        Returns:
            True if completed, False if lease lost
        """
        async with self._factory() as session:
            result = await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == outbox_id)
                .where(OutboxRow.state == "processing")
                .where(OutboxRow.lease_owner == fencing_token)
                .where(OutboxRow.lease_expires_at > func.now())
                .values(
                    state="completed",
                    processed_at=func.now(),
                    updated_at=func.now(),
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error=None,
                )
                .returning(OutboxRow.id)
            )
            row = result.first()
            await session.commit()
            return row is not None

    async def mark_failed(
        self,
        outbox_id: UUID,
        fencing_token: str,
        error: str | None,
        permanent: bool,
        retry_delay: timedelta | None = None,
    ) -> FailureDisposition:
        """Mark event as failed.

        Guarded by: row ID, state=processing, exact token, unexpired lease.
        Uses PostgreSQL func.now() for all timestamps.
        Uses single atomic UPDATE with CASE to prevent TOCTOU.

        Args:
            outbox_id: Row UUID
            fencing_token: Exact lease token
            error: Error message (will be capped at 4000 chars)
            permanent: If True, go to dead_letter immediately
            retry_delay: Delay before retry (for retryable failures)
                         If None, defaults to 0 (immediate retry)

        Returns:
            FailureDisposition: retried, dead_lettered, or lease_lost
        """
        capped_error = None
        if error:
            capped_error = error[:_MAX_ERROR_LENGTH]

        async with self._factory() as session:
            if permanent:
                # Single guarded UPDATE for permanent failure
                result = await session.execute(
                    update(OutboxRow)
                    .where(OutboxRow.id == outbox_id)
                    .where(OutboxRow.state == "processing")
                    .where(OutboxRow.lease_owner == fencing_token)
                    .where(OutboxRow.lease_expires_at > func.now())
                    .values(
                        state="dead_letter",
                        last_error=capped_error,
                        updated_at=func.now(),
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                    .returning(OutboxRow.id)
                )
                row = result.first()
                await session.commit()
                if row is not None:
                    return FailureDisposition.make_dead_lettered()
                else:
                    return FailureDisposition.make_lease_lost()

            # Retryable failure: single atomic UPDATE with CASE
            delay = retry_delay if retry_delay is not None else timedelta(0)

            # Use CASE to determine next state based on current attempt_count
            # If attempt_count >= max_attempts: dead_letter
            # Else: pending with available_at = now() + delay
            result = await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == outbox_id)
                .where(OutboxRow.state == "processing")
                .where(OutboxRow.lease_owner == fencing_token)
                .where(OutboxRow.lease_expires_at > func.now())
                .values(
                    state=case(
                        (
                            OutboxRow.attempt_count >= OutboxRow.max_attempts,
                            literal_column("'dead_letter'"),
                        ),
                        else_=literal_column("'pending'"),
                    ),
                    available_at=case(
                        (
                            OutboxRow.attempt_count >= OutboxRow.max_attempts,
                            OutboxRow.available_at,  # Keep old value for dead_letter
                        ),
                        else_=func.now() + delay,
                    ),
                    last_error=capped_error,
                    updated_at=func.now(),
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .returning(OutboxRow.state)
            )
            row = result.first()
            await session.commit()

            if row is None:
                return FailureDisposition.make_lease_lost()

            # Determine disposition from returned state
            returned_state = row[0]
            if returned_state == "dead_letter":
                return FailureDisposition.make_dead_lettered()
            else:
                return FailureDisposition.make_retried()

    async def requeue_dead_letter(self, outbox_id: UUID) -> bool:
        """Requeue a single dead-letter event.

        Uses single atomic UPDATE with RETURNING.

        Args:
            outbox_id: Row UUID

        Returns:
            True if one row changed, False otherwise
        """
        async with self._factory() as session:
            result = await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == outbox_id)
                .where(OutboxRow.state == "dead_letter")
                .values(
                    state="pending",
                    attempt_count=0,
                    available_at=func.now(),
                    updated_at=func.now(),
                    lease_owner=None,
                    lease_expires_at=None,
                    processed_at=None,
                    last_error=None,
                )
                .returning(OutboxRow.id)
            )
            row = result.first()
            await session.commit()
            return row is not None


__all__ = ["ProjectionTransitionStore"]
