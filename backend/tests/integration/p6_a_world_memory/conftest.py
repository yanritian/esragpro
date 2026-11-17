"""Shared fixtures for P6-A world-memory authority integration tests.

Follows the P1-03 fixture pattern: truncates the authority table
before and after each test so tests stay isolated.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from app.db.migration_config import validate_migration_test_database_url
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


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
        await session.execute(text("TRUNCATE world_memory_envelopes CASCADE"))
        await session.commit()
    yield
    async with session_factory() as session:
        await session.execute(text("TRUNCATE world_memory_envelopes CASCADE"))
        await session.commit()


def make_observation_envelope(
    *,
    content: str = "test observation",
    evidence_ref: str | None = "doc-1",
    projection_target: str = "test-target",
) -> dict[str, Any]:
    """Build a valid P7-B observation envelope for tests."""
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
