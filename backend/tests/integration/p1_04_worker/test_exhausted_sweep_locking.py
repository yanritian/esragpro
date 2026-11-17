"""Integration tests for exhausted sweep locking (P1-04).

Tests SKIP LOCKED behavior for exhausted processing row sweep.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.db.models.memory_plane import MemoryProjectionOutbox as OutboxRow
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore


class TestExhaustedSweepLocking:
    """Tests for SKIP LOCKED behavior during exhausted sweep."""

    @pytest.mark.asyncio
    async def test_locked_exhausted_row_skipped_and_later_dead_lettered(
        self, session_factory
    ) -> None:
        """A separately locked expired/exhausted row is skipped promptly without blocking."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Get current time
        async with session_factory() as session:
            result = await session.execute(text("SELECT NOW()"))
            now = result.scalar_one()

        # Create an expired exhausted processing row
        row_id = uuid4()
        async with session_factory() as session:
            row = OutboxRow(
                id=row_id,
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:locked-exhausted",
                payload={},
                state="processing",
                attempt_count=8,
                max_attempts=8,
                lease_owner="active-worker:token",
                lease_expires_at=now - timedelta(hours=1),  # Expired
            )
            session.add(row)
            await session.commit()

        # Lock the row in a separate transaction (simulating active processing)
        async with session_factory() as lock_session:
            # Lock the row with NOWAIT to verify it's lockable
            result = await lock_session.execute(
                select(OutboxRow)
                .where(OutboxRow.id == row_id)
                .with_for_update(nowait=True)
            )
            locked_row = result.scalar_one()
            assert locked_row.state == "processing"

            # While holding the lock, try claim from another transaction
            # This should NOT block - SKIP LOCKED should skip it
            store2 = ProjectionOutboxStore(session_factory, "other-worker")

            # Start the claim in background - should complete without blocking
            # because SKIP LOCKED skips the locked row
            claim_task = store2.claim_batch(
                batch_size=10,
                lease_duration=timedelta(seconds=60),
            )

            # Wait for claim to complete (should be fast since it skips locked row)
            import asyncio
            try:
                claimed, exhausted = await asyncio.wait_for(claim_task, timeout=5.0)
            except asyncio.TimeoutError:
                pytest.fail("Claim blocked on locked row - SKIP LOCKED not working")

            # Should claim nothing (exhausted row was locked)
            assert len(claimed) == 0
            # Should not sweep the locked row
            assert exhausted == 0

            # Commit to release lock
            await lock_session.commit()

        # After releasing lock, next claim should sweep it
        claimed2, exhausted2 = await store.claim_batch(
            batch_size=10,
            lease_duration=timedelta(seconds=60),
        )

        assert len(claimed2) == 0
        assert exhausted2 == 1

        # Verify dead_letter state
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == row_id)
            )
            row = result.scalar_one()
            assert row.state == "dead_letter"
            assert row.lease_owner is None
            assert row.last_error == "lease expired after final attempt"


class TestFilteredExhaustedSweepScope:
    """Tests for target_filter applied to exhausted sweep (P1-04 defect 2)."""

    @pytest.mark.asyncio
    async def test_filtered_sweep_does_not_cross_target_scope(
        self, session_factory
    ) -> None:
        """Redis-only worker must not dead-letter expired/exhausted Elasticsearch/Neo4j rows.

        The exhausted sweep must respect target_filter.
        """
        store = ProjectionOutboxStore(session_factory, "redis-worker")

        # Get current time
        async with session_factory() as session:
            result = await session.execute(text("SELECT NOW()"))
            now = result.scalar_one()

        # Create expired/exhausted rows for all three targets
        redis_id = uuid4()
        es_id = uuid4()
        neo4j_id = uuid4()

        async with session_factory() as session:
            # Redis row - should be swept by Redis-only worker
            redis_row = OutboxRow(
                id=redis_id,
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:filter-sweep:redis",
                payload={},
                state="processing",
                attempt_count=8,
                max_attempts=8,
                lease_owner="old-worker:redis",
                lease_expires_at=now - timedelta(hours=1),
            )
            # Elasticsearch row - should NOT be swept by Redis-only worker
            es_row = OutboxRow(
                id=es_id,
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="elasticsearch",
                idempotency_key="test:filter-sweep:es",
                payload={},
                state="processing",
                attempt_count=8,
                max_attempts=8,
                lease_owner="old-worker:es",
                lease_expires_at=now - timedelta(hours=1),
            )
            # Neo4j row - should NOT be swept by Redis-only worker
            neo4j_row = OutboxRow(
                id=neo4j_id,
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="neo4j",
                idempotency_key="test:filter-sweep:neo4j",
                payload={},
                state="processing",
                attempt_count=8,
                max_attempts=8,
                lease_owner="old-worker:neo4j",
                lease_expires_at=now - timedelta(hours=1),
            )
            session.add_all([redis_row, es_row, neo4j_row])
            await session.commit()

        # Claim with Redis-only target filter
        claimed, exhausted = await store.claim_batch(
            batch_size=10,
            lease_duration=timedelta(seconds=60),
            target_filter=["redis"],
        )

        # Should claim nothing
        assert len(claimed) == 0
        # Should sweep only Redis row
        assert exhausted == 1

        # Verify Redis is dead_lettered
        async with session_factory() as session:
            redis_result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == redis_id)
            )
            redis_row = redis_result.scalar_one()
            assert redis_row.state == "dead_letter"
            assert redis_row.lease_owner is None

            # Elasticsearch should remain processing with original lease fields
            es_result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == es_id)
            )
            es_row = es_result.scalar_one()
            assert es_row.state == "processing"
            assert es_row.lease_owner == "old-worker:es"
            assert es_row.last_error is None

            # Neo4j should remain processing with original lease fields
            neo4j_result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == neo4j_id)
            )
            neo4j_row = neo4j_result.scalar_one()
            assert neo4j_row.state == "processing"
            assert neo4j_row.lease_owner == "old-worker:neo4j"
            assert neo4j_row.last_error is None


__all__ = ["TestExhaustedSweepLocking", "TestFilteredExhaustedSweepScope"]