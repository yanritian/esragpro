"""Integration tests for claim transaction semantics (P1-04).

Tests claim timestamps, transaction isolation, and claim atomicity.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from app.db.models.memory_plane import MemoryProjectionOutbox as OutboxRow
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore


class TestClaimTransactionSemantics:
    """Tests for claim transaction behavior and timestamps."""

    @pytest.mark.asyncio
    async def test_claim_timestamps_are_timezone_aware(self, session_factory) -> None:
        """Claim timestamps should be timezone-aware and lease expiry approximately correct."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create pending event
        async with session_factory() as session:
            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:tz",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
            )
            session.add(row)
            await session.commit()

        lease_duration = timedelta(seconds=60)
        claimed, _ = await store.claim_batch(batch_size=1, lease_duration=lease_duration)
        assert len(claimed) == 1

        # Verify lease_expires_at is timezone-aware
        lease_expiry = claimed[0].lease_expires_at
        assert lease_expiry is not None
        assert lease_expiry.tzinfo is not None, "lease_expires_at should be timezone-aware"

        # Get PostgreSQL time and verify approximate lease duration
        async with session_factory() as session:
            result = await session.execute(select(func.now()))
            db_now = result.scalar_one()

            # Lease expiry should be approximately lease_duration after now
            delta = (lease_expiry - db_now).total_seconds()
            # Allow 5 second tolerance for network/database latency
            assert 55 <= delta <= 65, f"Lease duration delta {delta}s not close to {lease_duration.total_seconds()}s"

    @pytest.mark.asyncio
    async def test_real_manual_lock_with_skip_locked(
        self, session_factory
    ) -> None:
        """Transaction A manually locks first row; Transaction B uses SKIP LOCKED and claims second row.

        This proves SKIP LOCKED works by:
        1. Creating two eligible rows with deterministic first/second ordering
        2. Transaction A manually locks the first row with FOR UPDATE (keeps transaction open)
        3. While A holds the lock, Transaction B's claim_batch() runs under short asyncio.wait_for
        4. B should promptly claim exactly the second row (not block on first)
        """
        row1_id = uuid4()
        row2_id = uuid4()

        # Create two pending events with deterministic ordering (row1 first)
        async with session_factory() as session:
            row1 = OutboxRow(
                id=row1_id,
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:manual-lock:1",
                payload={"order": 1},
                state="pending",
                attempt_count=0,
                max_attempts=8,
            )
            row2 = OutboxRow(
                id=row2_id,
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:manual-lock:2",
                payload={"order": 2},
                state="pending",
                attempt_count=0,
                max_attempts=8,
            )
            session.add_all([row1, row2])
            await session.commit()

        # Transaction A: manually lock row1 and keep transaction open
        async with session_factory() as lock_session:
            # Lock row1 with FOR UPDATE
            result = await lock_session.execute(
                select(OutboxRow)
                .where(OutboxRow.id == row1_id)
                .with_for_update()
            )
            locked_row = result.scalar_one()
            assert locked_row.id == row1_id

            # While holding the lock, Transaction B claims
            store = ProjectionOutboxStore(session_factory, "worker-B")
            claim_task = store.claim_batch(
                batch_size=1,
                lease_duration=timedelta(seconds=60),
            )

            # B must complete promptly - proves SKIP LOCKED skipped locked row
            try:
                claimed, _ = await asyncio.wait_for(claim_task, timeout=3.0)
            except asyncio.TimeoutError:
                pytest.fail("Claim blocked - SKIP LOCKED not working")

            # B should have claimed row2 (row1 was locked by A)
            assert len(claimed) == 1
            assert claimed[0].event.outbox_id == row2_id

            # Release A's lock
            await lock_session.commit()

    @pytest.mark.asyncio
    async def test_injected_commit_failure_rolls_back(
        self, session_factory, db_engine
    ) -> None:
        """Injected commit failure rolls back state/attempt count and dispatches no handler.

        Tests that when claim_batch()'s commit fails:
        1. The error propagates to the caller
        2. Handler is never invoked
        3. Row remains pending with attempt_count=0, no lease fields
        """
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        from app.memory_core.projection_worker import (
            ProjectionWorker,
            ProjectionHandlerRegistry,
            ProjectionEvent,
        )
        from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy

        outbox_id = uuid4()

        # Step 1: Create pending row with normal factory
        async with session_factory() as session:
            row = OutboxRow(
                id=outbox_id,
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:commit-fail",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
            )
            session.add(row)
            await session.commit()

        # Step 2: Define AsyncSession subclass with failing commit
        class FailingCommitSession(AsyncSession):
            """Session that raises on commit()."""

            async def commit(self) -> None:
                raise RuntimeError("injected claim commit failure")

        # Step 3: Build sessionmaker bound to the SAME real engine
        failing_factory = async_sessionmaker(
            db_engine, class_=FailingCommitSession, expire_on_commit=False
        )

        # Step 4: Handler that records calls
        handler_calls: list[ProjectionEvent] = []

        async def recording_handler(event: ProjectionEvent) -> None:
            handler_calls.append(event)

        registry = ProjectionHandlerRegistry({"redis": recording_handler})

        # Step 5: Construct store and worker with failing factory
        store = ProjectionOutboxStore(failing_factory, "test-worker")
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)
        worker = ProjectionWorker(
            store=store,
            registry=registry,
            worker_identity="test-worker",
            batch_size=10,
            max_concurrency=10,
            lease_duration=timedelta(seconds=60),
            delivery_timeout=timedelta(seconds=30),
            finalization_margin=timedelta(seconds=5),
            retry_policy=retry_policy,
            target_filter={"redis"},
        )

        # Step 6: Assert worker.run_once() raises the injected error
        with pytest.raises(RuntimeError, match="injected claim commit failure"):
            await worker.run_once()

        # Step 7: Assert handler was never called
        assert handler_calls == [], "Handler should not be called when claim fails"

        # Step 8: Query with normal factory and verify row unchanged
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == outbox_id)
            )
            row = result.scalar_one()
            assert row.state == "pending"
            assert row.attempt_count == 0
            assert row.lease_owner is None
            assert row.lease_expires_at is None


__all__ = ["TestClaimTransactionSemantics"]