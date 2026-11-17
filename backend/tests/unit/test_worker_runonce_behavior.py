"""Projection worker run_once behavior tests (P1-04)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.memory_core.projection_worker.contracts import ProjectionHandlerRegistry
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.projection_worker.worker import ProjectionWorker


class TestProjectionWorkerRunOnce:
    """Tests for run_once() behavior."""

    @pytest.fixture
    def mock_store(self) -> MagicMock:
        """Create mock store."""
        store = MagicMock(spec=ProjectionOutboxStore)
        store.claim_batch = AsyncMock(return_value=([], 0))
        store.mark_completed = AsyncMock(return_value=True)
        store.mark_failed = AsyncMock(return_value=True)
        return store

    @pytest.fixture
    def mock_registry(self) -> ProjectionHandlerRegistry:
        """Create real registry with a test handler."""
        async def handler(event):
            pass

        return ProjectionHandlerRegistry({"redis": handler})

    @pytest.fixture
    def retry_policy(self) -> ExponentialBackoffPolicy:
        """Create retry policy."""
        return ExponentialBackoffPolicy(base_delay_seconds=1.0)

    @pytest.fixture
    def worker(
        self,
        mock_store: MagicMock,
        mock_registry: ProjectionHandlerRegistry,
        retry_policy: ExponentialBackoffPolicy,
    ) -> ProjectionWorker:
        """Create worker with mocks."""
        return ProjectionWorker(
            store=mock_store, registry=mock_registry, worker_identity="test-worker",
            batch_size=5, max_concurrency=10,
            lease_duration=timedelta(seconds=60), delivery_timeout=timedelta(seconds=30),
            finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
            target_filter={"redis"},
        )

    @pytest.mark.asyncio
    async def test_run_once_returns_empty_result_when_no_events(
        self,
        worker: ProjectionWorker,
        mock_store: MagicMock,
    ) -> None:
        """run_once() should return empty result when no events claimed."""
        mock_store.claim_batch = AsyncMock(return_value=([], 0))

        result = await worker.run_once()

        assert result.claimed == 0
        assert result.completed == 0
        assert result.total_processed == 0

    @pytest.mark.asyncio
    async def test_run_once_records_exhausted_recovered(
        self,
        worker: ProjectionWorker,
        mock_store: MagicMock,
    ) -> None:
        """run_once() should record exhausted_recovered count."""
        mock_store.claim_batch = AsyncMock(return_value=([], 5))

        result = await worker.run_once()

        assert result.exhausted_recovered == 5


class TestProjectionWorkerConcurrency:
    """Tests for concurrent handler execution."""

    def test_batch_size_respects_max_concurrency_invariant(self) -> None:
        """Worker should enforce batch_size <= max_concurrency."""
        async def handler(event):
            pass

        registry = ProjectionHandlerRegistry({"redis": handler})
        store = MagicMock(spec=ProjectionOutboxStore)
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        worker1 = ProjectionWorker(
            store=store, registry=registry, worker_identity="test-worker",
            batch_size=5, max_concurrency=5,
            lease_duration=timedelta(seconds=60), delivery_timeout=timedelta(seconds=30),
            finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
            target_filter={"redis"},
        )
        assert worker1._batch_size == 5

        with pytest.raises(ValueError):
            ProjectionWorker(
                store=store, registry=registry, worker_identity="test-worker",
                batch_size=10, max_concurrency=5,
                lease_duration=timedelta(seconds=60), delivery_timeout=timedelta(seconds=30),
                finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
                target_filter={"redis"},
            )
