"""Shared fixtures for P6-C world-memory projection worker tests."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from app.db.migration_config import validate_migration_test_database_url
from app.db.models.world_memory import WorldMemoryEnvelope
from app.memory_core.repositories.world_memory_authority import (
    WorldMemoryAuthorityRepository,
)
from app.memory_core.repositories.world_memory_projection_outbox import (
    WorldMemoryProjectionOutboxRepository,
)
from app.memory_core.world_memory import WorldObservation
from app.memory_core.world_memory_serializer import (
    EVENT_OBSERVATION_INGESTED,
    build_world_memory_envelope,
)


def _database_url() -> str | None:
    if os.environ.get("ESRAGPRO_MIGRATION_TEST_MODE") != "1":
        return None
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        return None
    return validate_migration_test_database_url(raw)


@pytest.fixture
async def session_factory():
    url = _database_url()
    if not url:
        pytest.skip(
            "DATABASE_URL not set (run with ESRAGPRO_MIGRATION_TEST_MODE=1 "
            "and DATABASE_URL=postgresql+asyncpg://...)"
        )
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
async def _isolate_tables(session_factory):
    async with session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE world_memory_projection_outbox, "
                "world_memory_envelopes"
            )
        )
        await session.commit()
    yield
    async with session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE world_memory_projection_outbox, "
                "world_memory_envelopes"
            )
        )
        await session.commit()


def make_observation_envelope(
    *,
    content: str = "test observation",
    evidence_ref: str | None = "doc-1",
    projection_target: str = "elasticsearch",
) -> dict[str, Any]:
    obs_id = uuid4()
    observed_at = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
    ingested_at = datetime(2026, 7, 31, 12, 0, 1, tzinfo=timezone.utc)
    obs = WorldObservation(
        id=obs_id,
        content=content,
        observed_at=observed_at,
        source="test-sensor",
        ingested_at=ingested_at,
        version=1,
        raw_evidence_ref=evidence_ref,
    )
    return build_world_memory_envelope(
        obs,
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target=projection_target,
    )


async def seed_authority_and_projection(
    session_factory,
    *,
    target: str = "elasticsearch",
    content: str = "test observation",
) -> Any:
    """Seed one authority + one projection row and commit.

    Returns the StoredProjectionRow for the projection row.
    """
    envelope_dict = make_observation_envelope(
        content=content, projection_target=target
    )
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


__all__ = [
    "make_observation_envelope",
    "seed_authority_and_projection",
]
