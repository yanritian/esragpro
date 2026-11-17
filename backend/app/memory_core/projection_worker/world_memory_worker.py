"""World-memory projection worker (P6-C).

Wraps :class:`WorldMemoryProjectionStore` (a session-factory facade over
the P6-B repository) with handler dispatch, delivery timeout, and
retry/dead-letter classification. The worker is replay-safe:

- Claim is committed before handlers run.
- Terminal state writes use independent sessions so a rollback cannot
  un-claim a row.
- ``ProjectionEvent`` carries a defensive deep copy of the full P7-B
  envelope; handler mutations cannot affect the stored payload.
- ``PermanentProjectionError`` dead-letters immediately; timeouts and
  other exceptions are retryable until the P6-B exhaustion rule
  dead-letters the final attempt.
- A lost lease never overwrites a newer claimant — the repository's
  fencing guard returns ``lease_lost``.

This worker does not register or modify legacy ``MemoryItem`` handlers
and does not provide a process entrypoint.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import anyio

from app.memory_core.projection_worker.contracts import (
    ClaimedEvent,
    PermanentProjectionError,
    ProjectionHandler,
    ProjectionHandlerRegistry,
    WorkerResult,
)
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.projection_worker.world_memory_store import (
    WorldMemoryProjectionStore,
)

logger = logging.getLogger(__name__)

_SAFE_NAME_MAX_LEN = 64


def _safe_exception_name(exc: BaseException) -> str:
    raw = type(exc).__name__
    safe = "".join(ch for ch in raw if ch.isalnum() or ch == "_")
    if not safe:
        return "Exception"
    return safe[:_SAFE_NAME_MAX_LEN]


class WorldMemoryProjectionWorker:
    """World-memory projection worker.

    The world-memory worker **requires** an explicit ``target_filter``:
    the registry must be non-empty and the filter must equal the
    registry's targets exactly. An accidentally incomplete registry
    cannot consume another target's work.
    """

    def __init__(
        self,
        store: WorldMemoryProjectionStore,
        registry: ProjectionHandlerRegistry,
        worker_identity: str,
        *,
        target_filter: set[str] | frozenset[str],
        batch_size: int = 10,
        lease_duration: timedelta = timedelta(seconds=30),
        delivery_timeout: timedelta = timedelta(seconds=10),
        retry_policy: ExponentialBackoffPolicy | None = None,
    ) -> None:
        if not worker_identity or not worker_identity.strip():
            raise ValueError("worker_identity must be non-empty")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if delivery_timeout <= timedelta(0):
            raise ValueError("delivery_timeout must be positive")
        if lease_duration <= delivery_timeout:
            raise ValueError(
                "lease_duration must be greater than delivery_timeout "
                "to leave margin for terminal state writes"
            )
        if not target_filter:
            raise ValueError(
                "target_filter is required for the world-memory worker; "
                "pass the explicit set of targets this worker should claim"
            )

        registry_targets = set(registry.targets)
        filter_set = set(target_filter)
        if filter_set != registry_targets:
            missing = registry_targets - filter_set
            extra = filter_set - registry_targets
            raise ValueError(
                "target_filter must equal registry.targets exactly; "
                f"missing_from_filter={sorted(missing)}, "
                f"extra_in_filter={sorted(extra)}"
            )

        self._store = store
        self._registry = registry
        self._worker_identity = worker_identity.strip()
        self._target_filter = frozenset(target_filter)
        self._batch_size = batch_size
        self._lease_duration = lease_duration
        self._delivery_timeout = delivery_timeout
        self._retry_policy = retry_policy or ExponentialBackoffPolicy(
            base_delay_seconds=1.0,
            multiplier=2.0,
            max_delay_seconds=60.0,
        )
        self._run_lock = anyio.Lock()

    @property
    def worker_identity(self) -> str:
        return self._worker_identity

    @property
    def target_filter(self) -> frozenset[str]:
        return self._target_filter

    async def run_once(self) -> WorkerResult:
        """Claim one batch, dispatch handlers, and record outcomes."""
        async with self._run_lock:
            return await self._run_once_internal()

    async def _run_once_internal(self) -> WorkerResult:
        result = WorkerResult()

        # The world-memory worker is scoped to a single target per
        # invocation when multiple targets are registered — callers
        # should construct one worker per target, or pass a singleton
        # target filter. For this slice we claim per-target in turn so
        # the batch_size applies to each target independently.
        for target in sorted(self._target_filter):
            try:
                claimed, _ = await self._store.claim_batch(
                    batch_size=self._batch_size,
                    lease_duration=self._lease_duration,
                    target_filter=target,
                )
            except Exception:
                logger.exception(
                    "world-memory worker failed to claim batch for target %s",
                    target,
                )
                continue

            if not claimed:
                continue

            result.claimed += len(claimed)
            await self._dispatch_batch(claimed, result)

        return result

    async def _dispatch_batch(
        self,
        claimed: list[ClaimedEvent],
        result: WorkerResult,
    ) -> None:
        for claimed_event in claimed:
            await self._process_single(claimed_event, result)

    async def _process_single(
        self,
        claimed_event: ClaimedEvent,
        result: WorkerResult,
    ) -> None:
        event = claimed_event.event
        handler: ProjectionHandler | None = self._registry.get_handler(
            event.projection_target
        )

        handler_error: str | None = None
        permanent = False

        if handler is None:
            logger.warning(
                "No handler registered for world-memory target %s "
                "(event %s) — dead-lettering",
                event.projection_target,
                event.outbox_id,
            )
            handler_error = "unknown_projection_target"
            permanent = True
        else:
            try:
                with anyio.fail_after(
                    self._delivery_timeout.total_seconds()
                ):
                    await handler(event)
            except TimeoutError:
                logger.warning(
                    "World-memory handler timed out for event %s",
                    event.outbox_id,
                )
                handler_error = "handler_timeout"
            except PermanentProjectionError:
                logger.error(
                    "World-memory permanent projection error for event %s",
                    event.outbox_id,
                )
                handler_error = "permanent_projection_error"
                permanent = True
            except Exception as e:  # noqa: BLE001
                reason = f"handler_error:{_safe_exception_name(e)}"
                logger.error(
                    "World-memory handler error for event %s: %s",
                    event.outbox_id,
                    reason,
                )
                handler_error = reason

        try:
            if handler_error is None:
                success = await self._store.mark_completed(
                    event.outbox_id,
                    fencing_token=claimed_event.fencing_token,
                )
                if success:
                    result.completed += 1
                else:
                    result.lease_lost += 1
            else:
                delay: timedelta | None = None
                if not permanent:
                    delay = timedelta(
                        seconds=self._retry_policy.compute_delay(
                            event.attempt_count
                        )
                    )
                disposition = await self._store.mark_failed(
                    event.outbox_id,
                    fencing_token=claimed_event.fencing_token,
                    error=handler_error,
                    permanent=permanent,
                    retry_delay=delay,
                )
                if disposition.retried:
                    result.retried += 1
                elif disposition.dead_lettered:
                    result.dead_lettered += 1
                elif disposition.lease_lost:
                    result.lease_lost += 1
        except Exception as e:  # noqa: BLE001
            reason = f"state_transition_error:{_safe_exception_name(e)}"
            logger.error(
                "World-memory state transition error for event %s: %s",
                event.outbox_id,
                reason,
            )
            result.state_write_errors += 1


__all__ = ["WorldMemoryProjectionWorker"]
