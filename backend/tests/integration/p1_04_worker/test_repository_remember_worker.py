"""Integration tests for repository remember() with worker (P1-04).

Tests real repository remember() creating outbox rows processed by worker.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models.memory_plane import MemoryProjectionOutbox as OutboxRow
from app.memory_core.projection_worker import (
    ProjectionWorker,
    ProjectionHandlerRegistry,
    ProjectionEvent,
)
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.repositories.postgres_repository import PostgresMemoryRepository
from app.memory_core.unpermissioned_contracts import ProvenanceInfo


class TestRealRepositoryRememberToProjections:
    """Tests for real repository remember() creating outbox rows."""

    @pytest.mark.asyncio
    async def test_repository_remember_creates_four_outbox_rows(
        self, session_factory, evidence_factory
    ) -> None:
        """PostgresMemoryRepository.remember() creates exactly four outbox rows."""
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            item = await repo.remember(
                content="test memory",
                evidence_refs=[evidence_factory(source_id="doc-remember-test")],
                provenance=ProvenanceInfo(
                    source_type="document",
                    source_name="test.pdf",
                ),
            )
            aggregate_id = item.id
            await session.commit()

        # Fresh session: assert exactly four outbox rows
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(OutboxRow.aggregate_id == aggregate_id)
            )
            rows = list(result.scalars().all())

            assert len(rows) == 4
            targets = {row.projection_target for row in rows}
            assert targets == {"redis", "elasticsearch", "neo4j", "minio"}

            for row in rows:
                assert row.state == "pending"
                assert row.attempt_count == 0

    @pytest.mark.asyncio
    async def test_real_worker_processes_four_remember_rows(
        self, session_factory, evidence_factory
    ) -> None:
        """Complete registry worker cycle: remember -> four outbox rows -> all complete."""
        side_effects: dict[str, list[str]] = {
            "redis": [],
            "elasticsearch": [],
            "neo4j": [],
            "minio": [],
        }

        async def make_handler(target: str):
            async def handler(event: ProjectionEvent) -> None:
                side_effects[target].append(f"processed:{event.idempotency_key}")

            return handler

        registry = ProjectionHandlerRegistry(
            {
                "redis": await make_handler("redis"),
                "elasticsearch": await make_handler("elasticsearch"),
                "neo4j": await make_handler("neo4j"),
                "minio": await make_handler("minio"),
            }
        )

        # Remember using real repository
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session)
            item = await repo.remember(
                content="test memory for worker",
                evidence_refs=[evidence_factory(source_id="doc-worker-test")],
                provenance=ProvenanceInfo(
                    source_type="document",
                    source_name="worker-test.pdf",
                ),
            )
            aggregate_id = item.id
            await session.commit()

        # Run real worker
        store = ProjectionOutboxStore(session_factory, "test-worker")
        retry_policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)
        worker = ProjectionWorker(
            store=store,
            registry=registry,
            worker_identity="test-worker",
            batch_size=10,
            max_concurrency=10,
            lease_duration=timedelta(seconds=60),
            delivery_timeout=timedelta(seconds=30),
            finalization_margin=timedelta(seconds=5),
            retry_policy=retry_policy,
        )

        result = await worker.run_once()

        assert result.claimed == 4
        assert result.completed == 4

        assert len(side_effects["redis"]) == 1
        assert len(side_effects["elasticsearch"]) == 1
        assert len(side_effects["neo4j"]) == 1
        assert len(side_effects["minio"]) == 1

        # Verify all rows are completed
        async with session_factory() as session:
            result_q = await session.execute(
                select(OutboxRow).where(OutboxRow.aggregate_id == aggregate_id)
            )
            rows = list(result_q.scalars().all())
            assert len(rows) == 4
            for row in rows:
                assert row.state == "completed"


__all__ = ["TestRealRepositoryRememberToProjections"]
