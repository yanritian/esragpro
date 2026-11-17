"""P6-C worker tests: happy path, full-envelope delivery, permanent
failure, retryable failure, final-attempt dead-letter, and lease-loss
fencing.

These tests prove the world-memory worker boundary:

- Success: handler returns normally → row transitions to completed.
- Full-envelope delivery: handler receives the complete P7-B envelope
  in ``event.payload`` (deep-copied, isolated from stored row).
- Permanent failure: ``PermanentProjectionError`` → immediate
  dead-letter without retry.
- Retryable failure: handler exception → row returns to pending (if
  attempts remain).
- Final-attempt dead-letter: retryable failure after the last allowed
  attempt → dead-letter via P6-B exhaustion rule.
- Lease-loss fencing: a lost lease does not overwrite a newer
  claimant's state; the worker records ``lease_lost``.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.memory_core.projection_worker.contracts import (
    PermanentProjectionError,
    ProjectionEvent,
    ProjectionHandlerRegistry,
    WorkerResult,
)
from app.memory_core.projection_worker.world_memory_store import (
    WorldMemoryProjectionStore,
)
from app.memory_core.projection_worker.world_memory_worker import (
    WorldMemoryProjectionWorker,
)

from .conftest import seed_authority_and_projection


async def _row_state(session_factory, row_id) -> tuple[str, int | None]:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT state, attempt_count FROM "
                "world_memory_projection_outbox WHERE id = :id"
            ),
            {"id": str(row_id)},
        )
        row = result.one()
        return row[0], row[1]


def _make_worker(
    session_factory,
    handlers: dict[str, Any],
    *,
    worker_identity: str = "world-worker-1",
    target_filter: set[str] | None = None,
    batch_size: int = 10,
    delivery_timeout: timedelta = timedelta(seconds=5),
) -> WorldMemoryProjectionWorker:
    store = WorldMemoryProjectionStore(session_factory, worker_identity)
    registry = ProjectionHandlerRegistry(handlers)
    targets = target_filter if target_filter is not None else set(handlers.keys())
    return WorldMemoryProjectionWorker(
        store,
        registry,
        worker_identity,
        target_filter=targets,
        batch_size=batch_size,
        delivery_timeout=delivery_timeout,
    )


# ----------------------------------------------------------------------------
# Success / full-envelope delivery
# ----------------------------------------------------------------------------


async def test_success_marks_completed(session_factory):
    """Handler success transitions row to completed."""
    row = await seed_authority_and_projection(
        session_factory, target="elasticsearch"
    )

    async def ok_handler(event: ProjectionEvent) -> None:
        return None

    worker = _make_worker(
        session_factory,
        {"elasticsearch": ok_handler},
        target_filter={"elasticsearch"},
    )
    result: WorkerResult = await worker.run_once()

    assert result.claimed == 1
    assert result.completed == 1
    assert result.retried == 0
    assert result.dead_lettered == 0
    assert result.lease_lost == 0

    state, _ = await _row_state(session_factory, row.id)
    assert state == "completed"


async def test_handler_receives_full_envelope(session_factory):
    """Handler receives the complete P7-B envelope in event.payload.

    The payload is a defensive deep copy — mutating it must not alter
    the stored row's envelope.
    """
    row = await seed_authority_and_projection(
        session_factory, target="elasticsearch"
    )

    received: dict[str, Any] = {}

    async def capturing_handler(event: ProjectionEvent) -> None:
        received["payload"] = event.payload
        received["target"] = event.projection_target
        received["event_type"] = event.event_type
        received["schema_version"] = event.schema_version
        received["idempotency_key"] = event.idempotency_key
        received["aggregate_id"] = event.aggregate_id
        received["outbox_id"] = event.outbox_id
        received["attempt_count"] = event.attempt_count
        received["max_attempts"] = event.max_attempts
        # Attempt to mutate the payload — must not affect stored row.
        received["payload"]["__mutated__"] = True

    worker = _make_worker(
        session_factory,
        {"elasticsearch": capturing_handler},
        target_filter={"elasticsearch"},
    )
    await worker.run_once()

    # Verify key envelope fields were delivered.
    payload = received["payload"]
    assert isinstance(payload, dict)
    assert payload.get("kind") == "observation" or "world_memory_snapshot" in payload
    assert received["target"] == "elasticsearch"
    assert received["event_type"] == "world.observation.ingested"
    assert received["schema_version"] == 1
    assert received["outbox_id"] == row.id
    assert received["aggregate_id"] == row.aggregate_id
    assert received["idempotency_key"] == row.idempotency_key
    assert received["attempt_count"] == 1
    assert received["max_attempts"] == row.max_attempts

    # Verify stored envelope is unmutated.
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT envelope FROM world_memory_projection_outbox "
                "WHERE id = :id"
            ),
            {"id": str(row.id)},
        )
        stored_envelope = result.scalar_one()
    assert "__mutated__" not in stored_envelope


# ----------------------------------------------------------------------------
# Permanent failure → immediate dead-letter
# ----------------------------------------------------------------------------


async def test_permanent_failure_dead_letters_immediately(session_factory):
    """PermanentProjectionError causes immediate dead-letter."""
    row = await seed_authority_and_projection(
        session_factory, target="elasticsearch"
    )

    async def permanent_handler(event: ProjectionEvent) -> None:
        raise PermanentProjectionError("irrecoverable")

    worker = _make_worker(
        session_factory,
        {"elasticsearch": permanent_handler},
        target_filter={"elasticsearch"},
    )
    result = await worker.run_once()

    assert result.claimed == 1
    assert result.dead_lettered == 1
    assert result.completed == 0
    assert result.retried == 0

    state, attempt_count = await _row_state(session_factory, row.id)
    assert state == "dead_letter"
    assert attempt_count == 1  # dead-lettered on first attempt


# ----------------------------------------------------------------------------
# Retryable failure → pending (if attempts remain)
# ----------------------------------------------------------------------------


async def test_retryable_failure_returns_to_pending(session_factory):
    """A handler exception returns the row to pending for retry."""
    row = await seed_authority_and_projection(
        session_factory, target="elasticsearch"
    )

    async def flaky_handler(event: ProjectionEvent) -> None:
        raise RuntimeError("transient failure")

    worker = _make_worker(
        session_factory,
        {"elasticsearch": flaky_handler},
        target_filter={"elasticsearch"},
    )
    result = await worker.run_once()

    assert result.claimed == 1
    assert result.retried == 1
    assert result.completed == 0
    assert result.dead_lettered == 0

    state, attempt_count = await _row_state(session_factory, row.id)
    assert state == "pending"
    assert attempt_count == 1  # first attempt failed, retryable


# ----------------------------------------------------------------------------
# Final attempt → dead-letter (exhaustion)
# ----------------------------------------------------------------------------


async def test_final_attempt_dead_letters(session_factory):
    """Retryable failure on the last allowed attempt dead-letters."""
    row = await seed_authority_and_projection(
        session_factory, target="elasticsearch"
    )

    # Force attempt_count to max_attempts - 1 so the next attempt is
    # the final one.
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE world_memory_projection_outbox "
                "SET attempt_count = max_attempts - 1, state = 'pending', "
                "    lease_owner = NULL, lease_expires_at = NULL "
                "WHERE id = :id"
            ),
            {"id": str(row.id)},
        )
        await session.commit()

    async def flaky_handler(event: ProjectionEvent) -> None:
        raise RuntimeError("still failing")

    worker = _make_worker(
        session_factory,
        {"elasticsearch": flaky_handler},
        target_filter={"elasticsearch"},
    )
    result = await worker.run_once()

    assert result.claimed == 1
    assert result.dead_lettered == 1
    assert result.retried == 0

    state, attempt_count = await _row_state(session_factory, row.id)
    assert state == "dead_letter"
    assert attempt_count == row.max_attempts


# ----------------------------------------------------------------------------
# Lease-loss fencing
# ----------------------------------------------------------------------------


async def test_lease_loss_does_not_overwrite_newer_claimant(session_factory):
    """If the lease is lost before terminal write, worker records
    lease_lost without overwriting the new claimant's state."""
    row = await seed_authority_and_projection(
        session_factory, target="elasticsearch"
    )

    # Capture the fencing token and outbox_id by letting the handler
    # steal the lease from itself before returning.
    captured_token: dict[str, str] = {}

    async def handler_that_loses_lease(event: ProjectionEvent) -> None:
        # Steal the lease by re-claiming the row with a different worker.
        # The row is in 'processing' with our token — we need to expire
        # the lease and re-claim.
        async with session_factory() as session:
            await session.execute(
                text(
                    "UPDATE world_memory_projection_outbox "
                    "SET lease_expires_at = lease_expires_at - "
                    "    (lease_expires_at - lease_expires_at) - "
                    "    interval '1 hour', "
                    "    state = 'pending', "
                    "    lease_owner = NULL "
                    "WHERE id = :id AND state = 'processing'"
                ),
                {"id": str(event.outbox_id)},
            )
            await session.commit()

        # Another worker claims it.
        other_store = WorldMemoryProjectionStore(
            session_factory, "world-worker-other"
        )
        other_claimed, _ = await other_store.claim_batch(
            batch_size=1,
            lease_duration=timedelta(seconds=60),
            target_filter="elasticsearch",
        )
        assert other_claimed, "other worker should have claimed the row"
        captured_token["new_token"] = other_claimed[0].fencing_token

    worker = _make_worker(
        session_factory,
        {"elasticsearch": handler_that_loses_lease},
        target_filter={"elasticsearch"},
        worker_identity="world-worker-1",
    )
    result = await worker.run_once()

    # The original worker's mark_completed call should find the lease
    # is no longer its own and report lease_lost.
    assert result.claimed == 1
    assert result.lease_lost == 1
    assert result.completed == 0

    # The row must now be owned by the second worker (state processing),
    # not completed.
    state, _ = await _row_state(session_factory, row.id)
    assert state == "processing"


