"""Worker behavior tests for failures and retry (P1-04)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.memory_core.projection_worker.contracts import (
    ClaimedEvent,
    FailureDisposition,
    PermanentProjectionError,
    ProjectionEvent,
    ProjectionHandlerRegistry,
)
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.projection_worker.worker import ProjectionWorker
from tests.unit.test_worker_behavior_helpers import make_claimed_event


class TestHandlerFailures:
    """Tests for handler failure handling."""

    @pytest.mark.asyncio
    async def test_ordinary_exception_calls_retryable_mark_failed(self) -> None:
        """Ordinary exception should call retryable mark_failed."""
        store = MagicMock(spec=ProjectionOutboxStore)
        store.claim_batch = AsyncMock(return_value=([make_claimed_event()], 0))
        store.mark_failed = AsyncMock(return_value=FailureDisposition.make_retried())

        async def handler(event):
            raise RuntimeError("Handler failed")

        registry = ProjectionHandlerRegistry({"redis": handler})
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
            target_filter={"redis"},
        )

        result = await worker.run_once()

        assert result.retried == 1
        assert result.completed == 0
        call = store.mark_failed.call_args
        assert call.kwargs["permanent"] is False

    @pytest.mark.asyncio
    async def test_timeout_calls_retryable_mark_failed(self) -> None:
        """Timeout should call retryable mark_failed."""
        store = MagicMock(spec=ProjectionOutboxStore)
        store.claim_batch = AsyncMock(return_value=([make_claimed_event()], 0))
        store.mark_failed = AsyncMock(return_value=FailureDisposition.make_retried())

        async def handler(event):
            await asyncio.sleep(10)

        registry = ProjectionHandlerRegistry({"redis": handler})
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)

        worker = ProjectionWorker(
            store=store,
            registry=registry,
            worker_identity="test-worker",
            batch_size=5,
            max_concurrency=10,
            lease_duration=timedelta(seconds=60),
            delivery_timeout=timedelta(seconds=0.1),
            finalization_margin=timedelta(seconds=5),
            retry_policy=retry_policy,
            target_filter={"redis"},
        )

        result = await worker.run_once()
        assert result.retried == 1

    @pytest.mark.asyncio
    async def test_permanent_error_calls_permanent_failure(self) -> None:
        """PermanentProjectionError should cause immediate dead-letter."""
        store = MagicMock(spec=ProjectionOutboxStore)
        store.claim_batch = AsyncMock(return_value=([make_claimed_event()], 0))
        store.mark_failed = AsyncMock(return_value=FailureDisposition.make_dead_lettered())

        async def handler(event):
            raise PermanentProjectionError("Cannot process")

        registry = ProjectionHandlerRegistry({"redis": handler})
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
            target_filter={"redis"},
        )

        result = await worker.run_once()
        assert result.dead_lettered == 1
        call = store.mark_failed.call_args
        assert call.kwargs["permanent"] is True

    @pytest.mark.asyncio
    async def test_unknown_target_with_complete_registry(self) -> None:
        """Unknown target with complete registry should be dead-lettered."""
        store = MagicMock(spec=ProjectionOutboxStore)
        event = make_claimed_event("unknown_target")
        store.claim_batch = AsyncMock(return_value=([event], 0))
        store.mark_failed = AsyncMock(return_value=FailureDisposition.make_dead_lettered())

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

        result = await worker.run_once()
        assert result.dead_lettered == 1
        call = store.mark_failed.call_args
        assert call.kwargs["permanent"] is True

    @pytest.mark.asyncio
    async def test_final_attempt_failure_goes_to_dead_letter(self) -> None:
        """Final attempt failure should result in dead-letter."""
        store = MagicMock(spec=ProjectionOutboxStore)
        event_final = ClaimedEvent(
            event=ProjectionEvent(
                outbox_id=uuid4(),
                aggregate_id=uuid4(),
                event_type="memory.remembered",
                schema_version=1,
                projection_target="redis",
                idempotency_key=f"test:{uuid4()}",
                payload={"test": "data"},
                attempt_count=8,
                max_attempts=8,
            ),
            fencing_token=f"worker:{uuid4()}",
            lease_expires_at=asyncio.get_event_loop().time() + 60,
        )
        store.claim_batch = AsyncMock(return_value=([event_final], 0))
        store.mark_failed = AsyncMock(return_value=FailureDisposition.make_dead_lettered())

        async def handler(event):
            raise RuntimeError("Final failure")

        registry = ProjectionHandlerRegistry({"redis": handler})
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
            target_filter={"redis"},
        )

        result = await worker.run_once()
        assert result.dead_lettered == 1


_SENSITIVE_MESSAGE = (
    "Authorization: Bearer TOPSECRET https://user:password@example.test payload=PRIVATE-CONTENT"
)
_SENSITIVE_TOKENS = ("TOPSECRET", "password", "PRIVATE-CONTENT", "Authorization")


class TestSensitiveDataSanitization:
    """Exception messages containing secrets must not leak to store or logs."""

    @pytest.mark.asyncio
    async def test_ordinary_exception_sanitized(self, caplog: pytest.LogCaptureFixture) -> None:
        """Ordinary exception: persisted error and logs must omit secrets."""
        store = MagicMock(spec=ProjectionOutboxStore)
        store.claim_batch = AsyncMock(return_value=([make_claimed_event()], 0))
        store.mark_failed = AsyncMock(return_value=FailureDisposition.make_retried())

        async def handler(event):
            raise ValueError(_SENSITIVE_MESSAGE)

        registry = ProjectionHandlerRegistry({"redis": handler})
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
            target_filter={"redis"},
        )

        with caplog.at_level(logging.ERROR):
            result = await worker.run_once()

        assert result.retried == 1
        call = store.mark_failed.call_args
        error = call.kwargs["error"]

        # Persisted reason is the stable, safe label
        assert error == "handler_error:ValueError"

        # No secret tokens in persisted error
        for token in _SENSITIVE_TOKENS:
            assert token not in error, f"persisted error leaked {token!r}"

        # No secret tokens in captured log output
        for token in _SENSITIVE_TOKENS:
            assert token not in caplog.text, f"caplog leaked {token!r}"

        # Stable reason appears in logs
        assert "handler_error:ValueError" in caplog.text

    @pytest.mark.asyncio
    async def test_permanent_exception_sanitized(self, caplog: pytest.LogCaptureFixture) -> None:
        """Permanent exception: persisted error and logs must omit secrets."""
        store = MagicMock(spec=ProjectionOutboxStore)
        store.claim_batch = AsyncMock(return_value=([make_claimed_event()], 0))
        store.mark_failed = AsyncMock(return_value=FailureDisposition.make_dead_lettered())

        async def handler(event):
            raise PermanentProjectionError(_SENSITIVE_MESSAGE)

        registry = ProjectionHandlerRegistry({"redis": handler})
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
            target_filter={"redis"},
        )

        with caplog.at_level(logging.ERROR):
            result = await worker.run_once()

        assert result.dead_lettered == 1
        call = store.mark_failed.call_args
        error = call.kwargs["error"]

        # Persisted reason is the stable, safe label
        assert error == "permanent_projection_error"

        # No secret tokens in persisted error
        for token in _SENSITIVE_TOKENS:
            assert token not in error, f"persisted error leaked {token!r}"

        # No secret tokens in captured log output
        for token in _SENSITIVE_TOKENS:
            assert token not in caplog.text, f"caplog leaked {token!r}"

        # Stable reason appears in logs
        assert "permanent_projection_error" in caplog.text
