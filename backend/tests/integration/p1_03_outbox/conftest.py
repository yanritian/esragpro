"""Shared fixtures for P1-03 transactional outbox tests.

Mirrors the P1-02 fixture pattern but truncates the new outbox table
alongside the memory plane tables so tests stay isolated.
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
    if os.environ.get("ESRAGPRO_MIGRATION_TEST_MODE") != "1":
        return None
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        return None
    return validate_migration_test_database_url(raw)


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_evidence(
    source_id: str = "doc-1",
    source_type: str = "document",
    confidence: float = 0.9,
    chunk_id: Optional[str] = None,
) -> EvidenceRef:
    return EvidenceRef(
        source_id=source_id,
        source_type=source_type,
        timestamp=_now_naive(),
        chunk_id=chunk_id,
        confidence=confidence,
    )


@pytest.fixture
async def session_factory():
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL not set (run via run-p1-03-outbox-test.ps1)")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def evidence_factory():
    return make_evidence


@pytest.fixture(autouse=True)
async def _isolate_tables(session_factory):
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