# ----------------------------------------------------------------------------
# Target filter required
# ----------------------------------------------------------------------------


async def test_missing_target_filter_rejected(session_factory):
    """The worker refuses to construct without a target_filter."""
    store = WorldMemoryProjectionStore(session_factory, "world-worker-1")

    async def ok_handler(event: ProjectionEvent) -> None:
        return None

    registry = ProjectionHandlerRegistry({"elasticsearch": ok_handler})

    with pytest.raises(ValueError, match="target_filter is required"):
        WorldMemoryProjectionWorker(
            store,
            registry,
            "world-worker-1",
            target_filter=set(),
        )


async def test_target_filter_must_match_registry(session_factory):
    """target_filter must equal registry.targets exactly."""
    store = WorldMemoryProjectionStore(session_factory, "world-worker-1")

    async def ok_handler(event: ProjectionEvent) -> None:
        return None

    registry = ProjectionHandlerRegistry({"elasticsearch": ok_handler})

    with pytest.raises(ValueError, match="target_filter must equal"):
        WorldMemoryProjectionWorker(
            store,
            registry,
            "world-worker-1",
            target_filter={"neo4j"},  # registered only 'elasticsearch'
        )


# ----------------------------------------------------------------------------
# Claim/terminal transactions survive session boundaries
# ----------------------------------------------------------------------------


