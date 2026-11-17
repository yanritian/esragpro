"""Outbox contract tests for P1-03.

Validates payload content, absence of permission-shaped keys, and the
behavioral contract that reads / ``update_status()`` / ``query()`` /
``count()`` do not emit additional outbox events.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select as sa_select

from app.db.models.memory_plane import MemoryProjectionOutbox
from app.memory_core.repositories.postgres_outbox import EVENT_TYPE_REMEMBERED
from app.memory_core.repositories.postgres_repository import (
    PostgresMemoryRepository,
)
from app.memory_core.unpermissioned_contracts import (
    MemoryItemStatus,
    ProvenanceInfo,
)

_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "tenant",
        "principal",
        "owner_subject",
        "user",
        "role",
        "permission",
        "acl",
        "scope",
        "oauth",
        "authorization",
        "auth",
        "owner",
    }
)


def _walk_keys(obj, acc: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(k)
            _walk_keys(v, acc)
    elif isinstance(obj, list):
        for item in obj:
            _walk_keys(item, acc)


@pytest.mark.asyncio
class TestPayloadSnapshot:
    async def test_payload_matches_stored_memory(
        self,
        session_factory,
        evidence_factory,
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            project_id = uuid4()
            item = await repo.remember(
                content="payload check",
                evidence_refs=[evidence_factory(source_id="doc-9")],
                provenance=ProvenanceInfo(
                    source_type="document",
                    source_name="spec.pdf",
                ),
                project_id=project_id,
            )
            out = (
                (
                    await session.execute(
                        sa_select(MemoryProjectionOutbox).where(
                            MemoryProjectionOutbox.aggregate_id == item.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(out) == 4
            for row in out:
                payload = row.payload
                assert payload["event_type"] == EVENT_TYPE_REMEMBERED
                assert payload["projection_target"] == row.projection_target
                snap = payload["memory_snapshot"]
                assert snap["id"] == str(item.id)
                assert snap["content"] == "payload check"
                assert snap["status"] == "active"
                assert snap["project_id"] == str(project_id)
                assert len(snap["evidence_refs"]) == 1
                assert snap["evidence_refs"][0]["source_id"] == "doc-9"
            await session.commit()


@pytest.mark.asyncio
class TestPayloadSafety:
    async def test_payload_contains_no_permission_keys(
        self,
        session_factory,
        evidence_factory,
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            item = await repo.remember(
                content="no auth keys",
                evidence_refs=[evidence_factory()],
                metadata={"safe": True},
            )
            out = (
                (
                    await session.execute(
                        sa_select(MemoryProjectionOutbox).where(
                            MemoryProjectionOutbox.aggregate_id == item.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in out:
                keys: set[str] = set()
                _walk_keys(row.payload, keys)
                for forbidden in _FORBIDDEN_PAYLOAD_KEYS:
                    assert forbidden not in keys, f"Forbidden key {forbidden!r} in payload"
            await session.commit()


@pytest.mark.asyncio
class TestReadsDoNotEmitEvents:
    async def test_get_by_id_emits_no_events(
        self,
        session_factory,
        evidence_factory,
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            item = await repo.remember(
                content="read only",
                evidence_refs=[evidence_factory()],
            )
            await session.commit()
            before = (
                (
                    await session.execute(
                        sa_select(MemoryProjectionOutbox).where(
                            MemoryProjectionOutbox.aggregate_id == item.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            fetched = await repo.get_by_id(item.id)
            assert fetched is not None
            after = (
                (
                    await session.execute(
                        sa_select(MemoryProjectionOutbox).where(
                            MemoryProjectionOutbox.aggregate_id == item.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(after) == len(before)

    async def test_query_emits_no_events(
        self,
        session_factory,
        evidence_factory,
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember(
                content="query no events",
                evidence_refs=[evidence_factory()],
            )
            await session.commit()
            total_before = (
                (await session.execute(sa_select(MemoryProjectionOutbox))).scalars().all()
            )
            results = await repo.query(query_text="query no events")
            assert len(results) == 1
            total_after = (await session.execute(sa_select(MemoryProjectionOutbox))).scalars().all()
            assert len(total_after) == len(total_before)

    async def test_count_emits_no_events(
        self,
        session_factory,
        evidence_factory,
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember(
                content="count no events",
                evidence_refs=[evidence_factory()],
            )
            await session.commit()
            before = (await session.execute(sa_select(MemoryProjectionOutbox))).scalars().all()
            cnt = await repo.count()
            assert cnt >= 1
            after = (await session.execute(sa_select(MemoryProjectionOutbox))).scalars().all()
            assert len(after) == len(before)

    async def test_update_status_emits_no_events(
        self,
        session_factory,
        evidence_factory,
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            item = await repo.remember(
                content="status change",
                evidence_refs=[evidence_factory()],
            )
            await session.commit()
            before = (
                (
                    await session.execute(
                        sa_select(MemoryProjectionOutbox).where(
                            MemoryProjectionOutbox.aggregate_id == item.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            updated = await repo.update_status(
                item.id,
                MemoryItemStatus.EXPIRED,
            )
            assert updated is not None
            assert updated.status == MemoryItemStatus.EXPIRED
            after = (
                (
                    await session.execute(
                        sa_select(MemoryProjectionOutbox).where(
                            MemoryProjectionOutbox.aggregate_id == item.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(after) == len(before)
