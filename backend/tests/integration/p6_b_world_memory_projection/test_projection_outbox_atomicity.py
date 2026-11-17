"""P6-B: Transaction atomicity tests.

Verifies that authority and projection rows commit and roll back together.
"""
from __future__ import annotations

from sqlalchemy import text

from app.db.models.world_memory import WorldMemoryEnvelope
from app.memory_core.repositories.world_memory_authority import (
    WorldMemoryAuthorityRepository,
)
from app.memory_core.repositories.world_memory_projection_outbox import (
    DEFAULT_WORLD_PROJECTION_TARGETS,
    WorldMemoryProjectionOutboxRepository,
)

from .conftest import make_observation_envelope


async def test_authority_and_projection_commit_together(session_factory):
    """Both authority and projection rows appear after commit."""
    envelope_dict = make_observation_envelope()

    async with session_factory() as session:
        authority_repo = WorldMemoryAuthorityRepository(session)
        stored = await authority_repo.remember(envelope_dict)

        # Fetch ORM row for projection enqueue
        from sqlalchemy import select
        result = await session.execute(
            select(WorldMemoryEnvelope).where(
                WorldMemoryEnvelope.idempotency_key == stored.idempotency_key
            )
        )
        orm_row = result.scalar_one()

        projection_repo = WorldMemoryProjectionOutboxRepository(session)
        await projection_repo.enqueue_for_targets(
            orm_row, targets=DEFAULT_WORLD_PROJECTION_TARGETS
        )
        await session.commit()

    # Verify both tables have rows
    async with session_factory() as session:
        auth_count = await session.execute(
            text("SELECT count(*) FROM world_memory_envelopes")
        )
        proj_count = await session.execute(
            text("SELECT count(*) FROM world_memory_projection_outbox")
        )
        assert int(auth_count.scalar_one()) == 1
        assert int(proj_count.scalar_one()) == len(DEFAULT_WORLD_PROJECTION_TARGETS)


async def test_rollback_leaves_neither_row(session_factory):
    """Rollback after authority+projection leaves zero rows in both tables."""
    envelope_dict = make_observation_envelope()

    async with session_factory() as session:
        authority_repo = WorldMemoryAuthorityRepository(session)
        stored = await authority_repo.remember(envelope_dict)

        from sqlalchemy import select
        result = await session.execute(
            select(WorldMemoryEnvelope).where(
                WorldMemoryEnvelope.idempotency_key == stored.idempotency_key
            )
        )
        orm_row = result.scalar_one()

        projection_repo = WorldMemoryProjectionOutboxRepository(session)
        await projection_repo.enqueue_for_targets(
            orm_row, targets=DEFAULT_WORLD_PROJECTION_TARGETS
        )

        # Rollback instead of commit
        await session.rollback()

    # Verify both tables are empty
    async with session_factory() as session:
        auth_count = await session.execute(
            text("SELECT count(*) FROM world_memory_envelopes")
        )
        proj_count = await session.execute(
            text("SELECT count(*) FROM world_memory_projection_outbox")
        )
        assert int(auth_count.scalar_one()) == 0
        assert int(proj_count.scalar_one()) == 0


async def test_projection_rows_link_to_authority(session_factory):
    """Projection rows have correct FK to authority envelope."""
    envelope_dict = make_observation_envelope()

    async with session_factory() as session:
        authority_repo = WorldMemoryAuthorityRepository(session)
        stored = await authority_repo.remember(envelope_dict)

        from sqlalchemy import select
        result = await session.execute(
            select(WorldMemoryEnvelope).where(
                WorldMemoryEnvelope.idempotency_key == stored.idempotency_key
            )
        )
        orm_row = result.scalar_one()

        projection_repo = WorldMemoryProjectionOutboxRepository(session)
        proj_rows = await projection_repo.enqueue_for_targets(
            orm_row, targets=DEFAULT_WORLD_PROJECTION_TARGETS
        )
        await session.commit()

        # Verify FK linkage
        for proj in proj_rows:
            assert proj.authority_envelope_id == stored.id


async def test_projection_rows_preserve_full_envelope(session_factory):
    """Each projection row carries the complete envelope payload."""
    envelope_dict = make_observation_envelope(content="full envelope test")

    async with session_factory() as session:
        authority_repo = WorldMemoryAuthorityRepository(session)
        stored = await authority_repo.remember(envelope_dict)

        from sqlalchemy import select
        result = await session.execute(
            select(WorldMemoryEnvelope).where(
                WorldMemoryEnvelope.idempotency_key == stored.idempotency_key
            )
        )
        orm_row = result.scalar_one()

        projection_repo = WorldMemoryProjectionOutboxRepository(session)
        proj_rows = await projection_repo.enqueue_for_targets(
            orm_row, targets=["elasticsearch", "neo4j"]
        )
        await session.commit()

    # Verify each projection row has the full envelope
    for proj in proj_rows:
        assert proj.envelope == stored.envelope
        assert proj.event_type == stored.event_type
        assert proj.schema_version == stored.schema_version
        assert proj.aggregate_id == stored.aggregate_id
