"""Integration tests for projection worker end-to-end (P1-04).

Tests worker with real PostgreSQL and handler dispatch.
"""

from __future__ import annotations

import asyncio
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


class FakeHandler:
    """Fake projection handler for testing."""

    def __init__(self, should_fail: bool = False, should_timeout: bool = False):
        self.called = False
        self.events: list[ProjectionEvent] = []
        self.should_fail = should_fail
        self.should_timeout = should_timeout

    async def __call__(self, event: ProjectionEvent) -> None:
        self.called = True
        self.events.append(event)
        if self.should_timeout:
            await asyncio.sleep(10)  # Longer than delivery_timeout
        if self.should_fail:
            raise RuntimeError("Simulated handler failure")


class TestWorkerEndToEnd:
    """Tests for worker with real PostgreSQL."""

    @pytest.mark.asyncio
    async def test_target_filter_processes_only_matching_targets(
        self, session_factory
    ) -> None:
        """Redis-only registry with target_filter={'redis'} processes only Redis rows."""
        # Create handler registry with only Redis handler
        redis_handler = FakeHandler()
        registry = ProjectionHandlerRegistry({"redis": redis_handler})

        # Create events for all three targets
        async with session_factory() as session:
            for target in ["redis", "elasticsearch", "neo4j"]:
                row = OutboxRow(
                    id=uuid4(),
                    aggregate_id=uuid4(),
                    event_type="memory.remembered",
                    schema_version=1,
                    projection_target=target,
                    idempotency_key=f"test:filter:{target}",
                    payload={"target": target},
                    state="pending",
                    attempt_count=0,
                    max_attempts=8,
                )
                session.add(row)
            await session.commit()

        # Create worker with target_filter
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

        # Run once
        result = await worker.run_once()

        # Only Redis should be claimed
        assert result.claimed == 1
        assert result.completed == 1

        # Verify handler was called
        assert redis_handler.called
        assert len(redis_handler.events) == 1
        assert redis_handler.events[0].projection_target == "redis"

        # Verify other targets still pending
        async with session_factory() as session:
            for target in ["elasticsearch", "neo4j"]:
                result_q = await session.execute(
                    select(OutboxRow).where(OutboxRow.projection_target == target)
                )
                row = result_q.scalar_one()
                assert row.state == "pending"

    @pytest.mark.asyncio
    async def test_three_targets_complete_idempotently(
        self, session_factory
    ) -> None:
        """Complete registry worker cycle processes all three targets idempotently."""
        # Create registry with handlers for all three targets
        redis_handler = FakeHandler()
        es_handler = FakeHandler()
        neo4j_handler = FakeHandler()
        registry = ProjectionHandlerRegistry({
            "redis": redis_handler,
            "elasticsearch": es_handler,
            "neo4j": neo4j_handler,
        })

        # Create three events
        async with session_factory() as session:
            for target in ["redis", "elasticsearch", "neo4j"]:
                row = OutboxRow(
                    id=uuid4(),
                    aggregate_id=uuid4(),
                    event_type="memory.remembered",
                    schema_version=1,
                    projection_target=target,
                    idempotency_key=f"test:complete:{target}",
                    payload={},
                    state="pending",
                    attempt_count=0,
                    max_attempts=8,
                )
                session.add(row)
            await session.commit()

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
            target_filter={"redis", "elasticsearch", "neo4j"},
        )

        # Run once
        result = await worker.run_once()

        # All three should complete
        assert result.claimed == 3
        assert result.completed == 3

        # Verify all handlers called
        assert redis_handler.called
        assert es_handler.called
        assert neo4j_handler.called

        # Second run should claim nothing (idempotent)
        result2 = await worker.run_once()
        assert result2.claimed == 0

    @pytest.mark.asyncio
    async def test_one_failure_leaves_siblings_completed(
        self, session_factory
    ) -> None:
        """One target failure leaves successful siblings completed; failed row pending."""
        # Create registry where ES fails
        redis_handler = FakeHandler()
        es_handler = FakeHandler(should_fail=True)
        neo4j_handler = FakeHandler()
        registry = ProjectionHandlerRegistry({
            "redis": redis_handler,
            "elasticsearch": es_handler,
            "neo4j": neo4j_handler,
        })

        # Create three events
        async with session_factory() as session:
            for target in ["redis", "elasticsearch", "neo4j"]:
                row = OutboxRow(
                    id=uuid4(),
                    aggregate_id=uuid4(),
                    event_type="memory.remembered",
                    schema_version=1,
                    projection_target=target,
                    idempotency_key=f"test:partial:{target}",
                    payload={},
                    state="pending",
                    attempt_count=0,
                    max_attempts=8,
                )
                session.add(row)
            await session.commit()

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
            target_filter={"redis", "elasticsearch", "neo4j"},
        )

        # Run once
        result = await worker.run_once()

        # All three should be claimed
        assert result.claimed == 3
        # Two should complete, one should retry
        assert result.completed == 2
        assert result.retried == 1

        # Verify Redis and Neo4j are completed
        async with session_factory() as session:
            for target in ["redis", "neo4j"]:
                result_q = await session.execute(
                    select(OutboxRow).where(OutboxRow.projection_target == target)
                )
                row = result_q.scalar_one()
                assert row.state == "completed"

            # ES should be pending (retryable failure)
            result_es = await session.execute(
                select(OutboxRow).where(OutboxRow.projection_target == "elasticsearch")
            )
            row_es = result_es.scalar_one()
            assert row_es.state == "pending"
            assert row_es.attempt_count == 1


__all__ = ["TestWorkerEndToEnd"]