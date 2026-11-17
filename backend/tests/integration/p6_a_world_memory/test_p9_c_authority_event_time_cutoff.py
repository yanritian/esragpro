from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.memory_core.repositories.world_memory_authority import (
    WorldMemoryAuthorityRepository,
)

from .conftest import make_observation_envelope


@pytest.mark.asyncio
async def test_authority_query_event_time_lte_includes_boundary_and_excludes_later_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cutoff = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    older_envelope = make_observation_envelope(content="historic authority row")
    older_envelope["occurred_at"] = "2026-07-31T12:00:00+00:00"
    later_envelope = make_observation_envelope(content="later authority row")
    later_envelope["occurred_at"] = "2026-07-31T12:05:00+00:00"

    async with session_factory() as session:
        repository = WorldMemoryAuthorityRepository(session)
        _ = await repository.remember(older_envelope)
        _ = await repository.remember(later_envelope)
        await session.commit()

        results = await repository.query(event_time_lte=cutoff)

    assert [row.envelope["world_memory_snapshot"]["content"] for row in results] == [
        "historic authority row"
    ]
