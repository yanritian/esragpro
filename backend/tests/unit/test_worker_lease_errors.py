"""Worker behavior tests for lease loss and state errors (P1-04)."""

from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.memory_core.projection_worker.contracts import (
    FailureDisposition,
    ProjectionHandlerRegistry,
)
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.projection_worker.worker import ProjectionWorker
from tests.unit.test_worker_behavior_helpers import make_claimed_event


class TestLeaseLoss:
    """Tests for lease loss handling."""

    @pytest.mark.asyncio
    async def test_completion_guard_false_increments_lease_lost(self) -> None:
        """When completion guard fails, should increment lease_lost."""
        store = MagicMock(spec=ProjectionOutboxStore)
        store.claim_batch = AsyncMock(return_value=([make_claimed_event()], 0))
        store.mark_completed = AsyncMock(return_value=False)

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
        assert result.lease_lost == 1
        assert result.completed == 0

    @pytest.mark.asyncio
    async def test_failure_disposition_lease_lost(self) -> None:
        """When failure disposition is lease_lost, should increment it."""
        store = MagicMock(spec=ProjectionOutboxStore)
        store.claim_batch = AsyncMock(return_value=([make_claimed_event()], 0))
        store.mark_failed = AsyncMock(return_value=FailureDisposition.make_lease_lost())

        async def handler(event):
            raise RuntimeError("Handler failed")

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
        assert result.lease_lost == 1
        assert result.retried == 0


class TestStateWriteErrors:
    """Tests for state transition exceptions."""

    @pytest.mark.asyncio
    async def test_completion_exception_increments_state_write_errors(self) -> None:
        """Exception during completion should increment state_write_errors."""
        store = MagicMock(spec=ProjectionOutboxStore)
        store.claim_batch = AsyncMock(return_value=([make_claimed_event()], 0))
        store.mark_completed = AsyncMock(side_effect=RuntimeError("DB error"))

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
        assert result.state_write_errors == 1
        assert result.completed == 0
        store.mark_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_transition_exception_isolated(self) -> None:
        """Failure transition exception should not cancel sibling."""
        store = MagicMock(spec=ProjectionOutboxStore)
        events = [make_claimed_event() for _ in range(2)]
        store.claim_batch = AsyncMock(return_value=(events, 0))

        call_count = 0
        def mark_completed_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return True
            else:
                raise RuntimeError("DB error")

        store.mark_completed = AsyncMock(side_effect=mark_completed_side_effect)

        async def handler(event):
            pass

        registry = ProjectionHandlerRegistry({"redis": handler})
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        worker = ProjectionWorker(
            store=store, registry=registry, worker_identity="test-worker",
            batch_size=2, max_concurrency=2,
            lease_duration=timedelta(seconds=60), delivery_timeout=timedelta(seconds=30),
            finalization_margin=timedelta(seconds=5), retry_policy=retry_policy,
            target_filter={"redis"},
        )

        result = await worker.run_once()
        # One event succeeds, one raises exception
        assert result.state_write_errors == 1
        assert result.completed == 1
        store.mark_failed.assert_not_called()


_SENSITIVE_MESSAGE = (
    "Authorization: Bearer TOPSECRET "
    "https://user:password@example.test "
    "payload=PRIVATE-CONTENT"
)
_SENSITIVE_TOKENS = ("TOPSECRET", "password", "PRIVATE-CONTENT", "Authorization")


class TestStateTransitionSensitiveSanitization:
    """State-transition exception messages containing secrets must not leak."""

    @pytest.mark.asyncio
    async def test_state_transition_exception_sanitized(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """State transition exception: logs must omit secrets, show safe reason."""
        store = MagicMock(spec=ProjectionOutboxStore)
        store.claim_batch = AsyncMock(return_value=([make_claimed_event()], 0))
        store.mark_completed = AsyncMock(
            side_effect=RuntimeError(_SENSITIVE_MESSAGE)
        )

        async def handler(event):
            pass

        registry = ProjectionHandlerRegistry({"redis": handler})
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        worker = ProjectionWorker(
            store=store, registry=registry, worker_identity="test-worker",
            batch_size=5, max_concurrency=10,
            lease_duration=timedelta(seconds=60),
            delivery_timeout=timedelta(seconds=30),
            finalization_margin=timedelta(seconds=5),
            retry_policy=retry_policy,
            target_filter={"redis"},
        )

        with caplog.at_level(logging.ERROR):
            result = await worker.run_once()

        assert result.state_write_errors == 1

        # No secret tokens in captured log output
        for token in _SENSITIVE_TOKENS:
            assert token not in caplog.text, f"caplog leaked {token!r}"

        # Stable reason appears in logs
        assert "state_transition_error:RuntimeError" in caplog.text
