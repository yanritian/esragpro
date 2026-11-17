"""P1-02 integration tests: write, read, and structural safety."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.memory_core.repositories.postgres_repository import (
    PostgresMemoryRepository,
)
from app.memory_core.unpermissioned_contracts import (
    EvidenceRef,
    MemoryItemStatus,
    ProvenanceInfo,
)

_FORBIDDEN_FIELDS = (
    "tenant", "principal", "owner_subject", "user", "role",
    "permission", "acl", "scope", "oauth", "authorization",
)


@pytest.mark.asyncio
class TestRemember:
    """remember() flushes rows and evidence but never commits."""

    async def test_remember_flushes_memory_and_evidence(
        self, session_factory, evidence_factory
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            evidence = [
                EvidenceRef(
                    source_id="doc-a", source_type="document",
                    timestamp=evidence_factory().timestamp,
                    chunk_id="c1", confidence=0.9, metadata={"page": 1},
                ),
                EvidenceRef(
                    source_id="doc-b", source_type="document",
                    timestamp=evidence_factory().timestamp, confidence=0.7,
                ),
            ]
            result = await repo.remember(
                content="Test memory",
                evidence_refs=evidence,
                provenance=ProvenanceInfo(
                    source_type="document", source_name="test.pdf",
                ),
                project_id=uuid4(),
                metadata={"key": "value"},
            )
            assert result.id is not None
            assert result.content == "Test memory"
            assert result.status == MemoryItemStatus.ACTIVE
            assert len(result.evidence_refs) == 2
            assert result.evidence_refs[0].source_id == "doc-a"
            assert result.evidence_refs[1].source_id == "doc-b"
            assert result.evidence_refs[0].metadata == {"page": 1}
            assert result.provenance is not None
            assert result.provenance.source_name == "test.pdf"
            assert result.metadata == {"key": "value"}
            fetched = await repo.get_by_id(result.id)
            assert fetched is not None
            assert fetched.content == "Test memory"
            await session.commit()

    async def test_caller_rollback_removes_both(
        self, session_factory, evidence_factory
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            result = await repo.remember(
                content="Rollback me", evidence_refs=[evidence_factory()],
            )
            item_id = result.id
        async with session_factory() as session2:
            repo2 = PostgresMemoryRepository(session2)
            assert await repo2.get_by_id(item_id) is None

    async def test_caller_commit_survives_new_session(
        self, session_factory, evidence_factory
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            result = await repo.remember(
                content="Persist me",
                evidence_refs=[evidence_factory(source_id="persist-doc")],
            )
            item_id = result.id
            await session.commit()
        async with session_factory() as session2:
            repo2 = PostgresMemoryRepository(session2)
            fetched = await repo2.get_by_id(item_id)
            assert fetched is not None
            assert fetched.content == "Persist me"
            assert len(fetched.evidence_refs) == 1
            assert fetched.evidence_refs[0].source_id == "persist-doc"

    async def test_remember_does_not_mutate_caller_collections(
        self, session_factory, evidence_factory
    ):
        evidence = [evidence_factory(source_id="original")]
        metadata = {"original": True}
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember(
                content="Immutable check",
                evidence_refs=evidence, metadata=metadata,
            )
            assert len(evidence) == 1
            assert evidence[0].source_id == "original"
            assert metadata == {"original": True}
            await session.commit()


@pytest.mark.asyncio
class TestGetById:
    """get_by_id() returns all fields or ``None`` for missing rows."""

    async def test_get_by_id_returns_all_fields(
        self, session_factory, evidence_factory
    ):
        project_id = uuid4()
        session_id = uuid4()
        agent_id = uuid4()
        kb_id = uuid4()
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            created = await repo.remember(
                content="Full fields",
                evidence_refs=[
                    evidence_factory(source_id="e1", confidence=0.8),
                    evidence_factory(source_id="e2", confidence=0.6),
                ],
                provenance=ProvenanceInfo(
                    source_type="conversation",
                    source_name="chat-1",
                    source_uri="chat://1",
                    extracted_by="bot",
                ),
                project_id=project_id,
                session_id=session_id,
                agent_id=agent_id,
                knowledge_base_id=kb_id,
                expires_at=datetime(2099, 1, 1),
                metadata={"tag": "important"},
            )
            await session.commit()
        async with session_factory() as session2:
            repo2 = PostgresMemoryRepository(session2)
            fetched = await repo2.get_by_id(created.id)
            assert fetched is not None
            assert fetched.content == "Full fields"
            assert fetched.project_id == project_id
            assert fetched.session_id == session_id
            assert fetched.agent_id == agent_id
            assert fetched.knowledge_base_id == kb_id
            assert fetched.metadata == {"tag": "important"}
            assert len(fetched.evidence_refs) == 2
            assert fetched.provenance is not None
            assert fetched.provenance.source_type == "conversation"
            assert fetched.provenance.extracted_by == "bot"

    async def test_get_by_id_missing_returns_none(self, session_factory):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            assert await repo.get_by_id(uuid4()) is None


@pytest.mark.asyncio
class TestNoPermissionFields:
    """Persisted rows must not contain authorization-shaped columns."""

    async def test_persisted_rows_have_no_permission_fields(
        self, session_factory, evidence_factory
    ):
        from sqlalchemy.ext.asyncio import create_async_engine
        import os

        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember("no auth fields", [evidence_factory()])
            await session.commit()

        url = os.environ.get("DATABASE_URL")
        assert url is not None
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='memory_items' "
                    "ORDER BY ordinal_position"
                ))
                columns = [row[0] for row in result]
                for col in columns:
                    low = col.lower()
                    for forbidden in _FORBIDDEN_FIELDS:
                        assert forbidden not in low, (
                            f"Forbidden field {forbidden!r} found in {col}"
                        )
        finally:
            await engine.dispose()
