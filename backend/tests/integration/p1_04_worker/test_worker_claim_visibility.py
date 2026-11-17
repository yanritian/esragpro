"""Integration tests for worker claim visibility (P1-04).

Tests that claim transactions are visible before handler I/O and
no row locks are held during handler execution.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models.memory_plane import MemoryProjectionOutbox as OutboxRow
from app.memory_core.projection_worker import (
    ProjectionWorker,
    ProjectionHandlerRegistry,
    ProjectionEvent,
)
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy


class TestWorkerClaimVisibility:
    """Tests for claim visibility and lock behavior during worker execution."""

    @pytest.mark.asyncio
    async def test_worker_claim_visible_before_handler_io(
        self, session_factory
    ) -> None:
        """Real worker: fresh session sees processing state before handler records side effect."""
        outbox_id = uuid4()

        async with session_factory() as session:
            row = OutboxRow(
                id=outbox_id,
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:visibility-before-io",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
            )
            session.add(row)
            await session.commit()

        side_effects: list[str] = []

        async def handler(event: ProjectionEvent) -> None:
            # BEFORE recording any side effect, verify claim is visible
            async with session_factory() as fresh_session:
                result = await fresh_session.execute(
                    select(OutboxRow).where(OutboxRow.id == event.outbox_id)
                )
                row = result.scalar_one()

                assert row.state == "processing"
                assert row.attempt_count == 1
                assert row.lease_owner is not None
                assert "test-worker" in row.lease_owner
                assert row.lease_expires_at is not None

            side_effects.append(f"processed:{event.outbox_id}")

        registry = ProjectionHandlerRegistry({"redis": handler})
        store = ProjectionOutboxStore(session_factory, "test-worker")
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

        result = await worker.run_once()
        assert result.claimed == 1
        assert result.completed == 1
        assert len(side_effects) == 1

    @pytest.mark.asyncio
    async def test_no_worker_row_lock_during_handler_execution(
        self, session_factory
    ) -> None:
        """Fresh session can acquire FOR UPDATE NOWAIT on row while handler executes."""
        outbox_id = uuid4()

        async with session_factory() as session:
            row = OutboxRow(
                id=outbox_id,
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key="test:no-lock-during-handler",
                payload={},
                state="pending",
                attempt_count=0,
                max_attempts=8,
            )
            session.add(row)
            await session.commit()

        handler_ran = False

        async def handler(event: ProjectionEvent) -> None:
            nonlocal handler_ran
            # While handler executes, fresh session can acquire NOWAIT lock
            async with session_factory() as fresh_session:
                result = await fresh_session.execute(
                    select(OutboxRow)
                    .where(OutboxRow.id == event.outbox_id)
                    .with_for_update(nowait=True)
                )
                row = result.scalar_one()
                assert row.state == "processing"
            handler_ran = True

        registry = ProjectionHandlerRegistry({"redis": handler})
        store = ProjectionOutboxStore(session_factory, "test-worker")
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

        result = await worker.run_once()
        assert result.claimed == 1
        assert result.completed == 1
        assert handler_ran


__all__ = ["TestWorkerClaimVisibility"]
