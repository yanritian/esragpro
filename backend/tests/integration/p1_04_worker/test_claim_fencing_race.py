"""Integration tests for fencing race conditions (P1-04).

Tests true fencing race scenarios with real PostgreSQL concurrency.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, func, update

from app.db.models.memory_plane import MemoryProjectionOutbox as OutboxRow
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore


class TestClaimFencingRace:
    """Tests for fencing token race conditions."""

    @pytest.mark.asyncio
    async def test_true_fencing_race_not_serial_stale_token(
        self, session_factory
    ) -> None:
        """True fencing race: concurrent update competes with stale mark_failed.

        Creates a real race:
        1. Claim and capture old token
        2. Transaction A manually row-locks the outbox row
        3. Start mark_failed(old_token, ...) in Transaction B - it waits on A
        4. While A holds lock, update to new token and unexpired lease, then commit
        5. mark_failed resumes and must return lease_lost without changing new owner
        """
        outbox_id = uuid4()

        async with session_factory() as session:
            row = OutboxRow(
                id=outbox_id,
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:true-race",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
            )
            session.add(row)
            await session.commit()

        # Claim and capture old token
        store = ProjectionOutboxStore(session_factory, "worker-1")
        claimed, _ = await store.claim_batch(
            batch_size=1, lease_duration=timedelta(seconds=60)
        )
        assert len(claimed) == 1
        old_token = claimed[0].fencing_token

        # Transaction A: row-lock the outbox row
        async with session_factory() as session_a:
            # Lock the row with FOR UPDATE
            result = await session_a.execute(
                select(OutboxRow)
                .where(OutboxRow.id == outbox_id)
                .with_for_update()
            )
            locked_row = result.scalar_one()
            assert locked_row.lease_owner == old_token

            # Start mark_failed(old_token) in background - it will wait on A's lock
            async def try_mark_failed():
                disposition = await store.mark_failed(
                    outbox_id=outbox_id,
                    fencing_token=old_token,
                    error="stale attempt",
                    permanent=False,
                    retry_delay=timedelta(seconds=10),
                )
                return disposition

            # Run mark_failed concurrently
            mark_task = asyncio.create_task(try_mark_failed())

            # Give it a moment to start waiting
            await asyncio.sleep(0.5)

            # While A holds the lock, update to new owner
            # Simulate another worker claiming
            new_token = "worker-2:new-claim-token"
            await session_a.execute(
                update(OutboxRow)
                .where(OutboxRow.id == outbox_id)
                .values(
                    lease_owner=new_token,
                    lease_expires_at=func.now() + timedelta(hours=1),
                )
            )
            # Commit releases A's lock
            await session_a.commit()

        # mark_failed should resume and see lease_lost
        disposition = await mark_task
        assert disposition.lease_lost
        assert not disposition.retried
        assert not disposition.dead_lettered

        # Row should have new owner, unchanged state/error
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == outbox_id)
            )
            row = result.scalar_one()
            assert row.state == "processing"
            assert row.lease_owner == new_token
            assert row.last_error is None


__all__ = ["TestClaimFencingRace"]
