"""Integration tests for retry timing and fencing (P1-04).

Tests retry timing derivation, fencing token semantics, and ordering.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text, func, update

from app.db.models.memory_plane import MemoryProjectionOutbox as OutboxRow
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore


class TestRetryTimingAndFencing:
    """Tests for retry timing and fencing behavior."""

    @pytest.mark.asyncio
    async def test_retry_available_at_derived_from_postgres_time(
        self, session_factory
    ) -> None:
        """Retry available_at is derived from PostgreSQL time and approximately correct delay."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create and claim event
        async with session_factory() as session:
            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:timing",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
            )
            session.add(row)
            await session.commit()

        claimed, _ = await store.claim_batch(batch_size=1, lease_duration=timedelta(seconds=60))

        # Mark as failed with 30 second delay
        retry_delay = timedelta(seconds=30)
        disposition = await store.mark_failed(
            outbox_id=claimed[0].event.outbox_id,
            fencing_token=claimed[0].fencing_token,
            error="Test error",
            permanent=False,
            retry_delay=retry_delay,
        )
        assert disposition.retried

        # Verify available_at is approximately retry_delay in the future
        async with session_factory() as session:
            # Get current PostgreSQL time
            result = await session.execute(select(func.now()))
            db_now = result.scalar_one()

            # Get the row
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == claimed[0].event.outbox_id)
            )
            row = result.scalar_one()

            # available_at should be approximately retry_delay after now
            delta = (row.available_at - db_now).total_seconds()
            # Allow 5 second tolerance
            assert 25 <= delta <= 35, f"Retry delay delta {delta}s not close to {retry_delay.total_seconds()}s"

    @pytest.mark.asyncio
    async def test_stale_token_cannot_retryable_fail(self, session_factory) -> None:
        """Stale fencing token cannot perform retryable failure and returns lease_lost."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create and claim event
        async with session_factory() as session:
            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:stale-retry",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
            )
            session.add(row)
            await session.commit()

        claimed1, _ = await store.claim_batch(batch_size=1, lease_duration=timedelta(seconds=60))
        old_token = claimed1[0].fencing_token

        # Expire and reclaim
        async with session_factory() as session:
            result = await session.execute(text("SELECT NOW() - INTERVAL '1 hour'"))
            past_time = result.scalar_one()
            await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == claimed1[0].event.outbox_id)
                .values(lease_expires_at=past_time)
            )
            await session.commit()

        claimed2, _ = await store.claim_batch(batch_size=1, lease_duration=timedelta(seconds=60))
        assert len(claimed2) == 1

        # Try to mark failed with old token
        disposition = await store.mark_failed(
            outbox_id=claimed1[0].event.outbox_id,
            fencing_token=old_token,
            error="Stale attempt",
            permanent=False,
            retry_delay=timedelta(seconds=10),
        )

        # Should return lease_lost
        assert disposition.lease_lost
        assert not disposition.retried
        assert not disposition.dead_lettered

        # Verify state unchanged (still processing with new token)
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == claimed1[0].event.outbox_id)
            )
            row = result.scalar_one()
            assert row.state == "processing"
            assert row.lease_owner == claimed2[0].fencing_token

    @pytest.mark.asyncio
    async def test_due_recovery_ordering_proven(self, session_factory) -> None:
        """Due/recovery ordering proven with disagreeing available_at and lease_expires_at."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Get current time
        async with session_factory() as session:
            result = await session.execute(text("SELECT NOW()"))
            now = result.scalar_one()

        # Create pending row with future available_at
        pending_id = uuid4()
        async with session_factory() as session:
            row = OutboxRow(
                id=pending_id,
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:pending-future",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
                available_at=now + timedelta(hours=1),  # Future
            )
            session.add(row)
            await session.commit()

        # Create processing row with expired lease (older)
        processing_id = uuid4()
        async with session_factory() as session:
            row = OutboxRow(
                id=processing_id,
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:processing-expired",
                payload={},
                state="processing",
                attempt_count=1,
                max_attempts=8,
                lease_owner="old-worker:token",
                lease_expires_at=now - timedelta(hours=1),  # Expired (older)
                available_at=now + timedelta(hours=2),  # Future available_at (newer)
            )
            session.add(row)
            await session.commit()

        # Claim should prefer processing row (older lease expiry)
        # even though its available_at is newer
        claimed, _ = await store.claim_batch(batch_size=1, lease_duration=timedelta(seconds=60))

        assert len(claimed) == 1
        assert claimed[0].event.outbox_id == processing_id  # Recovery preferred over future pending

        # Complete it
        await store.mark_completed(claimed[0].event.outbox_id, claimed[0].fencing_token)

        # Now pending should be claimable
        claimed2, _ = await store.claim_batch(batch_size=1, lease_duration=timedelta(hours=1))
        # Should get nothing because available_at is in future
        assert len(claimed2) == 0


__all__ = ["TestRetryTimingAndFencing"]