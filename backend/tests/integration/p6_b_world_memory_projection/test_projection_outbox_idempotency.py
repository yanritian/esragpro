"""P6-B: Idempotency and conflict tests.

Verifies exact-duplicate dedup and conflicting-key rejection.
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.models.world_memory import WorldMemoryEnvelope
from app.memory_core.repositories.world_memory_authority import (
    WorldMemoryAuthorityRepository,
)
from app.memory_core.repositories.world_memory_projection_outbox import (
    WorldMemoryProjectionOutboxRepository,
)

from .conftest import make_observation_envelope


async def test_exact_duplicate_enqueue_is_idempotent(session_factory):
    """Enqueueing the same (authority, target) twice returns the existing row."""
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

        # First enqueue
        rows_1 = await projection_repo.enqueue_for_targets(
            orm_row, targets=["elasticsearch"]
        )
        assert len(rows_1) == 1

        # Second enqueue with same authority + target → idempotent
        rows_2 = await projection_repo.enqueue_for_targets(
            orm_row, targets=["elasticsearch"]
        )
        assert len(rows_2) == 1
        assert rows_2[0].id == rows_1[0].id
        assert rows_2[0].idempotency_key == rows_1[0].idempotency_key

        await session.commit()

    # Verify only one projection row exists
    async with session_factory() as session:
        from sqlalchemy import text
        count_result = await session.execute(
            text("SELECT count(*) FROM world_memory_projection_outbox")
        )
        assert int(count_result.scalar_one()) == 1


async def test_different_targets_create_different_rows(session_factory):
    """Two explicit targets create two target-specific rows."""
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
            orm_row, targets=["elasticsearch", "neo4j"]
        )
        await session.commit()

        assert len(rows) == 2
        assert rows[0].projection_target != rows[1].projection_target
        assert rows[0].idempotency_key != rows[1].idempotency_key
        # Both carry the full envelope
        assert rows[0].envelope == rows[1].envelope == stored.envelope


async def test_deterministic_idempotency_keys(session_factory):
    """Idempotency keys are deterministic for the same (authority, target)."""
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

        rows_1 = await projection_repo.enqueue_for_targets(
            orm_row, targets=["elasticsearch"]
        )
        # Don't commit — enqueue again in same session
        rows_2 = await projection_repo.enqueue_for_targets(
            orm_row, targets=["elasticsearch"]
        )

        # Keys must match exactly
        assert rows_1[0].idempotency_key == rows_2[0].idempotency_key
        # Pattern check
        expected_prefix = f"world-projection:{stored.id}:v{stored.schema_version}:"
        assert rows_1[0].idempotency_key.startswith(expected_prefix)


async def test_envelope_payload_replayable(session_factory):
    """Stored envelope in projection row is replay-safe (full payload)."""
    envelope_dict = make_observation_envelope(content="replay test")

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
            orm_row, targets=["elasticsearch"]
        )
        await session.commit()

    # Verify full envelope is stored and replayable
    proj = rows[0]
    assert "world_memory_snapshot" in proj.envelope
    assert proj.envelope["event_type"] == stored.event_type
    assert proj.envelope["schema_version"] == stored.schema_version
    assert proj.envelope["aggregate_type"] == "world_memory"
