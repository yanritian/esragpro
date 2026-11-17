"""Integration tests for projection worker state transitions (P1-04).

Tests completion, failure, retry, dead-letter, and fencing semantics.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models.memory_plane import MemoryProjectionOutbox as OutboxRow
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore


class TestStateTransitions:
    """Tests for state transition operations."""

    @pytest.mark.asyncio
    async def test_correct_token_completes_event(self, session_factory) -> None:
        """Correct unexpired token should complete event."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Claim an event
        async with session_factory() as session:
            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:complete",
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

        # Complete it
        success = await store.mark_completed(
            outbox_id=claimed[0].event.outbox_id,
            fencing_token=claimed[0].fencing_token,
        )
        assert success

        # Verify state
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == claimed[0].event.outbox_id)
            )
            row = result.scalar_one()
            assert row.state == "completed"
            assert row.processed_at is not None
            assert row.lease_owner is None
            assert row.last_error is None

    @pytest.mark.asyncio
    async def test_stale_token_cannot_complete(self, session_factory) -> None:
        """Stale token should not complete after newer claim."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create and claim event
        async with session_factory() as session:
            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:stale",
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
        old_token = claimed1[0].fencing_token

        # Expire and reclaim with new token
        from datetime import datetime, timezone
        from sqlalchemy import update
        past_time = datetime.now(timezone.utc) - timedelta(hours=1)  # Real past timestamp
        await session.execute(
            update(OutboxRow)
            .where(OutboxRow.id == claimed1[0].event.outbox_id)
            .values(lease_expires_at=past_time)  # Set real past expiry
        )
        await session.commit()

        claimed2, _ = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )
        assert len(claimed2) == 1
        new_token = claimed2[0].fencing_token

        # Try to complete with old token - should fail
        success = await store.mark_completed(
            outbox_id=claimed1[0].event.outbox_id,
            fencing_token=old_token,
        )
        assert not success

        # New token should work
        success = await store.mark_completed(
            outbox_id=claimed1[0].event.outbox_id,
            fencing_token=new_token,
        )
        assert success

    @pytest.mark.asyncio
    async def test_retryable_failure_returns_to_pending(self, session_factory) -> None:
        """Retryable failure should return to pending with future available_at."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create and claim event
        async with session_factory() as session:
            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:retry",
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

        # Mark as failed (retryable)
        retry_delay = timedelta(minutes=5)
        disposition = await store.mark_failed(
            outbox_id=claimed[0].event.outbox_id,
            fencing_token=claimed[0].fencing_token,
            error="Temporary failure",
            permanent=False,
            retry_delay=retry_delay,
        )
        assert disposition.retried

        # Verify state
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == claimed[0].event.outbox_id)
            )
            row = result.scalar_one()
            assert row.state == "pending"
            assert row.attempt_count == 1
            assert row.available_at is not None
            assert "Temporary failure" in row.last_error
            assert row.lease_owner is None

    @pytest.mark.asyncio
    async def test_max_attempt_failure_enters_dead_letter(self, session_factory) -> None:
        """Max attempt failure should enter dead_letter."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create event on max attempt
        async with session_factory() as session:
            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:max",
                payload={},
                state="pending",
                attempt_count=7,  # One less than max
                max_attempts=8,
            )
            session.add(row)
            await session.commit()

        claimed, _ = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )
        assert claimed[0].event.attempt_count == 8  # Now at max

        # Mark as failed - should go to dead_letter
        disposition = await store.mark_failed(
            outbox_id=claimed[0].event.outbox_id,
            fencing_token=claimed[0].fencing_token,
            error="Max attempts reached",
            permanent=False,
            retry_delay=None,
        )
        assert disposition.dead_lettered

        # Verify state
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == claimed[0].event.outbox_id)
            )
            row = result.scalar_one()
            assert row.state == "dead_letter"
            assert row.attempt_count == 8
            assert row.processed_at is None

    @pytest.mark.asyncio
    async def test_permanent_failure_immediate_dead_letter(self, session_factory) -> None:
        """Permanent failure should go to dead_letter immediately."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create and claim event
        async with session_factory() as session:
            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:permanent",
                payload={},
                state="pending",
                attempt_count=1,  # Only first attempt
                max_attempts=8,
            )
            session.add(row)
            await session.commit()

        claimed, _ = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )

        # Mark as permanent failure
        disposition = await store.mark_failed(
            outbox_id=claimed[0].event.outbox_id,
            fencing_token=claimed[0].fencing_token,
            error="Permanent error",
            permanent=True,
            retry_delay=None,
        )
        assert disposition.dead_lettered

        # Verify immediate dead_letter
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == claimed[0].event.outbox_id)
            )
            row = result.scalar_one()
            assert row.state == "dead_letter"
            assert row.attempt_count == 2  # Still incremented
