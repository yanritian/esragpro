"""Worker behavior tests for target filter invariants (P1-04)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.memory_core.projection_worker.contracts import (
    ClaimedEvent,
    ProjectionEvent,
    ProjectionHandlerRegistry,
)
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.projection_worker.worker import ProjectionWorker
from tests.unit._p1_04_worker_fakes import FakeHandler
from tests.unit.test_worker_behavior_helpers import make_claimed_event


class TestTargetFilterInvariants:
    """Tests for target filter invariants."""

    def test_incomplete_registry_requires_filter(self) -> None:
        """Incomplete registry without filter should be rejected."""
        store = MagicMock(spec=ProjectionOutboxStore)

        async def handler(event):
            pass

        registry = ProjectionHandlerRegistry({"redis": handler})
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        with pytest.raises(ValueError, match="Catch-all worker"):
            ProjectionWorker(
                store=store,
                registry=registry,
                worker_identity="test-worker",
                batch_size=5,
                max_concurrency=10,
                lease_duration=timedelta(seconds=60),
                delivery_timeout=timedelta(seconds=30),
                finalization_margin=timedelta(seconds=5),
                retry_policy=retry_policy,
            )

    def test_complete_registry_accepts_catch_all(self) -> None:
        """Complete registry should accept catch-all."""
        store = MagicMock(spec=ProjectionOutboxStore)

        async def handler(event):
            pass

        registry = ProjectionHandlerRegistry(
            {
                "redis": handler,
                "elasticsearch": handler,
                "neo4j": handler,
                "minio": handler,
            }
        )
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        worker = ProjectionWorker(
            store=store,
            registry=registry,
            worker_identity="test-worker",
            batch_size=5,
            max_concurrency=10,
            lease_duration=timedelta(seconds=60),
            delivery_timeout=timedelta(seconds=30),
            finalization_margin=timedelta(seconds=5),
            retry_policy=retry_policy,
        )

        assert worker._target_filter is None

    def test_filter_must_match_registry(self) -> None:
        """Filter must exactly match registry targets."""
        store = MagicMock(spec=ProjectionOutboxStore)

        async def handler(event):
            pass

        registry = ProjectionHandlerRegistry({"redis": handler})
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        with pytest.raises(ValueError, match="unregistered targets"):
            ProjectionWorker(
                store=store,
                registry=registry,
                worker_identity="test-worker",
                batch_size=5,
                max_concurrency=10,
                lease_duration=timedelta(seconds=60),
                delivery_timeout=timedelta(seconds=30),
                finalization_margin=timedelta(seconds=5),
                retry_policy=retry_policy,
                target_filter={"redis", "elasticsearch"},
            )

    def test_filter_must_include_all_registry_targets(self) -> None:
        """Filter must include all registry targets."""
        store = MagicMock(spec=ProjectionOutboxStore)

        async def handler(event):
            pass

        registry = ProjectionHandlerRegistry(
            {
                "redis": handler,
                "elasticsearch": handler,
            }
        )
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        with pytest.raises(ValueError, match="missing registered targets"):
            ProjectionWorker(
                store=store,
                registry=registry,
                worker_identity="test-worker",
                batch_size=5,
                max_concurrency=10,
                lease_duration=timedelta(seconds=60),
                delivery_timeout=timedelta(seconds=30),
                finalization_margin=timedelta(seconds=5),
                retry_policy=retry_policy,
                target_filter={"redis"},
            )


class TestIdempotentHandler:
    """Tests for idempotent handler behavior."""

    @pytest.mark.asyncio
    async def test_duplicate_delivery_records_one_side_effect(self) -> None:
        """Duplicate delivery should record only one idempotent side effect."""
        store = MagicMock(spec=ProjectionOutboxStore)

        event1 = make_claimed_event()
        event2 = ClaimedEvent(
            event=ProjectionEvent(
                outbox_id=event1.event.outbox_id,
                aggregate_id=event1.event.aggregate_id,
                event_type=event1.event.event_type,
                schema_version=event1.event.schema_version,
                projection_target="redis",
                idempotency_key=event1.event.idempotency_key,
                payload={"test": "data"},
                attempt_count=event1.event.attempt_count,
                max_attempts=event1.event.max_attempts,
            ),
            fencing_token=f"worker:{uuid4()}",
            lease_expires_at=asyncio.get_event_loop().time() + 60,
        )

        store.claim_batch = AsyncMock(return_value=([event1, event2], 0))
        store.mark_completed = AsyncMock(return_value=True)

        fake_handler = FakeHandler(target_name="redis")

        registry = ProjectionHandlerRegistry({"redis": fake_handler})
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        worker = ProjectionWorker(
            store=store,
            registry=registry,
            worker_identity="test-worker",
            batch_size=2,
            max_concurrency=2,
            lease_duration=timedelta(seconds=60),
            delivery_timeout=timedelta(seconds=30),
            finalization_margin=timedelta(seconds=5),
            retry_policy=retry_policy,
            target_filter={"redis"},
        )

        result = await worker.run_once()

        assert result.completed == 2
        assert fake_handler.state.total_calls() == 2
        assert fake_handler.state.unique_calls() == 1
