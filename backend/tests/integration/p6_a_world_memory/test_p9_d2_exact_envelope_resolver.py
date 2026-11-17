from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.memory_core.repositories.world_memory_authority import (
    WorldMemoryAuthorityRepository,
)

from .conftest import make_observation_envelope


@pytest.mark.asyncio
async def test_exact_envelope_resolver_preserves_requested_historical_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    object_id = uuid4()
    aggregate_id = uuid4()
    historic = make_observation_envelope(content="historic reading")
    latest = make_observation_envelope(content="current reading")
    for envelope, occurred_at in (
        (historic, "2026-08-02T12:00:00+00:00"),
        (latest, "2026-08-02T12:05:00+00:00"),
    ):
        envelope["aggregate_id"] = str(aggregate_id)
        envelope["occurred_at"] = occurred_at
        envelope["world_memory_snapshot"]["id"] = str(object_id)

    async with session_factory() as session:
        repository = WorldMemoryAuthorityRepository(session)
        historic_stored = await repository.remember(historic)
        latest_stored = await repository.remember(latest)
        await session.commit()

        resolved = await repository.resolve_exact_envelopes(
            (latest_stored.id, uuid4(), historic_stored.id, latest_stored.id)
        )

    assert [row.id for row in resolved] == [latest_stored.id, historic_stored.id]
    assert [row.envelope["world_memory_snapshot"]["content"] for row in resolved] == [
        "current reading",
        "historic reading",
    ]
