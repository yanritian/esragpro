"""Transactional tests for P1-03 remember + outbox.

Each test runs against a real PostgreSQL database through the runner
script. These tests validate that the memory row, evidence rows, and
outbox rows share the caller-owned transaction boundary.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError

from app.db.models.memory_plane import (
    EvidenceRef as EvidenceRefRow,
    MemoryItem as MemoryItemRow,
    MemoryProjectionOutbox,
)
from app.memory_core.repositories.postgres_outbox import (
    DEFAULT_PROJECTION_TARGETS,
    EVENT_TYPE_REMEMBERED,
    SCHEMA_VERSION,
    build_idempotency_key,
)
from app.memory_core.repositories.postgres_repository import (
    PostgresMemoryRepository,
)
from app.memory_core.unpermissioned_contracts import (
    ProvenanceInfo,
)


@pytest.mark.asyncio
class TestTransactionalRemember:
    """remember() writes memory + evidence + 3 outbox rows atomically."""

    async def test_one_remember_produces_four_outbox_rows(
        self,
        session_factory,
        evidence_factory,
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            item = await repo.remember(
                content="transactional remember",
                evidence_refs=[evidence_factory()],
                provenance=ProvenanceInfo(
                    source_type="document",
                    source_name="spec.pdf",
                ),
            )
            # Before commit, inspect in-session state.
            outbox_rows = (
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
            assert len(outbox_rows) == 4
            targets = {row.projection_target for row in outbox_rows}
            assert targets == set(DEFAULT_PROJECTION_TARGETS)
            for row in outbox_rows:
                assert row.event_type == EVENT_TYPE_REMEMBERED
                assert row.schema_version == SCHEMA_VERSION
                assert row.state == "pending"
                assert row.attempt_count == 0
                assert row.max_attempts == 8
                assert row.idempotency_key == build_idempotency_key(
                    item.id,
                    row.projection_target,
                )
            # Exactly one memory row and one evidence row visible in-session.
            mem_count = (
                await session.execute(sa_select(MemoryItemRow).where(MemoryItemRow.id == item.id))
            ).scalar_one_or_none()
            assert mem_count is not None
            ev_count = (
                (
                    await session.execute(
                        sa_select(EvidenceRefRow).where(EvidenceRefRow.memory_item_id == item.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(ev_count) == 1
            await session.commit()

    async def test_caller_rollback_removes_everything(
        self,
        session_factory,
        evidence_factory,
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            item = await repo.remember(
                content="rollback everything",
                evidence_refs=[evidence_factory()],
            )
            item_id = item.id
            # Do NOT commit: session exit rolls back.
        async with session_factory() as session2:
            mem = (
                await session2.execute(sa_select(MemoryItemRow).where(MemoryItemRow.id == item_id))
            ).scalar_one_or_none()
            assert mem is None
            evs = (
                (
                    await session2.execute(
                        sa_select(EvidenceRefRow).where(EvidenceRefRow.memory_item_id == item_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(evs) == 0
            out = (
                (
                    await session2.execute(
                        sa_select(MemoryProjectionOutbox).where(
                            MemoryProjectionOutbox.aggregate_id == item_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(out) == 0

    async def test_caller_commit_survives_new_session(
        self,
        session_factory,
        evidence_factory,
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            item = await repo.remember(
                content="commit survives",
                evidence_refs=[evidence_factory(source_id="persist")],
            )
            item_id = item.id
            await session.commit()
        async with session_factory() as session2:
            out = (
                (
                    await session2.execute(
                        sa_select(MemoryProjectionOutbox).where(
                            MemoryProjectionOutbox.aggregate_id == item_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(out) == 4
            repo2 = PostgresMemoryRepository(session2)
            fetched = await repo2.get_by_id(item_id)
            assert fetched is not None
            assert fetched.content == "commit survives"
            # The fresh session must also see the persisted evidence row
            # with the caller-supplied source_id intact.
            ev_rows = (
                (
                    await session2.execute(
                        sa_select(EvidenceRefRow).where(EvidenceRefRow.memory_item_id == item_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(ev_rows) == 1
            assert ev_rows[0].source_id == "persist"

    async def test_outbox_flush_failure_propagates_through_remember(
        self,
        session_factory,
        evidence_factory,
        monkeypatch,
    ):
        """A real idempotency conflict during remember() must propagate.

        We monkeypatch only the module-level symbol imported by the
        repository; the real helper implementation, the SQLAlchemy
        session, flush, and PostgreSQL unique constraint all remain
        intact. The wrapper invokes the real helper twice for the same
        item/targets so the second flush raises IntegrityError.
        """
        import app.memory_core.repositories.postgres_repository as repo_mod
        from app.memory_core.repositories.postgres_outbox import (
            enqueue_remembered_events as _real_enqueue,
        )

        captured: dict = {"item": None}

        async def _wrapper(session, *, memory_item, targets=None):
            captured["item"] = memory_item
            # First real call adds outbox rows and flushes successfully.
            await _real_enqueue(
                session,
                memory_item=memory_item,
                targets=targets,
            )
            # Second real call for the same item/targets hits the real
            # idempotency unique constraint and raises IntegrityError.
            await _real_enqueue(
                session,
                memory_item=memory_item,
                targets=targets,
            )

        monkeypatch.setattr(
            repo_mod,
            "enqueue_remembered_events",
            _wrapper,
        )

        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            with pytest.raises(IntegrityError):
                await repo.remember(
                    content="outbox flush fail",
                    evidence_refs=[evidence_factory()],
                )
            await session.rollback()

        captured_id = captured["item"].id
        async with session_factory() as check:
            assert (
                await check.execute(sa_select(MemoryItemRow).where(MemoryItemRow.id == captured_id))
            ).scalar_one_or_none() is None
            assert (
                await check.execute(
                    sa_select(EvidenceRefRow).where(EvidenceRefRow.memory_item_id == captured_id)
                )
            ).scalars().all() == []
            assert (
                await check.execute(
                    sa_select(MemoryProjectionOutbox).where(
                        MemoryProjectionOutbox.aggregate_id == captured_id
                    )
                )
            ).scalars().all() == []

    async def test_session_remains_active_after_remember(
        self,
        session_factory,
        evidence_factory,
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            await repo.remember(
                content="still active",
                evidence_refs=[evidence_factory()],
            )
            # Session must still be in a transaction and usable.
            assert session.in_transaction()
            await session.commit()
            # After commit the session is still usable.
            await repo.remember(
                content="second item",
                evidence_refs=[evidence_factory()],
            )
            await session.commit()


@pytest.mark.asyncio
class TestCustomTargets:
    async def test_custom_targets_produce_exact_rows(
        self,
        session_factory,
        evidence_factory,
    ):
        async with session_factory() as session:
            repo = PostgresMemoryRepository(
                session,
                projection_targets=["webhook", "s3-archive"],
            )
            item = await repo.remember(
                content="custom targets",
                evidence_refs=[evidence_factory()],
            )
            out = (
                (
                    await session.execute(
                        __import__("sqlalchemy")
                        .select(MemoryProjectionOutbox)
                        .where(MemoryProjectionOutbox.aggregate_id == item.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(out) == 2
            targets = {r.projection_target for r in out}
            assert targets == {"webhook", "s3-archive"}
            await session.commit()
