"""Worker behavior tests for concurrency and advanced scenarios (P1-04)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.memory_core.projection_worker import ProjectionWorker, run_polling_loop
from app.memory_core.projection_worker.contracts import ProjectionHandlerRegistry
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from tests.unit.test_worker_behavior_helpers import make_claimed_event


class TestConcurrencyLimits:
    """Tests for concurrency enforcement."""

    @pytest.mark.asyncio
    async def test_active_handler_count_never_exceeds_max_concurrency(self) -> None:
        """Active handler count must reach (not merely stay <=) max_concurrency."""
        store = MagicMock(spec=ProjectionOutboxStore)
        events = [make_claimed_event() for _ in range(3)]
        store.claim_batch = AsyncMock(return_value=(events, 0))
        store.mark_completed = AsyncMock(return_value=True)

        active_count = 0
        max_seen = 0
        all_started = asyncio.Event()
        release = asyncio.Event()

        async def handler(event):
            nonlocal active_count, max_seen
            active_count += 1
            max_seen = max(max_seen, active_count)
            if active_count == 3:
                all_started.set()
            try:
                await release.wait()
            finally:
                active_count -= 1

        registry = ProjectionHandlerRegistry({"redis": handler})
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        worker = ProjectionWorker(
            store=store, registry=registry, worker_identity="test-worker",
            batch_size=3, max_concurrency=3,
            lease_duration=timedelta(seconds=60), delivery_timeout=timedelta(seconds=30),
            finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
            target_filter={"redis"},
        )

        result = None
        task = asyncio.create_task(worker.run_once())
        try:
            # A sequential implementation would never set all_started, so this
            # bounded timeout forces the test to fail instead of hanging.
            await asyncio.wait_for(all_started.wait(), timeout=2.0)

            # While all 3 handlers are still blocked on ``release``, prove the
            # worker actually dispatched them concurrently up to the bound.
            assert max_seen == 3
            assert max_seen <= 3
        finally:
            release.set()
            try:
                result = await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass

        assert result is not None
        assert result.completed == 3
        assert active_count == 0


class TestRunOnceSerialization:
    """Tests for run_once() serialization."""

    @pytest.mark.asyncio
    async def test_overlapping_run_once_calls_serialized(self) -> None:
        """Overlapping run_once() calls should be serialized."""
        store = MagicMock(spec=ProjectionOutboxStore)
        call_times = []

        async def claim_batch_delayed(*args, **kwargs):
            call_times.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.05)
            return ([], 0)

        store.claim_batch = claim_batch_delayed

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

        task1 = asyncio.create_task(worker.run_once())
        task2 = asyncio.create_task(worker.run_once())
        await asyncio.gather(task1, task2)

        assert len(call_times) == 2
        assert call_times[1] >= call_times[0] + 0.04


class TestCancellationPropagation:
    """Tests for cancellation behavior."""

    @pytest.mark.asyncio
    async def test_cancellation_no_transition(self) -> None:
        """Cancellation propagates and invokes no transition."""
        store = MagicMock(spec=ProjectionOutboxStore)
        store.claim_batch = AsyncMock(return_value=([make_claimed_event()], 0))
        store.mark_completed = AsyncMock(return_value=True)

        handler_started = asyncio.Event()

        async def handler(event):
            handler_started.set()
            await asyncio.sleep(1000)

        registry = ProjectionHandlerRegistry({"redis": handler})
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        worker = ProjectionWorker(
            store=store, registry=registry, worker_identity="test-worker",
            batch_size=1, max_concurrency=1,
            lease_duration=timedelta(seconds=60), delivery_timeout=timedelta(seconds=30),
            finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
            target_filter={"redis"},
        )

        task = asyncio.create_task(worker.run_once())
        await handler_started.wait()
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        store.mark_completed.assert_not_called()
        store.mark_failed.assert_not_called()


class TestStopEventInterrupt:
    """Tests for stop event interrupting polling."""

    @pytest.mark.asyncio
    async def test_stop_event_interrupts_idle_wait(self) -> None:
        """Stop event should interrupt idle wait promptly."""
        store = MagicMock(spec=ProjectionOutboxStore)
        store.claim_batch = AsyncMock(return_value=([], 0))

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

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_polling_loop(worker, stop_event, idle_wait=timedelta(seconds=10))
        )

        await asyncio.sleep(0.1)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)
