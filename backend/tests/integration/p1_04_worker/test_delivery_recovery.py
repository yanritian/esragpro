"""Integration tests for projection worker delivery and recovery (P1-04).

Tests crash recovery, exhausted sweep, and idempotent delivery.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update, text

from app.db.models.memory_plane import MemoryProjectionOutbox as OutboxRow
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore


class TestCrashRecovery:
    """Tests for worker crash recovery scenarios."""

    @pytest.mark.asyncio
    async def test_crash_leaves_recoverable_processing_row(self, session_factory) -> None:
        """Worker crash should leave processing row recoverable after lease expiry."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create and claim event
        async with session_factory() as session:
            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:crash",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
            )
            session.add(row)
            await session.commit()

        claimed, _ = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )
        assert len(claimed) == 1

        # Simulate crash - just leave it in processing state
        # (In real crash, worker would not complete the event)

        # After lease expiry, it should be reclaimable
        async with session_factory() as session:
            # Manually expire the lease
            await session.execute(text("SELECT NOW() - INTERVAL '2 hours'"))
            result = await session.execute(text("SELECT NOW() - INTERVAL '2 hours'"))
            past_time = result.scalar_one()

            await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == claimed[0].event.outbox_id)
                .values(lease_expires_at=past_time)
            )
            await session.commit()

        # New claim should recover it
        claimed2, _ = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )

        assert len(claimed2) == 1
        assert claimed2[0].event.outbox_id == claimed[0].event.outbox_id
        assert claimed2[0].event.attempt_count == 2  # Incremented again

    @pytest.mark.asyncio
    async def test_crash_after_final_claim_moves_to_dead_letter(self, session_factory) -> None:
        """Crash after final claim should sweep to dead_letter on next recovery."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create event near max attempts
        async with session_factory() as session:
            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:final-crash",
                payload={},
                state="pending",
                attempt_count=7,  # One less than max
                max_attempts=8,
            )
            session.add(row)
            await session.commit()

        # Claim (increments to 8)
        claimed, _ = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )
        assert claimed[0].event.attempt_count == 8

        # Simulate crash - expire lease
        async with session_factory() as session:
            result = await session.execute(text("SELECT NOW() - INTERVAL '1 hour'"))
            past_time = result.scalar_one()
            await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == claimed[0].event.outbox_id)
                .values(lease_expires_at=past_time)
            )
            await session.commit()

        # Next claim sweep should move it to dead_letter
        claimed2, exhausted = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )

        # Nothing claimed (it went to dead_letter)
        assert len(claimed2) == 0
        assert exhausted == 1

        # Verify it's in dead_letter
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == claimed[0].event.outbox_id)
            )
            row = result.scalar_one()
            assert row.state == "dead_letter"


class TestExhaustedRecovery:
    """Tests for exhausted processing row sweep."""

    @pytest.mark.asyncio
    async def test_expired_exhausted_swept_to_dead_letter(self, session_factory) -> None:
        """Expired processing row at max attempts should be swept to dead_letter."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create expired processing row at max attempts
        async with session_factory() as session:
            result = await session.execute(text("SELECT NOW() - INTERVAL '1 hour'"))
            past_time = result.scalar_one()

            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:exhausted",
                payload={},
                state="processing",
                attempt_count=8,
                max_attempts=8,
                lease_owner="crashed-worker",
                lease_expires_at=past_time,
            )
            session.add(row)
            await session.commit()

        # Claim should sweep it
        claimed, exhausted = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )

        assert len(claimed) == 0  # Nothing to claim
        assert exhausted == 1  # One recovered

        # Verify dead_letter state
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == row.id)
            )
            row = result.scalar_one()
            assert row.state == "dead_letter"
            assert row.lease_owner is None

    @pytest.mark.asyncio
    async def test_non_expired_exhausted_not_swept(self, session_factory) -> None:
        """Non-expired processing row at max attempts should not be swept."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create non-expired processing row at max attempts
        async with session_factory() as session:
            result = await session.execute(text("SELECT NOW() + INTERVAL '1 hour'"))
            future_time = result.scalar_one()

            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:active-exhausted",
                payload={},
                state="processing",
                attempt_count=8,
                max_attempts=8,
                lease_owner="active-worker",
                lease_expires_at=future_time,
            )
            session.add(row)
            await session.commit()

        # Claim should not sweep it (lease still valid)
        claimed, exhausted = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )

        assert len(claimed) == 0
        assert exhausted == 0  # Not swept

        # Verify still processing
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == row.id)
            )
            row = result.scalar_one()
            assert row.state == "processing"


class TestIdempotentDelivery:
    """Tests for idempotent delivery contract."""

    @pytest.mark.asyncio
    async def test_duplicate_claim_same_event_different_tokens(self, session_factory) -> None:
        """Reclaiming same event should generate different fencing token."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create and claim
        async with session_factory() as session:
            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:reclaim",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
            )
            session.add(row)
            await session.commit()

        claimed1, _ = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )
        token1 = claimed1[0].fencing_token

        # Expire and reclaim
        async with session_factory() as session:
            await session.execute(text("SELECT NOW() - INTERVAL '1 hour'"))
            result = await session.execute(text("SELECT NOW() - INTERVAL '1 hour'"))
            past_time = result.scalar_one()
            await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == claimed1[0].event.outbox_id)
                .values(lease_expires_at=past_time)
            )
            await session.commit()

        claimed2, _ = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )
        token2 = claimed2[0].fencing_token

        # Different tokens
        assert token1 != token2

        # Prevents ABA stale completion
        success = await store.mark_completed(
            outbox_id=claimed1[0].event.outbox_id,
            fencing_token=token1,  # Old token
        )
        assert not success  # Should fail