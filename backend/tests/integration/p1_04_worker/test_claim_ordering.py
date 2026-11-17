"""Integration tests for projection worker claim ordering (P1-04).

Tests claim order respects due times for pending and processing rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db.models.memory_plane import MemoryProjectionOutbox as OutboxRow
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore


class TestClaimOrdering:
    """Tests for claim ordering behavior."""

    @pytest.mark.asyncio
    async def test_claim_orders_by_available_at_for_pending_rows(
        self, session_factory
    ) -> None:
        """Pending rows should be claimed in available_at order."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        now = datetime.now(timezone.utc)

        # Create three pending events with different available_at
        async with session_factory() as session:
            row1 = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:order:1",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
                available_at=now - timedelta(minutes=10),  # Oldest
            )
            row2 = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:order:2",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
                available_at=now - timedelta(minutes=5),  # Middle
            )
            row3 = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:order:3",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
                available_at=now - timedelta(minutes=1),  # Newest
            )
            session.add_all([row1, row2, row3])
            await session.commit()

        # Claim should get oldest first
        claimed, _ = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )
        assert len(claimed) == 1
        assert claimed[0].event.idempotency_key == "test:order:1"

        # Complete and claim again
        await store.mark_completed(claimed[0].event.outbox_id, claimed[0].fencing_token)

        claimed, _ = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )
        assert len(claimed) == 1
        assert claimed[0].event.idempotency_key == "test:order:2"

    @pytest.mark.asyncio
    async def test_claim_orders_processing_by_lease_expiry(
        self, session_factory
    ) -> None:
        """Processing rows should be claimed in lease_expires_at order."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        now = datetime.now(timezone.utc)

        # Create two processing rows with expired leases
        async with session_factory() as session:
            row1 = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:proc:1",
                payload={},
                state="processing",
                attempt_count=1,
                max_attempts=8,
                lease_owner="old-worker:1",
                lease_expires_at=now - timedelta(minutes=30),  # Older expiry
                available_at=now + timedelta(hours=1),  # Future available_at
            )
            row2 = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:proc:2",
                payload={},
                state="processing",
                attempt_count=1,
                max_attempts=8,
                lease_owner="old-worker:2",
                lease_expires_at=now - timedelta(minutes=15),  # Newer expiry
                available_at=now + timedelta(hours=1),  # Future available_at
            )
            session.add_all([row1, row2])
            await session.commit()

        # Claim should get row with oldest lease expiry first
        claimed, _ = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )
        assert len(claimed) == 1
        assert claimed[0].event.idempotency_key == "test:proc:1"

    @pytest.mark.asyncio
    async def test_claim_orders_mixed_pending_and_processing(
        self, session_factory
    ) -> None:
        """Claim should order by due time regardless of state."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        now = datetime.now(timezone.utc)

        # Create pending and processing rows with different due times
        async with session_factory() as session:
            # Pending row due now
            pending_now = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:mix:pending-now",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
                available_at=now - timedelta(minutes=5),
            )
            # Processing row with older lease expiry
            processing_old = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:mix:processing-old",
                payload={},
                state="processing",
                attempt_count=1,
                max_attempts=8,
                lease_owner="old-worker",
                lease_expires_at=now - timedelta(minutes=10),  # Due before pending
                available_at=now + timedelta(hours=1),
            )
            session.add_all([pending_now, processing_old])
            await session.commit()

        # Should claim processing row first (older lease expiry)
        claimed, _ = await store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
        )
        assert len(claimed) == 1
        assert claimed[0].event.idempotency_key == "test:mix:processing-old"


__all__ = ["TestClaimOrdering"]
