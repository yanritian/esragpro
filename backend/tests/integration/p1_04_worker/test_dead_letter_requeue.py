"""Integration tests for dead-letter requeue (P1-04).

Tests requeue of dead-lettered events.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models.memory_plane import MemoryProjectionOutbox as OutboxRow
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore

class TestDeadLetterRequeue:
    """Tests for dead-letter requeue operation."""

    @pytest.mark.asyncio
    async def test_requeue_dead_letter_resets_state(self, session_factory) -> None:
        """Requeue should reset dead_letter to pending with attempt_count=0."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create dead_letter event
        async with session_factory() as session:
            row = OutboxRow(
                id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:requeue",
                payload={},
                state="dead_letter",
                attempt_count=8,
                max_attempts=8,
                last_error="Failed",
            )
            session.add(row)
            await session.commit()

        # Requeue
        success = await store.requeue_dead_letter(row.id)
        assert success

        # Verify reset
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == row.id)
            )
            row = result.scalar_one()
            assert row.state == "pending"
            assert row.attempt_count == 0
            assert row.last_error is None
            assert row.processed_at is None
            assert row.lease_owner is None

    @pytest.mark.asyncio
    async def test_requeue_only_affects_dead_letter(self, session_factory) -> None:
        """Requeue should only affect dead_letter state."""
        store = ProjectionOutboxStore(session_factory, "test-worker")

        # Create completed event
        async with session_factory() as session:
            row = OutboxRow(
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
            session.add(row)
            await session.commit()

        # Try to requeue - should not work
        success = await store.requeue_dead_letter(row.id)
        assert not success

        # Verify unchanged
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.id == row.id)
            )
            row = result.scalar_one()
            assert row.state == "completed"