async def test_claim_commits_before_handler_runs(session_factory):
    """The claim is durable before any handler executes — verified by
    checking that the row is in 'processing' inside the handler."""
    await seed_authority_and_projection(
        session_factory, target="elasticsearch"
    )

    observed_state: dict[str, str] = {}

    async def peeking_handler(event: ProjectionEvent) -> None:
        # Inside the handler, the claim must already be committed.
        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT state, lease_owner FROM "
                    "world_memory_projection_outbox WHERE id = :id"
                ),
                {"id": str(event.outbox_id)},
            )
            state_row = result.one()
            observed_state["state"] = state_row[0]
            observed_state["lease_owner"] = state_row[1]

    worker = _make_worker(
        session_factory,
        {"elasticsearch": peeking_handler},
        target_filter={"elasticsearch"},
    )
    await worker.run_once()

    assert observed_state["state"] == "processing"
    assert observed_state["lease_owner"] is not None


async def test_terminal_commit_survives_separate_session(session_factory):
    """After run_once completes, the terminal state is visible in a
    fresh session — proving claim and terminal commits used separate
    sessions/transactions."""
    row = await seed_authority_and_projection(
        session_factory, target="elasticsearch"
    )

    async def ok_handler(event: ProjectionEvent) -> None:
        return None

    worker = _make_worker(
        session_factory,
        {"elasticsearch": ok_handler},
        target_filter={"elasticsearch"},
    )
    await worker.run_once()

    # Fresh session: state must be committed and visible.
    state, _ = await _row_state(session_factory, row.id)
    assert state == "completed"
