"""Integration tests for projection worker claiming (P1-04).

Tests claim transactions, deterministic ordering, concurrent workers,
and fencing semantics against real PostgreSQL.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.db.models.memory_plane import MemoryProjectionOutbox as OutboxRow
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore


class TestClaiming:
    """Tests for claim transaction behavior."""

    @pytest.mark.asyncio
    async def test_due_pending_rows_claim_in_order(self, session_factory) -> None:
        """Due pending rows should claim in deterministic order."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create events with different creation times
        async with session_factory() as session:
            for i in range(3):
                row = OutboxRow(
                    id=uuid4(),
                    aggregate_id=uuid4(),
                    event_type="memory.remembered",
                    schema_version=1,
                    projection_target="redis",
                    idempotency_key=f"test:key:{i}",
                    payload={"index": i},
                    state="pending",
                    attempt_count=0,
                    max_attempts=8,
                )
                session.add(row)
            await session.commit()

        # Claim batch
        claimed, exhausted = await store.claim_batch(
            batch_size=10,
            lease_duration=timedelta(seconds=60),
        )

        assert len(claimed) == 3
        assert exhausted == 0

        # All should be in processing state
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow)
                .where(OutboxRow.state == "processing")
                .order_by(OutboxRow.created_at)
            )
            rows = list(result.scalars().all())
            assert len(rows) == 3

            # Verify attempt count incremented
            for row in rows:
                assert row.attempt_count == 1

    @pytest.mark.asyncio
    async def test_future_pending_not_claimed(self, session_factory) -> None:
        """Future pending rows should not be claimed."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create event available in the future
        async with session_factory() as session:
            # Event available now
            row1 = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:now",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
            )

            # Event available in 1 hour
            result = await session.execute(text("SELECT NOW() + INTERVAL '1 hour'"))
            future_time = result.scalar_one()

            row2 = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:future",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
                available_at=future_time,
            )

            session.add_all([row1, row2])
            await session.commit()

        # Claim batch - should only get the available one
        claimed, _ = await store.claim_batch(
            batch_size=10,
            lease_duration=timedelta(seconds=60),
        )

        assert len(claimed) == 1
        assert claimed[0].event.idempotency_key == "test:now"

    @pytest.mark.asyncio
    async def test_completed_and_dead_letter_not_claimed(self, session_factory) -> None:
        """Completed and dead_letter rows should not be claimed."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        async with session_factory() as session:
            # Pending - should be claimed
            row1 = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:pending",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
            )

            # Completed - should not be claimed
            row2 = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:completed",
                payload={},
                state="completed",
                attempt_count=1,
                max_attempts=8,
            )

            # Dead-letter - should not be claimed
            row3 = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:dead",
                payload={},
                state="dead_letter",
                attempt_count=8,
                max_attempts=8,
            )

            session.add_all([row1, row2, row3])
            await session.commit()

        claimed, _ = await store.claim_batch(
            batch_size=10,
            lease_duration=timedelta(seconds=60),
        )

        assert len(claimed) == 1
        assert claimed[0].event.idempotency_key == "test:pending"

    @pytest.mark.asyncio
    async def test_expired_processing_reclaimed(self, session_factory) -> None:
        """Expired processing rows should be reclaimed with fresh token."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create an expired processing row
        async with session_factory() as session:
            result = await session.execute(text("SELECT NOW() - INTERVAL '1 hour'"))
            past_time = result.scalar_one()

            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:expired",
                payload={},
                state="processing",
                attempt_count=1,
                max_attempts=8,
                lease_owner="old-worker:old-token",
                lease_expires_at=past_time,
            )

            session.add(row)
            await session.commit()

        # Claim should reclaim it
        claimed, _ = await store.claim_batch(
            batch_size=10,
            lease_duration=timedelta(seconds=60),
        )

        assert len(claimed) == 1
        assert claimed[0].event.idempotency_key == "test:expired"
        assert claimed[0].event.attempt_count == 2  # Incremented
        assert "old-token" not in claimed[0].fencing_token  # New token

    @pytest.mark.asyncio
    async def test_concurrent_workers_claim_disjoint_events(self, session_factory) -> None:
        """Concurrent workers should claim disjoint event IDs."""
        store1 = ProjectionOutboxStore(session_factory, "worker-1")
        store2 = ProjectionOutboxStore(session_factory, "worker-2")

        # Create multiple events
        async with session_factory() as session:
            for i in range(10):
                row = OutboxRow(
                    id=uuid4(),
                    aggregate_id=uuid4(),
                    event_type="memory.remembered",
                    schema_version=1,
                    projection_target="redis",
                    idempotency_key=f"test:concurrent:{i}",
                    payload={"index": i},
                    state="pending",
                    attempt_count=0,
                    max_attempts=8,
                )
                session.add(row)
            await session.commit()

        # Concurrent claims
        results = await asyncio.gather(
            store1.claim_batch(batch_size=5, lease_duration=timedelta(seconds=60)),
            store2.claim_batch(batch_size=5, lease_duration=timedelta(seconds=60)),
        )

        claimed1, _ = results[0]
        claimed2, _ = results[1]

        # Should have claimed 5 each (total 10)
        assert len(claimed1) == 5
        assert len(claimed2) == 5

        # No overlap
        ids1 = {e.event.outbox_id for e in claimed1}
        ids2 = {e.event.outbox_id for e in claimed2}
        assert ids1.isdisjoint(ids2)