"""Shared fixtures for P1-04 worker integration tests.

Mirrors the P1-02/P1-03 fixture pattern.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from app.db.migration_config import validate_migration_test_database_url
from app.memory_core.unpermissioned_contracts import EvidenceRef


def _database_url() -> Optional[str]:
    """Return the validated URL only when migration-test mode is active."""
    if os.environ.get("ESRAGPRO_MIGRATION_TEST_MODE") != "1":
        return None
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        return None
    return validate_migration_test_database_url(raw)


def _now_naive() -> datetime:
    """Naive UTC ``now`` without using the deprecated ``utcnow``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_evidence(
    source_id: str = "doc-1",
    source_type: str = "document",
    confidence: float = 0.9,
    chunk_id: Optional[str] = None,
) -> EvidenceRef:
    """Build an ``EvidenceRef`` with a naive-UTC timestamp."""
    return EvidenceRef(
        source_id=source_id,
        source_type=source_type,
        timestamp=_now_naive(),
        chunk_id=chunk_id,
        confidence=confidence,
    )


@pytest.fixture
async def session_factory():
    """Yield an ``async_sessionmaker`` for the isolated test database."""
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL not set (run via run-p1-04-worker-test.ps1)")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
async def db_engine():
    """Yield the async engine for the isolated test database.

    Use this fixture when you need to create session factories with
    custom session classes (e.g., for testing commit failures).
    """
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL not set (run via run-p1-04-worker-test.ps1)")
    engine = create_async_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def evidence_factory():
    """Return the shared evidence builder for test modules."""
    return make_evidence


@pytest.fixture(autouse=True)
async def _isolate_tables(session_factory):
    """Truncate memory tables before and after each test for isolation."""
    async with session_factory() as session:
        await session.execute(text(
            "TRUNCATE memory_projection_outbox, evidence_refs, memory_items"
        ))
        await session.commit()
    yield
    async with session_factory() as session:
        await session.execute(text(
            "TRUNCATE memory_projection_outbox, evidence_refs, memory_items"
        ))
        await session.commit()