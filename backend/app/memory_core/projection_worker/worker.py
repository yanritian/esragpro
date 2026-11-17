"""Projection worker implementation (P1-04).

Claims events, dispatches to handlers, and records outcomes.
No lease heartbeat - uses batch_size <= max_concurrency safety.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from app.memory_core.projection_worker.contracts import (
    ClaimedEvent,
    ProjectionHandlerRegistry,
    WorkerResult,
    PermanentProjectionError,
    FailureDisposition,
)
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore
from app.memory_core.repositories.postgres_outbox import (
    validate_projection_targets,
    DEFAULT_PROJECTION_TARGETS,
)

logger = logging.getLogger(__name__)

_SAFE_NAME_MAX_LEN = 64


def _safe_exception_name(exc: BaseException) -> str:
    """Derive a log-safe exception class name.

    Uses only Python identifier characters (alphanumeric + underscore),
    caps length, and falls back to ``"Exception"`` when the result is empty.
    """
    raw = type(exc).__name__
    safe = "".join(ch for ch in raw if ch.isalnum() or ch == "_")
    if not safe:
        return "Exception"
    return safe[:_SAFE_NAME_MAX_LEN]


class ProjectionWorker:
    """Production projection worker.

    Claims events from outbox, dispatches to handlers, and records outcomes.
    Enforces safety invariants without lease heartbeat.
    """

    def __init__(
        self,
        store: ProjectionOutboxStore,
        registry: ProjectionHandlerRegistry,
        worker_identity: str,
        batch_size: int,
        max_concurrency: int,
        lease_duration: timedelta,
        delivery_timeout: timedelta,
        finalization_margin: timedelta,
        retry_policy: ExponentialBackoffPolicy,
        target_filter: set[str] | None = None,
    ) -> None:
        if not worker_identity or not worker_identity.strip():
            raise ValueError("worker_identity must be non-empty")

        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")

        if batch_size > max_concurrency:
            raise ValueError(
                f"batch_size ({batch_size}) must be <= max_concurrency "
                f"({max_concurrency})"
            )

        # Validate each duration is positive before comparing their relationship
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if delivery_timeout <= timedelta(0):
            raise ValueError("delivery_timeout must be positive")
        if finalization_margin <= timedelta(0):
            raise ValueError("finalization_margin must be positive")

        # Now validate relationship between durations
        if lease_duration <= delivery_timeout + finalization_margin:
            raise ValueError(
                f"lease_duration ({lease_duration}) must be > "
                f"delivery_timeout ({delivery_timeout}) + "
                f"finalization_margin ({finalization_margin})"
            )

        # Validate registry targets using P1-03 lowercase-safe grammar
        registered_targets = list(registry.targets)
        try:
            validated_targets = validate_projection_targets(registered_targets)
        except (TypeError, ValueError) as e:
            raise ValueError(f"Invalid registry target: {e}") from e

        # Validate target filter invariants
        registry_set = set(validated_targets)
        default_targets_set = set(DEFAULT_PROJECTION_TARGETS)

        if target_filter is not None:
            if not target_filter:
                raise ValueError("target_filter cannot be empty if provided")

            # Validate filter target names using P1-03 grammar
            try:
                validated_filter_list = validate_projection_targets(list(target_filter))
            except (TypeError, ValueError) as e:
                raise ValueError(f"Invalid target_filter: {e}") from e

            # Filter must equal registry.targets, not just be a subset
            filter_set = set(validated_filter_list)
            if filter_set != registry_set:
                missing_from_filter = registry_set - filter_set
                extra_in_filter = filter_set - registry_set
                if missing_from_filter:
                    raise ValueError(
                        f"target_filter missing registered targets: {missing_from_filter}. "
                        f"Filter must include all registered targets."
                    )
                if extra_in_filter:
                    raise ValueError(
                        f"target_filter contains unregistered targets: {extra_in_filter}"
                    )
        else:
            # No explicit filter: check registry completeness
            # Only allow catch-all when registry equals all default targets
            if registry_set != default_targets_set:
                raise ValueError(
                    f"Catch-all worker (target_filter=None) requires registry to contain "
                    f"all default targets {set(DEFAULT_PROJECTION_TARGETS)}. "
                    f"Registry has: {registry_set}. "
                    f"Provide explicit target_filter matching registry targets."
                )

        # Freeze the filter to prevent caller mutation
        frozen_filter = frozenset(target_filter) if target_filter else None

        self._store = store
        self._registry = registry
        self._worker_identity = worker_identity.strip()
        self._batch_size = batch_size
        self._max_concurrency = max_concurrency
        self._lease_duration = lease_duration
        self._delivery_timeout = delivery_timeout
        self._finalization_margin = finalization_margin
        self._retry_policy = retry_policy
        self._target_filter = frozen_filter
        self._run_lock = asyncio.Lock()

    async def run_once(self) -> WorkerResult:
        """Claim one batch, dispatch handlers, and record outcomes."""
        async with self._run_lock:
            return await self._run_once_internal()

    async def _run_once_internal(self) -> WorkerResult:
        """Internal implementation of run_once (assumes lock is held)."""
        result = WorkerResult()
        claimed, exhausted_recovered = await self._store.claim_batch(
            batch_size=self._batch_size,
            lease_duration=self._lease_duration,
            target_filter=self._target_filter,
        )
        result.exhausted_recovered = exhausted_recovered

        if not claimed:
            return result

        result.claimed = len(claimed)

        tasks: list[asyncio.Task[None]] = []
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def process_event(claimed_event: ClaimedEvent) -> None:
            async with semaphore:
                await self._process_single_event(claimed_event, result)

        for claimed_event in claimed:
            task = asyncio.create_task(process_event(claimed_event))
            tasks.append(task)

        await asyncio.gather(*tasks, return_exceptions=False)
        return result

    async def _process_single_event(
        self,
        claimed_event: ClaimedEvent,
        result: WorkerResult,
    ) -> None:
        """Process a single claimed event.

        Two-phase execution:
        1. Invoke handler and classify result
        2. Perform exactly one terminal transition

        Transition exceptions are counted but isolated from siblings.
        """
        event = claimed_event.event
        handler_result = None  # None=success, str=error, 'timeout', 'permanent'
        permanent_error = False

        # Phase 1: Execute handler
        try:
            handler = self._registry.get_handler(event.projection_target)

            if handler is None:
                logger.warning(
                    "No handler for target %s, dead-lettering event %s",
                    event.projection_target,
                    event.outbox_id,
                )
                handler_result = "unknown_projection_target"
                permanent_error = True
            else:
                await asyncio.wait_for(
                    handler(event),
                    timeout=self._delivery_timeout.total_seconds(),
                )
                # Handler succeeded
                handler_result = None

        except asyncio.TimeoutError:
            logger.warning("Handler timeout for event %s", event.outbox_id)
            handler_result = "handler_timeout"
            permanent_error = False

        except PermanentProjectionError:
            logger.error(
                "Permanent projection error for event %s: permanent_projection_error",
                event.outbox_id,
            )
            handler_result = "permanent_projection_error"
            permanent_error = True

        except Exception as e:
            reason = f"handler_error:{_safe_exception_name(e)}"
            logger.error(
                "Handler error for event %s: %s",
                event.outbox_id,
                reason,
            )
            handler_result = reason
            permanent_error = False

        # Phase 2: Perform exactly one terminal transition
        try:
            if handler_result is None:
                # Success path
                success = await self._store.mark_completed(
                    outbox_id=event.outbox_id,
                    fencing_token=claimed_event.fencing_token,
                )
                if success:
                    result.completed += 1
                else:
                    result.lease_lost += 1
            else:
                # Failure path
                delay = None
                if not permanent_error:
                    delay = timedelta(
                        seconds=self._retry_policy.compute_delay(event.attempt_count)
                    )

                disposition = await self._store.mark_failed(
                    outbox_id=event.outbox_id,
                    fencing_token=claimed_event.fencing_token,
                    error=handler_result,
                    permanent=permanent_error,
                    retry_delay=delay,
                )
                self._record_disposition(disposition, result)

        except Exception as e:
            # Transition exception: count it, don't invoke second transition,
            # don't cancel siblings, leave row for lease recovery
            reason = f"state_transition_error:{_safe_exception_name(e)}"
            logger.error(
                "State transition error for event %s: %s",
                event.outbox_id,
                reason,
            )
            result.state_write_errors += 1

    def _record_disposition(
        self,
        disposition: FailureDisposition,
        result: WorkerResult,
    ) -> None:
        """Record a failure disposition in the result."""
        if disposition.retried:
            result.retried += 1
        elif disposition.dead_lettered:
            result.dead_lettered += 1
        elif disposition.lease_lost:
            result.lease_lost += 1


__all__ = ["ProjectionWorker"]