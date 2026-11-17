"""Live P9-D4 aggregate-scoped valid-time state snapshot (PostgreSQL authority).

Runs only when ``ESRAGPRO_MIGRATION_TEST_MODE=1`` and ``DATABASE_URL`` point
at the isolated ``esragpro_test`` database (see ``conftest.py``). The
``_isolate_tables`` autouse fixture truncates ``world_memory_envelopes``
before and after each test, so the database is left clean.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.memory_core.repositories.world_memory_authority import (
    WorldMemoryAuthorityRepository,
)
from app.memory_core.world_memory import ValidityInterval, WorldState
from app.memory_core.world_memory_serializer import (
    EVENT_STATE_FUSED,
    build_world_memory_envelope,
)

from .conftest import make_observation_envelope


def make_state_envelope(
    *,
    aggregate_id: UUID,
    content: str,
    validity_start: datetime,
    validity_end: datetime | None = None,
    observation_ids: tuple[UUID, ...] = (),
    supersedes: tuple[UUID, ...] = (),
) -> dict[str, Any]:
    """Build a valid P7-B state envelope scoped to *aggregate_id*."""
    state = WorldState(
        id=uuid4(),
        content=content,
        observation_ids=observation_ids,
        validity=ValidityInterval(start=validity_start, end=validity_end),
        supersedes=supersedes,
    )
    envelope = build_world_memory_envelope(
        state,
        event_type=EVENT_STATE_FUSED,
        projection_target="test",
    )
    envelope["aggregate_id"] = str(aggregate_id)
    return envelope


@pytest.mark.asyncio
async def test_authority_resolve_state_at_returns_exact_stored_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aggregate_id = uuid4()
    envelope = make_state_envelope(
        aggregate_id=aggregate_id,
        content="Bridge B is impassable",
        validity_start=datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc),
        validity_end=datetime(2026, 8, 2, 5, 0, tzinfo=timezone.utc),
        observation_ids=(uuid4(),),
    )

    async with session_factory() as session:
        repository = WorldMemoryAuthorityRepository(session)
        stored = await repository.remember(envelope)
        await session.commit()

        resolved = await repository.resolve_state_at(
            aggregate_id=aggregate_id,
            valid_at=datetime(2026, 8, 2, 4, 30, tzinfo=timezone.utc),
        )

    assert len(resolved) == 1
    assert resolved[0].id == stored.id
    assert resolved[0].envelope == envelope


@pytest.mark.asyncio
async def test_authority_resolve_state_at_removes_superseded_overlap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aggregate_id = uuid4()
    superseded_id = uuid4()
    superseding_envelope = make_state_envelope(
        aggregate_id=aggregate_id,
        content="new state",
        validity_start=datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc),
        validity_end=datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc),
        observation_ids=(uuid4(),),
        supersedes=(superseded_id,),
    )
    superseded_envelope = make_state_envelope(
        aggregate_id=aggregate_id,
        content="old state",
        validity_start=datetime(2026, 8, 2, 3, 0, tzinfo=timezone.utc),
        validity_end=datetime(2026, 8, 2, 5, 0, tzinfo=timezone.utc),
        observation_ids=(uuid4(),),
    )
    # Make the superseding state reference the superseded state's snapshot id.
    superseded_snapshot_id = superseded_envelope["world_memory_snapshot"]["id"]
    superseding_envelope["world_memory_snapshot"]["supersedes"] = [
        superseded_snapshot_id
    ]

    async with session_factory() as session:
        repository = WorldMemoryAuthorityRepository(session)
        await repository.remember(superseded_envelope)
        await repository.remember(superseding_envelope)
        await session.commit()

        resolved = await repository.resolve_state_at(
            aggregate_id=aggregate_id,
            valid_at=datetime(2026, 8, 2, 4, 30, tzinfo=timezone.utc),
        )

    assert len(resolved) == 1
    assert resolved[0].envelope["world_memory_snapshot"]["content"] == "new state"


@pytest.mark.asyncio
async def test_authority_resolve_state_at_keeps_conflicting_non_superseded_states(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aggregate_id = uuid4()
    conflict_a = make_state_envelope(
        aggregate_id=aggregate_id,
        content="conflict A",
        validity_start=datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc),
        validity_end=datetime(2026, 8, 2, 5, 0, tzinfo=timezone.utc),
        observation_ids=(uuid4(),),
    )
    conflict_b = make_state_envelope(
        aggregate_id=aggregate_id,
        content="conflict B",
        validity_start=datetime(2026, 8, 2, 4, 30, tzinfo=timezone.utc),
        validity_end=datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc),
        observation_ids=(uuid4(),),
    )

    async with session_factory() as session:
        repository = WorldMemoryAuthorityRepository(session)
        await repository.remember(conflict_a)
        await repository.remember(conflict_b)
        await session.commit()

        resolved = await repository.resolve_state_at(
            aggregate_id=aggregate_id,
            valid_at=datetime(2026, 8, 2, 4, 30, tzinfo=timezone.utc),
        )

    assert len(resolved) == 2


@pytest.mark.asyncio
async def test_authority_resolve_state_at_ignores_other_aggregates_and_kinds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aggregate_id = uuid4()
    other_aggregate_id = uuid4()
    state_envelope = make_state_envelope(
        aggregate_id=aggregate_id,
        content="target state",
        validity_start=datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc),
        validity_end=datetime(2026, 8, 2, 5, 0, tzinfo=timezone.utc),
        observation_ids=(uuid4(),),
    )
    other_aggregate_state = make_state_envelope(
        aggregate_id=other_aggregate_id,
        content="other aggregate state",
        validity_start=datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc),
        validity_end=datetime(2026, 8, 2, 5, 0, tzinfo=timezone.utc),
        observation_ids=(uuid4(),),
    )
    observation_envelope = make_observation_envelope(content="not a state")

    async with session_factory() as session:
        repository = WorldMemoryAuthorityRepository(session)
        await repository.remember(state_envelope)
        await repository.remember(other_aggregate_state)
        await repository.remember(observation_envelope)
        await session.commit()

        resolved = await repository.resolve_state_at(
            aggregate_id=aggregate_id,
            valid_at=datetime(2026, 8, 2, 4, 30, tzinfo=timezone.utc),
        )

    assert len(resolved) == 1
    assert resolved[0].envelope["world_memory_snapshot"]["content"] == "target state"