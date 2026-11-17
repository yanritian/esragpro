"""P1-02 integration tests: query dimensions, status, evidence, text, order."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.memory_core.repositories.postgres_repository import (
    PostgresMemoryRepository,
)
from app.memory_core.unpermissioned_contracts import MemoryItemStatus


@pytest.mark.asyncio
class TestQueryDimensions:
    """Dimension filters compose independently and jointly."""

    async def test_dimension_filters_independently(
        self, session_factory, evidence_factory
    ):
        p1, p2 = uuid4(), uuid4()
        s1, s2 = uuid4(), uuid4()
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("p1 s1", [evidence_factory()], project_id=p1, session_id=s1)
            await repo.remember("p1 s2", [evidence_factory()], project_id=p1, session_id=s2)
            await repo.remember("p2 s1", [evidence_factory()], project_id=p2, session_id=s1)
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            assert len(await repo.query(project_id=p1)) == 2
            assert len(await repo.query(session_id=s1)) == 2
            r = await repo.query(project_id=p1, session_id=s2)
            assert len(r) == 1 and r[0].content == "p1 s2"

    async def test_dimension_filters_combined(
        self, session_factory, evidence_factory
    ):
        p1, a1 = uuid4(), uuid4()
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("match", [evidence_factory()], project_id=p1, agent_id=a1)
            await repo.remember("no agent", [evidence_factory()], project_id=p1)
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            r = await repo.query(project_id=p1, agent_id=a1)
            assert len(r) == 1 and r[0].content == "match"


@pytest.mark.asyncio
class TestQueryStatusAndExpiration:
    """Status defaults to ACTIVE; expired rows are hidden."""

    async def test_default_status_hides_non_active(
        self, session_factory, evidence_factory
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            item = await repo.remember("active one", [evidence_factory()])
            await session.commit()
            await session.execute(
                text("UPDATE memory_items SET status='archived' WHERE id=:id"),
                {"id": item.id},
            )
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            assert len(await repo.query()) == 0
            r = await repo.query(status=MemoryItemStatus.ARCHIVED)
            assert len(r) == 1

    async def test_explicit_status_returns_matching(
        self, session_factory, evidence_factory
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("archived", [evidence_factory()])
            item = await repo.remember("to be archived", [evidence_factory()])
            await session.commit()
            await repo.update_status(item.id, MemoryItemStatus.ARCHIVED)
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            r = await repo.query(status=MemoryItemStatus.ARCHIVED)
            assert len(r) == 1 and r[0].content == "to be archived"
            r = await repo.query(status=MemoryItemStatus.ACTIVE)
            assert len(r) == 1 and r[0].content == "archived"

    async def test_expiration_hides_elapsed(
        self, session_factory, evidence_factory
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("expired", [evidence_factory()], expires_at=datetime(2020, 1, 1))
            await repo.remember("not expired", [evidence_factory()], expires_at=datetime(2099, 1, 1))
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            r = await repo.query()
            assert len(r) == 1 and r[0].content == "not expired"

    async def test_start_inclusive_end_exclusive(
        self, session_factory, evidence_factory
    ):
        # Given three committed rows with distinct DB timestamps.
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("first", [evidence_factory()])
            await session.commit()
            middle = await repo.remember("middle", [evidence_factory()])
            await session.commit()
            last = await repo.remember("last", [evidence_factory()])
            await session.commit()
        # When the range is [middle.created_at, last.created_at).
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            middle_fresh = await repo.get_by_id(middle.id)
            last_fresh = await repo.get_by_id(last.id)
            r = await repo.query(
                start_time=middle_fresh.created_at,
                end_time=last_fresh.created_at,
            )
            # Then middle is included, first and last are excluded.
            contents = {item.content for item in r}
            assert "first" not in contents
            assert "middle" in contents
            assert "last" not in contents


@pytest.mark.asyncio
class TestQueryEvidenceFilters:
    """require_evidence and min_confidence filter correctly."""

    async def test_require_evidence(self, session_factory, evidence_factory):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("with evidence", [evidence_factory()])
            await repo.remember("without evidence", [])
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            r = await repo.query(require_evidence=True)
            assert len(r) == 1 and r[0].content == "with evidence"

    async def test_min_confidence(self, session_factory, evidence_factory):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("high conf", [evidence_factory(confidence=0.9)])
            await repo.remember("low conf", [evidence_factory(confidence=0.3)])
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            r = await repo.query(min_confidence=0.5)
            assert len(r) == 1 and r[0].content == "high conf"


@pytest.mark.asyncio
class TestQueryLexicalMatching:
    """Case-insensitive term matching including CJK and blank queries."""

    async def test_multi_term_latin(self, session_factory, evidence_factory):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("alpha beta gamma", [evidence_factory()])
            await repo.remember("alpha beta", [evidence_factory()])
            await repo.remember("alpha only", [evidence_factory()])
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            r = await repo.query(query_text="alpha beta")
            contents = {item.content for item in r}
            assert "alpha beta gamma" in contents
            assert "alpha beta" in contents
            assert "alpha only" not in contents

    async def test_single_token_latin(self, session_factory, evidence_factory):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("the quick brown fox", [evidence_factory()])
            await repo.remember("slow turtle", [evidence_factory()])
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            r = await repo.query(query_text="quick")
            assert len(r) == 1 and r[0].content == "the quick brown fox"

    async def test_cjk_single_token(self, session_factory, evidence_factory):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("这是一个测试文档", [evidence_factory()])
            await repo.remember("另一个内容", [evidence_factory()])
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            r = await repo.query(query_text="测试")
            assert len(r) == 1 and "测试" in r[0].content

    async def test_blank_query_means_dimension_only(
        self, session_factory, evidence_factory
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("any content", [evidence_factory()])
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            assert len(await repo.query(query_text="")) == 1
            assert len(await repo.query(query_text=None)) == 1
            assert len(await repo.query(query_text="   ")) == 1

    async def test_no_match(self, session_factory, evidence_factory):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("hello world", [evidence_factory()])
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            assert await repo.query(query_text="nonexistent") == []


@pytest.mark.asyncio
class TestQueryOrdering:
    """Results are newest-first and respect max_items."""

    async def test_newest_first_deterministic(
        self, session_factory, evidence_factory
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("first", [evidence_factory()])
            await session.commit()
            await repo.remember("second", [evidence_factory()])
            await session.commit()
            await repo.remember("third", [evidence_factory()])
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            r = await repo.query()
            assert [x.content for x in r] == ["third", "second", "first"]

    async def test_max_items_enforced(self, session_factory, evidence_factory):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            for i in range(5):
                await repo.remember(f"item-{i}", [evidence_factory()])
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            assert len(await repo.query(max_items=3)) == 3

    async def test_non_positive_max_items(self, session_factory, evidence_factory):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("item", [evidence_factory()])
            await session.commit()
        async with session_factory() as session2:
            repo = PostgresMemoryRepository(session2)
            assert await repo.query(max_items=0) == []
            assert await repo.query(max_items=-1) == []
