"""Projection worker constructor validation tests (P1-04)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from app.memory_core.projection_worker.contracts import ProjectionHandlerRegistry
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.projection_worker.worker import ProjectionWorker


class TestProjectionWorkerConstructor:
    """Tests for worker constructor validation."""

    def test_rejects_empty_worker_identity(self) -> None:
        """Worker should reject empty identity."""
        store = MagicMock(spec=ProjectionOutboxStore)
        registry = MagicMock(spec=ProjectionHandlerRegistry)
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        with pytest.raises(ValueError, match="worker_identity must be non-empty"):
            ProjectionWorker(
                store=store, registry=registry, worker_identity="",
                batch_size=5, max_concurrency=10,
                lease_duration=timedelta(seconds=60), delivery_timeout=timedelta(seconds=30),
                finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
            )

    def test_rejects_non_positive_lease_duration(self) -> None:
        """Worker should reject non-positive lease_duration."""
        store = MagicMock(spec=ProjectionOutboxStore)
        registry = MagicMock(spec=ProjectionHandlerRegistry)
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        with pytest.raises(ValueError, match="lease_duration must be positive"):
            ProjectionWorker(
                store=store, registry=registry, worker_identity="test-worker",
                batch_size=5, max_concurrency=10,
                lease_duration=timedelta(seconds=0),
                delivery_timeout=timedelta(seconds=30),
                finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
            )

    def test_rejects_non_positive_delivery_timeout(self) -> None:
        """Worker should reject non-positive delivery_timeout."""
        store = MagicMock(spec=ProjectionOutboxStore)
        registry = MagicMock(spec=ProjectionHandlerRegistry)
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        with pytest.raises(ValueError, match="delivery_timeout must be positive"):
            ProjectionWorker(
                store=store, registry=registry, worker_identity="test-worker",
                batch_size=5, max_concurrency=10,
                lease_duration=timedelta(seconds=60),
                delivery_timeout=timedelta(seconds=0),
                finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
            )

    def test_rejects_non_positive_finalization_margin(self) -> None:
        """Worker should reject non-positive finalization_margin."""
        store = MagicMock(spec=ProjectionOutboxStore)
        registry = MagicMock(spec=ProjectionHandlerRegistry)
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        with pytest.raises(ValueError, match="finalization_margin must be positive"):
            ProjectionWorker(
                store=store, registry=registry, worker_identity="test-worker",
                batch_size=5, max_concurrency=10,
                lease_duration=timedelta(seconds=60),
                delivery_timeout=timedelta(seconds=30),
                finalization_margin=timedelta(seconds=0), retry_policy=retry_policy,
            )

    def test_rejects_batch_size_greater_than_max_concurrency(self) -> None:
        """Worker should reject batch_size > max_concurrency."""
        store = MagicMock(spec=ProjectionOutboxStore)
        registry = MagicMock(spec=ProjectionHandlerRegistry)
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        with pytest.raises(ValueError, match="batch_size .* must be <= max_concurrency"):
            ProjectionWorker(
                store=store, registry=registry, worker_identity="test-worker",
                batch_size=20, max_concurrency=10,
                lease_duration=timedelta(seconds=60), delivery_timeout=timedelta(seconds=30),
                finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
            )

    def test_accepts_batch_size_equals_max_concurrency(self) -> None:
        """Worker should accept batch_size == max_concurrency."""
        store = MagicMock(spec=ProjectionOutboxStore)

        async def handler(event):
            pass

        registry = ProjectionHandlerRegistry({"redis": handler})
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        worker = ProjectionWorker(
            store=store, registry=registry, worker_identity="test-worker",
            batch_size=10, max_concurrency=10,
            lease_duration=timedelta(seconds=60), delivery_timeout=timedelta(seconds=30),
            finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
            target_filter={"redis"},
        )

        assert worker._batch_size == 10
        assert worker._max_concurrency == 10

    def test_rejects_lease_duration_not_greater_than_timeout_plus_margin(self) -> None:
        """Worker should reject lease_duration <= delivery_timeout + finalization_margin."""
        store = MagicMock(spec=ProjectionOutboxStore)
        registry = MagicMock(spec=ProjectionHandlerRegistry)
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        with pytest.raises(ValueError, match="lease_duration .* must be >"):
            ProjectionWorker(
                store=store, registry=registry, worker_identity="test-worker",
                batch_size=5, max_concurrency=10,
                lease_duration=timedelta(seconds=30),
                delivery_timeout=timedelta(seconds=30),
                finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
            )

    def test_accepts_lease_duration_strictly_greater(self) -> None:
        """Worker should accept lease_duration > delivery_timeout + finalization_margin."""
        store = MagicMock(spec=ProjectionOutboxStore)

        async def handler(event):
            pass

        registry = ProjectionHandlerRegistry({"redis": handler})
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        worker = ProjectionWorker(
            store=store, registry=registry, worker_identity="test-worker",
            batch_size=5, max_concurrency=10,
            lease_duration=timedelta(seconds=36),
            delivery_timeout=timedelta(seconds=30),
            finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
            target_filter={"redis"},
        )

        assert worker._lease_duration == timedelta(seconds=36)
