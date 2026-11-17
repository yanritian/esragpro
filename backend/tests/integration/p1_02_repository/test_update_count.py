"""P1-02 integration tests: update_status and count operations."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from app.memory_core.repositories.postgres_repository import (
    PostgresMemoryRepository,
)
from app.memory_core.unpermissioned_contracts import MemoryItemStatus


@pytest.mark.asyncio
class TestUpdateStatus:
    """update_status() uses database timestamps and preserves evidence."""

    async def test_update_status_success(
        self, session_factory, evidence_factory
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            item = await repo.remember("update me", [evidence_factory()])
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            updated = await repo.update_status(item.id, MemoryItemStatus.ARCHIVED)
            assert updated is not None
            assert updated.status == MemoryItemStatus.ARCHIVED
            await session2.commit()
        async with session_factory() as session3:
            repo = PostgresMemoryRepository(session3)
            fetched = await repo.get_by_id(item.id)
            assert fetched is not None
            assert fetched.status == MemoryItemStatus.ARCHIVED

    async def test_update_status_missing_row(self, session_factory):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            result = await repo.update_status(uuid4(), MemoryItemStatus.DELETED)
            assert result is None

    async def test_update_status_timestamp_advancement(
        self, session_factory, evidence_factory
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            item = await repo.remember("ts check", [evidence_factory()])
            await session.commit()
            original_updated = item.updated_at
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            updated = await repo.update_status(item.id, MemoryItemStatus.ARCHIVED)
            assert updated is not None
            assert updated.updated_at >= original_updated
            await session2.commit()

    async def test_update_status_preserves_evidence(
        self, session_factory, evidence_factory
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            item = await repo.remember(
                "evidence preserved",
                [evidence_factory(source_id="e1"), evidence_factory(source_id="e2")],
            )
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            updated = await repo.update_status(item.id, MemoryItemStatus.EXPIRED)
            assert updated is not None
            assert len(updated.evidence_refs) == 2
            assert {e.source_id for e in updated.evidence_refs} == {"e1", "e2"}
            await session2.commit()


@pytest.mark.asyncio
class TestCount:
    """count() mirrors query filtering without returning rows."""

    async def test_count_parity_with_filters(
        self, session_factory, evidence_factory
    ):
        p1, p2 = uuid4(), uuid4()
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("p1-a", [evidence_factory()], project_id=p1)
            await repo.remember("p1-b", [evidence_factory()], project_id=p1)
            await repo.remember("p2-a", [evidence_factory()], project_id=p2)
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            assert await repo.count() == 3
            assert await repo.count(project_id=p1) == 2
            assert await repo.count(project_id=p2) == 1

    async def test_count_respects_status(
        self, session_factory, evidence_factory
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            item = await repo.remember("will archive", [evidence_factory()])
            await repo.remember("stays active", [evidence_factory()])
            await session.commit()
            await repo.update_status(item.id, MemoryItemStatus.ARCHIVED)
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            assert await repo.count() == 1  # default: active only
            assert await repo.count(status=MemoryItemStatus.ARCHIVED) == 1
            assert await repo.count(status=MemoryItemStatus.ACTIVE) == 1

    async def test_count_excludes_expired(
        self, session_factory, evidence_factory
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember(
                "expired", [evidence_factory()], expires_at=datetime(2020, 1, 1),
            )
            await repo.remember("alive", [evidence_factory()])
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            assert await repo.count() == 1
