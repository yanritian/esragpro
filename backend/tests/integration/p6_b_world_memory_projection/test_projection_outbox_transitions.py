"""P6-B: Claim, lease, retry, and dead-letter state transition tests."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, text

from app.db.models.world_memory import WorldMemoryEnvelope
from app.memory_core.repositories.world_memory_authority import (
    WorldMemoryAuthorityRepository,
)
from app.memory_core.repositories.world_memory_projection_outbox import (
    WorldMemoryProjectionOutboxRepository,
)

from .conftest import make_observation_envelope


async def _seed_one(session_factory, target: str = "elasticsearch"):
    """Seed one authority + one projection row and commit."""
    envelope_dict = make_observation_envelope()
    async with session_factory() as session:
        authority_repo = WorldMemoryAuthorityRepository(session)
        stored = await authority_repo.remember(envelope_dict)
        result = await session.execute(
            select(WorldMemoryEnvelope).where(
                WorldMemoryEnvelope.idempotency_key == stored.idempotency_key
            )
        )
        orm_row = result.scalar_one()
        projection_repo = WorldMemoryProjectionOutboxRepository(session)
        rows = await projection_repo.enqueue_for_targets(
            orm_row, targets=[target]
        )
        await session.commit()
        return rows[0]


async def test_claim_transitions_to_processing(session_factory):
    """Claiming a pending row transitions it to processing."""
    await _seed_one(session_factory)

    async with session_factory() as session:
        repo = WorldMemoryProjectionOutboxRepository(session)
        claimed = await repo.claim_batch(
            worker_identity="worker-1",
            batch_size=1,
            lease_duration=timedelta(seconds=30),
        )
        await session.commit()

    assert len(claimed) == 1
    assert claimed[0].state == "processing"
    assert claimed[0].attempt_count == 1
    assert claimed[0].lease_owner is not None
    assert claimed[0].lease_expires_at is not None


async def test_completed_transition(session_factory):
    """Completing a claimed row transitions to completed."""
    row = await _seed_one(session_factory)

    async with session_factory() as session:
        repo = WorldMemoryProjectionOutboxRepository(session)
        claimed = await repo.claim_batch(
            worker_identity="worker-1",
            batch_size=1,
            lease_duration=timedelta(seconds=30),
        )
        assert len(claimed) == 1
        assert claimed[0].lease_owner is not None
        fencing_token: str = claimed[0].lease_owner

        success = await repo.mark_completed(
            claimed[0].id, fencing_token=fencing_token
        )
        await session.commit()

    assert success is True

    # Verify state in DB
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT state, processed_at FROM world_memory_projection_outbox "
                "WHERE id = :id"
            ),
            {"id": str(row.id)},
        )
        state_row = result.one()
        assert state_row[0] == "completed"
        assert state_row[1] is not None


async def test_retryable_failure_returns_to_pending(session_factory):
    """A retryable failure transitions the row back to pending."""
    await _seed_one(session_factory)

    async with session_factory() as session:
        repo = WorldMemoryProjectionOutboxRepository(session)
        claimed = await repo.claim_batch(
            worker_identity="worker-1",
            batch_size=1,
            lease_duration=timedelta(seconds=30),
        )
        assert len(claimed) == 1
        assert claimed[0].lease_owner is not None
        fencing_token: str = claimed[0].lease_owner

        disposition = await repo.mark_failed(
            claimed[0].id,
            fencing_token=fencing_token,
            error="transient error",
            permanent=False,
            retry_delay=timedelta(seconds=5),
        )
        await session.commit()

    assert disposition == "pending"  # retried

    # Verify state
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT state, last_error, attempt_count "
                "FROM world_memory_projection_outbox WHERE id = :id"
            ),
            {"id": str(claimed[0].id)},
        )
        state_row = result.one()
        assert state_row[0] == "pending"
        assert "transient error" in state_row[1]
        assert state_row[2] == 1


async def test_permanent_failure_goes_to_dead_letter(session_factory):
    """A permanent failure transitions to dead_letter."""
    await _seed_one(session_factory)

    async with session_factory() as session:
        repo = WorldMemoryProjectionOutboxRepository(session)
        claimed = await repo.claim_batch(
            worker_identity="worker-1",
            batch_size=1,
            lease_duration=timedelta(seconds=30),
        )
        assert len(claimed) == 1
        assert claimed[0].lease_owner is not None
        fencing_token: str = claimed[0].lease_owner

        disposition = await repo.mark_failed(
            claimed[0].id,
            fencing_token=fencing_token,
            error="permanent error",
            permanent=True,
        )
        await session.commit()

    assert disposition == "dead_letter"

    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT state FROM world_memory_projection_outbox WHERE id = :id"
            ),
            {"id": str(claimed[0].id)},
        )
        assert result.scalar_one() == "dead_letter"


async def test_dead_letter_requeue(session_factory):
    """Requeuing a dead-letter row resets it to pending with zero attempts."""
    await _seed_one(session_factory)

    # Move to dead_letter
    async with session_factory() as session:
        repo = WorldMemoryProjectionOutboxRepository(session)
        claimed = await repo.claim_batch(
            worker_identity="worker-1",
            batch_size=1,
            lease_duration=timedelta(seconds=30),
        )
        assert len(claimed) == 1
        assert claimed[0].lease_owner is not None
        fencing_token: str = claimed[0].lease_owner
        await repo.mark_failed(
            claimed[0].id,
            fencing_token=fencing_token,
            error="permanent",
            permanent=True,
        )
        await session.commit()
        row_id = claimed[0].id

    # Requeue
    async with session_factory() as session:
        repo = WorldMemoryProjectionOutboxRepository(session)
        success = await repo.requeue_dead_letter(row_id)
        await session.commit()

    assert success is True

    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT state, attempt_count, last_error "
                "FROM world_memory_projection_outbox WHERE id = :id"
            ),
            {"id": str(row_id)},
        )
        state_row = result.one()
        assert state_row[0] == "pending"
        assert state_row[1] == 0
        assert state_row[2] is None


async def test_lease_recovery_resets_to_pending(session_factory):
    """Expired-lease processing rows are recovered back to pending on next claim."""
    await _seed_one(session_factory)

    # Claim with a very short lease
    async with session_factory() as session:
        repo = WorldMemoryProjectionOutboxRepository(session)
        claimed = await repo.claim_batch(
            worker_identity="worker-1",
            batch_size=1,
            lease_duration=timedelta(seconds=30),
        )
        assert len(claimed) == 1
        assert claimed[0].lease_owner is not None
        await session.commit()

    # Manually expire the lease
    async with session_factory() as session:
        from datetime import datetime, timezone
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        await session.execute(
            text(
                "UPDATE world_memory_projection_outbox "
                "SET lease_expires_at = :past WHERE id = :id"
            ),
            {"past": past, "id": str(claimed[0].id)},
        )
        await session.commit()

    # Next claim should recover the expired lease
    async with session_factory() as session:
        repo = WorldMemoryProjectionOutboxRepository(session)
        recovered = await repo.claim_batch(
            worker_identity="worker-2",
            batch_size=1,
            lease_duration=timedelta(seconds=30),
        )
        await session.commit()

    assert len(recovered) == 1
    assert recovered[0].lease_owner is not None
    assert recovered[0].attempt_count == 2  # incremented from 1


async def test_completed_mark_with_wrong_token_fails(session_factory):
    """mark_completed with wrong fencing token returns False (lease lost)."""
    await _seed_one(session_factory)

    async with session_factory() as session:
        repo = WorldMemoryProjectionOutboxRepository(session)
        claimed = await repo.claim_batch(
            worker_identity="worker-1",
            batch_size=1,
            lease_duration=timedelta(seconds=30),
        )
        assert len(claimed) == 1
        assert claimed[0].lease_owner is not None

        # Try to complete with wrong token
        success = await repo.mark_completed(
            claimed[0].id, fencing_token="wrong-token"
        )
        await session.commit()

    assert success is False

    # Row should still be in processing state
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT state FROM world_memory_projection_outbox WHERE id = :id"
            ),
            {"id": str(claimed[0].id)},
        )
        assert result.scalar_one() == "processing"


# ----------------------------------------------------------------------
# Exhaustion edge cases (P6-B lifecycle fix)
# ----------------------------------------------------------------------


async def test_exhausted_lease_expired_goes_to_dead_letter(session_factory):
    """An exhausted processing row with expired lease is dead-lettered during recovery."""
    row = await _seed_one(session_factory)

    # Set max_attempts=1 so the first claim exhausts the row.
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE world_memory_projection_outbox "
                "SET max_attempts = 1 WHERE id = :id"
            ),
            {"id": str(row.id)},
        )
        await session.commit()

    # First claim: transitions to processing with attempt_count=1 (exhausted).
    async with session_factory() as session:
        repo = WorldMemoryProjectionOutboxRepository(session)
        claimed = await repo.claim_batch(
            worker_identity="worker-1",
            batch_size=1,
            lease_duration=timedelta(seconds=30),
        )
        await session.commit()

    assert len(claimed) == 1
    assert claimed[0].state == "processing"
    assert claimed[0].attempt_count == 1

    # Expire that processing lease.
    async with session_factory() as session:
        from datetime import datetime, timezone

        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        await session.execute(
            text(
                "UPDATE world_memory_projection_outbox "
                "SET lease_expires_at = :past WHERE id = :id"
            ),
            {"past": past, "id": str(row.id)},
        )
        await session.commit()

    # Next claim_batch should recover the expired lease and dead-letter the exhausted row.
    async with session_factory() as session:
        repo = WorldMemoryProjectionOutboxRepository(session)
        claimed_again = await repo.claim_batch(
            worker_identity="worker-2",
            batch_size=1,
            lease_duration=timedelta(seconds=30),
        )
        await session.commit()

    assert len(claimed_again) == 0

    # Verify it went to dead_letter
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT state FROM world_memory_projection_outbox WHERE id = :id"
            ),
            {"id": str(row.id)},
        )
        assert result.scalar_one() == "dead_letter"


async def test_retryable_failure_on_final_attempt_goes_to_dead_letter(
    session_factory,
):
    """A retryable failure on the final allowed attempt transitions to dead_letter."""
    row = await _seed_one(session_factory)

    # Set up a row at attempt_count = max_attempts - 1 (one attempt left)
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE world_memory_projection_outbox "
                "SET max_attempts = 2, attempt_count = 1 "
                "WHERE id = :id"
            ),
            {"id": str(row.id)},
        )
        await session.commit()

    # Claim it (attempt_count becomes 2 = max_attempts)
    async with session_factory() as session:
        repo = WorldMemoryProjectionOutboxRepository(session)
        claimed = await repo.claim_batch(
            worker_identity="worker-1",
            batch_size=1,
            lease_duration=timedelta(seconds=30),
        )
        assert len(claimed) == 1
        assert claimed[0].attempt_count == 2
        assert claimed[0].lease_owner is not None
        fencing_token: str = claimed[0].lease_owner

        # Retryable failure on final attempt -> should dead-letter
        disposition = await repo.mark_failed(
            claimed[0].id,
            fencing_token=fencing_token,
            error="transient on final attempt",
            permanent=False,
        )
        await session.commit()

    assert disposition == "dead_letter"

    # Verify state
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT state, last_error FROM world_memory_projection_outbox "
                "WHERE id = :id"
            ),
            {"id": str(row.id)},
        )
        state_row = result.one()
        assert state_row[0] == "dead_letter"
        assert "transient on final attempt" in state_row[1]


async def test_exhausted_row_not_claimed(session_factory):
    """A pending row with attempt_count >= max_attempts is dead-lettered during recovery."""
    row = await _seed_one(session_factory)

    # Set to exhausted but still pending
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE world_memory_projection_outbox "
                "SET max_attempts = 2, attempt_count = 2, state = 'pending' "
                "WHERE id = :id"
            ),
            {"id": str(row.id)},
        )
        await session.commit()

    # Attempt to claim - should dead-letter the exhausted pending row
    async with session_factory() as session:
        repo = WorldMemoryProjectionOutboxRepository(session)
        claimed = await repo.claim_batch(
            worker_identity="worker-1",
            batch_size=1,
            lease_duration=timedelta(seconds=30),
        )
        await session.commit()

    assert len(claimed) == 0

    # Verify it was moved to dead_letter
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT state FROM world_memory_projection_outbox WHERE id = :id"
            ),
            {"id": str(row.id)},
        )
        assert result.scalar_one() == "dead_letter"
