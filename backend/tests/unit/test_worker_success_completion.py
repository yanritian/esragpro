"""Worker behavior tests for successful completion (P1-04)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.memory_core.projection_worker.contracts import ProjectionHandlerRegistry
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.projection_worker.worker import ProjectionWorker
from tests.unit.test_worker_behavior_helpers import make_claimed_event


class TestSuccessfulHandlerCompletion:
    """Tests for successful handler completion."""

    @pytest.mark.asyncio
    async def test_successful_handler_calls_mark_completed(self) -> None:
        """Successful handler should call mark_completed and increment completed."""
        store = MagicMock(spec=ProjectionOutboxStore)
        store.claim_batch = AsyncMock(return_value=([make_claimed_event()], 0))
        store.mark_completed = AsyncMock(return_value=True)

        async def handler(event):
            pass

        registry = ProjectionHandlerRegistry({"redis": handler})
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        worker = ProjectionWorker(
            store=store, registry=registry, worker_identity="test-worker",
            batch_size=5, max_concurrency=10,
            lease_duration=timedelta(seconds=60), delivery_timeout=timedelta(seconds=30),
            finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
            target_filter={"redis"},
        )

        result = await worker.run_once()

        assert result.completed == 1
        assert result.retried == 0
        assert result.dead_lettered == 0
        store.mark_completed.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_handler_completes_multiple_events(self) -> None:
        """Multiple successful handlers should all complete."""
        store = MagicMock(spec=ProjectionOutboxStore)
        events = [make_claimed_event() for _ in range(3)]
        store.claim_batch = AsyncMock(return_value=(events, 0))
        store.mark_completed = AsyncMock(return_value=True)

        async def handler(event):
            pass

        registry = ProjectionHandlerRegistry({"redis": handler})
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        worker = ProjectionWorker(
            store=store, registry=registry, worker_identity="test-worker",
            batch_size=5, max_concurrency=10,
            lease_duration=timedelta(seconds=60), delivery_timeout=timedelta(seconds=30),
            finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
            target_filter={"redis"},
        )

        result = await worker.run_once()

        assert result.completed == 3
        assert store.mark_completed.call_count == 3
