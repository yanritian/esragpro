"""P1-07 Neo4j projection integration tests.

Covers:
- Real worker projection (remember → outbox → worker → graph query)
- Exact round-trip fidelity (write then read through Neo4j)
- Duplicate/conflict handling (idempotency)
- Evidence and provenance graph traversal
- Outage/recovery (Neo4j Docker stop/start)
- Persistence restart (constraints survive reinstantiation)
- PostgreSQL readability during Neo4j outage
- Composition preflight no-claim behavior
- Full pipeline delivery: completed + dead_letter

Uses real PostgreSQL and real Neo4j 5.15.
No ACL/permission/tenant fields anywhere.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models.memory_plane import (
    MemoryItem as MemoryItemRow,
    MemoryProjectionOutbox as OutboxRow,
)
from app.memory_core.neo4j_compose import compose_and_run_neo4j_worker
from app.memory_core.neo4j_handler import Neo4jProjectionHandler
from app.memory_core.projection_worker.contracts import (
    ProjectionEvent,
    ProjectionHandlerRegistry,
)
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.projection_worker.worker import ProjectionWorker
from app.memory_core.repositories.neo4j_projection.adapter import (
    IdempotencyConflictError,
    Neo4jProjectionAdapter,
)
from app.memory_core.repositories.postgres_outbox import (
    build_event_payload,
    build_idempotency_key,
    enqueue_remembered_events,
)
from app.memory_core.unpermissioned_contracts import (
    MemoryItem,
    MemoryItemStatus,
    ProvenanceInfo,
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _make_memory_item(
    *,
    content: str = "test content",
    project_id=None,
    session_id=None,
    agent_id=None,
    knowledge_base_id=None,
    status: MemoryItemStatus = MemoryItemStatus.ACTIVE,
    evidence_refs: Optional[list] = None,
    provenance: Optional[ProvenanceInfo] = None,
) -> MemoryItem:
    mid = uuid4()
    now = _now_utc().replace(tzinfo=None)
    return MemoryItem(
        id=mid,
        content=content,
        status=status,
        created_at=now,
        updated_at=now,
        expires_at=None,
        project_id=project_id,
        session_id=session_id,
        agent_id=agent_id,
        knowledge_base_id=knowledge_base_id,
        provenance=provenance,
        metadata={},
        evidence_refs=evidence_refs or [],
    )


# ---------------------------------------------------------------------------
# Exact round-trip tests
# ---------------------------------------------------------------------------


class TestExactRoundTrip:
    """Test write-then-read fidelity through Neo4j."""

    @pytest.mark.asyncio
    async def test_project_and_get(self, neo4j_uri, neo4j_user, neo4j_password):
        adapter = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            await adapter.create_constraints()
            mid = str(uuid4())
            await adapter.project_memory_item(
                memory_id=mid,
                content="Hello world test content",
                status="active",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                expires_at=None,
                project_id=str(uuid4()),
                session_id=None,
                agent_id=None,
                knowledge_base_id=None,
                metadata={"key": "value"},
                idempotency_key=f"key-{mid}",
                event_type="memory.remembered",
                schema_version=1,
                projection_target="neo4j",
                evidence_refs=[],
                provenance=None,
            )

            result = await adapter.get_memory_item(mid)
            assert result is not None
            assert result["id"] == mid
            assert result["content"] == "Hello world test content"
            assert result["status"] == "active"
            assert result["metadata"] == {"key": "value"}
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_project_idempotent(self, neo4j_uri, neo4j_user, neo4j_password):
        adapter = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            await adapter.create_constraints()
            mid = str(uuid4())
            key = f"idem-{mid}"

            await adapter.project_memory_item(
                memory_id=mid,
                content="idempotent test",
                status="active",
                created_at=None,
                updated_at=None,
                expires_at=None,
                project_id=None,
                session_id=None,
                agent_id=None,
                knowledge_base_id=None,
                metadata={},
                idempotency_key=key,
                event_type="memory.remembered",
                schema_version=1,
                projection_target="neo4j",
                evidence_refs=[],
                provenance=None,
            )

            # Second call with same key = idempotent update, no error.
            await adapter.project_memory_item(
                memory_id=mid,
                content="idempotent test updated",
                status="active",
                created_at=None,
                updated_at=None,
                expires_at=None,
                project_id=None,
                session_id=None,
                agent_id=None,
                knowledge_base_id=None,
                metadata={},
                idempotency_key=key,
                event_type="memory.remembered",
                schema_version=1,
                projection_target="neo4j",
                evidence_refs=[],
                provenance=None,
            )

            result = await adapter.get_memory_item(mid)
            assert result is not None
            assert result["content"] == "idempotent test updated"
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_delete(self, neo4j_uri, neo4j_user, neo4j_password):
        adapter = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            await adapter.create_constraints()
            mid = str(uuid4())
            await adapter.project_memory_item(
                memory_id=mid,
                content="delete me",
                status="active",
                created_at=None,
                updated_at=None,
                expires_at=None,
                project_id=None,
                session_id=None,
                agent_id=None,
                knowledge_base_id=None,
                metadata={},
                idempotency_key=f"del-{mid}",
                event_type="memory.remembered",
                schema_version=1,
                projection_target="neo4j",
                evidence_refs=[],
                provenance=None,
            )

            deleted = await adapter.delete_memory_item(mid)
            assert deleted is True

            result = await adapter.get_memory_item(mid)
            assert result is None
        finally:
            await adapter.close()


# ---------------------------------------------------------------------------
# Evidence and provenance graph traversal
# ---------------------------------------------------------------------------


class TestEvidenceProvenanceTraversal:
    """Test evidence/provenance as graph nodes with relationships."""

    @pytest.mark.asyncio
    async def test_evidence_nodes_linked(self, neo4j_uri, neo4j_user, neo4j_password):
        adapter = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            await adapter.create_constraints()
            mid = str(uuid4())
            evidence = [
                {
                    "source_id": "doc-1",
                    "source_type": "document",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "chunk_id": "chunk-42",
                    "confidence": 0.95,
                    "metadata": {"page": 12},
                    "position": 0,
                },
                {
                    "source_id": "doc-2",
                    "source_type": "conversation",
                    "timestamp": "2026-01-02T00:00:00+00:00",
                    "chunk_id": None,
                    "confidence": 0.8,
                    "metadata": {"turn": 5},
                    "position": 1,
                },
            ]
            await adapter.project_memory_item(
                memory_id=mid,
                content="evidence test",
                status="active",
                created_at=None,
                updated_at=None,
                expires_at=None,
                project_id=None,
                session_id=None,
                agent_id=None,
                knowledge_base_id=None,
                metadata={},
                idempotency_key=f"ev-{mid}",
                event_type="memory.remembered",
                schema_version=1,
                projection_target="neo4j",
                evidence_refs=evidence,
                provenance=None,
            )

            result = await adapter.get_memory_item(mid)
            assert result is not None
            assert len(result["evidence_refs"]) == 2
            assert result["evidence_refs"][0]["source_id"] == "doc-1"
            assert result["evidence_refs"][1]["source_id"] == "doc-2"
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_provenance_node_linked(self, neo4j_uri, neo4j_user, neo4j_password):
        adapter = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            await adapter.create_constraints()
            mid = str(uuid4())
            provenance = {
                "source_type": "document",
                "source_name": "test-doc.pdf",
                "source_uri": "file:///docs/test-doc.pdf",
                "ingestion_job_id": str(uuid4()),
                "version": 3,
                "extracted_at": "2026-01-01T00:00:00+00:00",
                "extracted_by": "test-agent",
            }
            await adapter.project_memory_item(
                memory_id=mid,
                content="provenance test",
                status="active",
                created_at=None,
                updated_at=None,
                expires_at=None,
                project_id=None,
                session_id=None,
                agent_id=None,
                knowledge_base_id=None,
                metadata={},
                idempotency_key=f"prov-{mid}",
                event_type="memory.remembered",
                schema_version=1,
                projection_target="neo4j",
                evidence_refs=[],
                provenance=provenance,
            )

            result = await adapter.get_memory_item(mid)
            assert result is not None
            assert result["provenance"] is not None
            assert result["provenance"]["source_name"] == "test-doc.pdf"
            assert result["provenance"]["version"] == 3
        finally:
            await adapter.close()


# ---------------------------------------------------------------------------
# Duplicate/conflict handling tests
# ---------------------------------------------------------------------------


class TestDuplicateConflict:
    """Test idempotent duplicate and conflict handling."""

    @pytest.mark.asyncio
    async def test_conflicting_idempotency_raises(self, neo4j_uri, neo4j_user, neo4j_password):
        adapter = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            await adapter.create_constraints()
            mid = str(uuid4())

            await adapter.project_memory_item(
                memory_id=mid,
                content="first write",
                status="active",
                created_at=None,
                updated_at=None,
                expires_at=None,
                project_id=None,
                session_id=None,
                agent_id=None,
                knowledge_base_id=None,
                metadata={},
                idempotency_key=f"alpha-{mid}",
                event_type="memory.remembered",
                schema_version=1,
                projection_target="neo4j",
                evidence_refs=[],
                provenance=None,
            )

            # Same mid, different idempotency_key → conflict.
            with pytest.raises(IdempotencyConflictError):
                await adapter.project_memory_item(
                    memory_id=mid,
                    content="conflicting write",
                    status="active",
                    created_at=None,
                    updated_at=None,
                    expires_at=None,
                    project_id=None,
                    session_id=None,
                    agent_id=None,
                    knowledge_base_id=None,
                    metadata={},
                    idempotency_key=f"beta-{mid}",
                    event_type="memory.remembered",
                    schema_version=1,
                    projection_target="neo4j",
                    evidence_refs=[],
                    provenance=None,
                )
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_idempotency_key_deterministic(self):
        agg_id = uuid4()
        k1 = build_idempotency_key(agg_id, "neo4j")
        k2 = build_idempotency_key(agg_id, "neo4j")
        assert k1 == k2
        k3 = build_idempotency_key(agg_id, "redis")
        assert k1 != k3


# ---------------------------------------------------------------------------
# Outage/recovery tests
# ---------------------------------------------------------------------------


class TestOutageRecovery:
    """Test recovery after Neo4j connection loss."""

    @pytest.mark.asyncio
    async def test_real_docker_stop_start_recovery(
        self,
        neo4j_uri,
        neo4j_user,
        neo4j_password,
        docker_neo4j,
    ):
        """Stop Neo4j container, observe failure, restart, verify recovery."""
        from tests.integration.p1_07_neo4j.conftest import _wait_for_neo4j_healthy

        adapter = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            await adapter.create_constraints()
            mid = str(uuid4())
            await adapter.project_memory_item(
                memory_id=mid,
                content="pre-outage content",
                status="active",
                created_at=None,
                updated_at=None,
                expires_at=None,
                project_id=None,
                session_id=None,
                agent_id=None,
                knowledge_base_id=None,
                metadata={},
                idempotency_key=f"outage-{mid}",
                event_type="memory.remembered",
                schema_version=1,
                projection_target="neo4j",
                evidence_refs=[],
                provenance=None,
            )

            result = await adapter.get_memory_item(mid)
            assert result is not None
            assert result["content"] == "pre-outage content"
        finally:
            await adapter.close()

        # Stop Neo4j container.
        subprocess.run(
            [
                docker_neo4j["docker_exe"],
                "compose",
                "-p",
                docker_neo4j["compose_project"],
                "-f",
                docker_neo4j["compose_file"],
                "stop",
                "neo4j",
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )

        # Observe connection failure.
        outage_error = None
        adapter2 = Neo4jProjectionAdapter.__new__(Neo4jProjectionAdapter)
        from neo4j import AsyncGraphDatabase

        driver = AsyncGraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
        adapter2._driver = driver
        adapter2._owns_driver = True
        try:
            await adapter2.get_memory_item(mid)
        except Exception as exc:
            outage_error = exc
        finally:
            await driver.close()

        assert outage_error is not None, "Expected connection error after Docker stop"

        # Restart Neo4j container.
        subprocess.run(
            [
                docker_neo4j["docker_exe"],
                "compose",
                "-p",
                docker_neo4j["compose_project"],
                "-f",
                docker_neo4j["compose_file"],
                "up",
                "-d",
                "--wait",
                "neo4j",
            ],
            capture_output=True,
            timeout=120,
            check=True,
        )
        _wait_for_neo4j_healthy(docker_neo4j["neo4j_bolt"], timeout_s=60)

        # Post-recovery: data persists (backed by Docker volume).
        adapter3 = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            result_after = await adapter3.get_memory_item(mid)
            assert result_after is not None, "Data should persist across Docker stop/start"
            assert result_after["content"] == "pre-outage content"
        finally:
            await adapter3.close()


# ---------------------------------------------------------------------------
# Persistence restart tests
# ---------------------------------------------------------------------------


class TestPersistenceRestart:
    """Test constraints survive adapter reinstantiation."""

    @pytest.mark.asyncio
    async def test_constraints_survive_reinstantiation(self, neo4j_uri, neo4j_user, neo4j_password):
        adapter1 = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        await adapter1.create_constraints()
        mid = str(uuid4())
        await adapter1.project_memory_item(
            memory_id=mid,
            content="persistent content",
            status="active",
            created_at=None,
            updated_at=None,
            expires_at=None,
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
            metadata={},
            idempotency_key=f"persist-{mid}",
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            evidence_refs=[],
            provenance=None,
        )
        await adapter1.close()

        # New adapter instance connects to same Neo4j.
        adapter2 = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            await adapter2.create_constraints()  # Idempotent
            result = await adapter2.get_memory_item(mid)
            assert result is not None
            assert result["content"] == "persistent content"
        finally:
            await adapter2.close()


# ---------------------------------------------------------------------------
# PostgreSQL readability during outage
# ---------------------------------------------------------------------------


class TestPostgresReadabilityDuringOutage:
    """PostgreSQL must remain readable even when Neo4j is unavailable."""

    @pytest.mark.asyncio
    async def test_postgres_readable(self, session_factory):
        async with session_factory() as session:
            item = _make_memory_item(content="pg readable test")
            row = MemoryItemRow(
                id=item.id,
                content=item.content,
                status=item.status.value,
                created_at=item.created_at,
                updated_at=item.updated_at,
                metadata_=item.metadata,
            )
            session.add(row)
            await session.commit()

            stmt = select(MemoryItemRow).where(MemoryItemRow.id == item.id)
            result = await session.execute(stmt)
            fetched = result.scalar_one_or_none()
            assert fetched is not None
            assert fetched.content == item.content


# ---------------------------------------------------------------------------
# Composition preflight no-claim tests
# ---------------------------------------------------------------------------


class TestCompositionRoot:
    """Test composition root preflight before any outbox claim."""

    @pytest.mark.asyncio
    async def test_preflight_before_any_claim(self, neo4j_uri, neo4j_user, neo4j_password):
        handler = await Neo4jProjectionHandler.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            assert handler.adapter is not None
            ping_result = await handler.preflight_ping()
            assert "name" in ping_result or "versions" in ping_result
        finally:
            await handler.close()

    @pytest.mark.asyncio
    async def test_preflight_bad_uri_fails(self):
        with pytest.raises(Exception):
            await Neo4jProjectionHandler.create(
                uri="bolt://127.0.0.1:1",
                user="neo4j",
                password="wrong",
                preflight_timeout_s=2.0,
            )


# ---------------------------------------------------------------------------
# Full pipeline delivery: completed + dead_letter
# ---------------------------------------------------------------------------


class TestFullPipelineDelivery:
    """Full worker pipeline through real PostgreSQL outbox store."""

    @pytest.mark.asyncio
    async def test_successful_delivery_reaches_completed(
        self, session_factory, neo4j_uri, neo4j_user, neo4j_password
    ):
        item = _make_memory_item(content="pipeline completed test")

        async with session_factory() as session:
            await enqueue_remembered_events(session, memory_item=item, targets=["neo4j"])
            await session.commit()

        handler = await Neo4jProjectionHandler.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            store = ProjectionOutboxStore(session_factory, worker_identity="test-worker-completed")
            worker = ProjectionWorker(
                store=store,
                registry=ProjectionHandlerRegistry({"neo4j": handler}),
                worker_identity="test-worker-completed",
                batch_size=1,
                max_concurrency=1,
                lease_duration=timedelta(seconds=30),
                delivery_timeout=timedelta(seconds=10),
                finalization_margin=timedelta(seconds=5),
                retry_policy=ExponentialBackoffPolicy(base_delay_seconds=1.0),
                target_filter={"neo4j"},
            )

            result = await worker.run_once()
            assert result.completed == 1
            assert result.dead_lettered == 0
            assert result.retried == 0

            async with session_factory() as session:
                rows = await session.execute(select(OutboxRow))
                rows = rows.scalars().all()
                assert len(rows) == 1
                assert rows[0].state == "completed"
                assert rows[0].processed_at is not None
        finally:
            await handler.close()

    @pytest.mark.asyncio
    async def test_conflicting_idempotency_delivery_reaches_dead_letter(
        self, session_factory, neo4j_uri, neo4j_user, neo4j_password
    ):
        item = _make_memory_item(content="pipeline dead-letter conflict test")

        async with session_factory() as session:
            await enqueue_remembered_events(session, memory_item=item, targets=["neo4j"])
            await session.commit()

        handler = await Neo4jProjectionHandler.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            # Seed Neo4j with same memory_id but different idempotency_key.
            seed_key = f"legacy-{uuid4()}"
            await handler.adapter.project_memory_item(
                memory_id=str(item.id),
                content=item.content,
                status=item.status.value,
                created_at=None,
                updated_at=None,
                expires_at=None,
                project_id=None,
                session_id=None,
                agent_id=None,
                knowledge_base_id=None,
                metadata={},
                idempotency_key=seed_key,
                event_type="memory.remembered",
                schema_version=1,
                projection_target="neo4j",
                evidence_refs=[],
                provenance=None,
            )

            store = ProjectionOutboxStore(session_factory, worker_identity="test-worker-conflict")
            worker = ProjectionWorker(
                store=store,
                registry=ProjectionHandlerRegistry({"neo4j": handler}),
                worker_identity="test-worker-conflict",
                batch_size=1,
                max_concurrency=1,
                lease_duration=timedelta(seconds=30),
                delivery_timeout=timedelta(seconds=10),
                finalization_margin=timedelta(seconds=5),
                retry_policy=ExponentialBackoffPolicy(base_delay_seconds=1.0),
                target_filter={"neo4j"},
            )

            result = await worker.run_once()
            assert result.dead_lettered == 1
            assert result.completed == 0

            async with session_factory() as session:
                rows = await session.execute(select(OutboxRow))
                rows = rows.scalars().all()
                assert len(rows) == 1
                dl_row = rows[0]
                assert dl_row.state == "dead_letter"
                assert dl_row.last_error == "permanent_projection_error"
        finally:
            await handler.close()


# ---------------------------------------------------------------------------
# Preflight with real pending outbox row
# ---------------------------------------------------------------------------


class TestPreflightPendingRow:
    """Composition root preflight with a real pending outbox row."""

    @pytest.mark.asyncio
    async def test_unreachable_neo4j_preserves_pending_row(self, session_factory):
        item = _make_memory_item(content="preflight pending row test")

        async with session_factory() as session:
            await enqueue_remembered_events(session, memory_item=item, targets=["neo4j"])
            await session.commit()

        async with session_factory() as session:
            rows_before = (await session.execute(select(OutboxRow))).scalars().all()
            assert len(rows_before) == 1
            assert rows_before[0].state == "pending"
            assert rows_before[0].attempt_count == 0
            assert rows_before[0].lease_owner is None

        # Attempt the full composition root with an unreachable Neo4j endpoint.
        with pytest.raises(Exception):
            await compose_and_run_neo4j_worker(
                session_factory=session_factory,
                neo4j_uri="bolt://127.0.0.1:1",
                neo4j_user="neo4j",
                neo4j_password="wrong",
                worker_identity="test-preflight-worker",
                preflight_timeout_s=2.0,
            )

        # The pending row must be completely unchanged.
        async with session_factory() as session:
            rows_after = (await session.execute(select(OutboxRow))).scalars().all()
            assert len(rows_after) == 1
            row = rows_after[0]
            assert row.state == "pending"
            assert row.attempt_count == 0
            assert row.lease_owner is None
            assert row.lease_expires_at is None
            assert row.processed_at is None
            assert row.last_error is None


# ---------------------------------------------------------------------------
# Worker processes a real outbox row
# ---------------------------------------------------------------------------


class TestWorkerOutboxDelivery:
    """Worker must process a real outbox row end-to-end."""

    @pytest.mark.asyncio
    async def test_worker_processes_real_outbox_row(
        self, session_factory, neo4j_uri, neo4j_user, neo4j_password
    ):
        item = _make_memory_item(content="worker outbox delivery test")
        payload = build_event_payload(memory_item=item, projection_target="neo4j")
        agg_id = item.id
        idem_key = build_idempotency_key(agg_id, "neo4j")

        event = ProjectionEvent(
            outbox_id=uuid4(),
            aggregate_id=agg_id,
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            idempotency_key=idem_key,
            payload=payload,
            attempt_count=1,
            max_attempts=5,
        )

        handler = await Neo4jProjectionHandler.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            await handler(event)
            result = await handler.adapter.get_memory_item(str(agg_id))
            assert result is not None
            assert result["content"] == item.content
            assert result["idempotency_key"] == idem_key
        finally:
            await handler.close()


# ---------------------------------------------------------------------------
# PostgreSQL read during real Neo4j outage
# ---------------------------------------------------------------------------


class TestPostgresReadDuringRealOutage:
    """PostgreSQL must remain readable during a genuine Neo4j Docker outage."""

    @pytest.mark.asyncio
    async def test_postgres_readable_during_outage(
        self,
        session_factory,
        neo4j_uri,
        neo4j_user,
        neo4j_password,
        docker_neo4j,
    ):
        from tests.integration.p1_07_neo4j.conftest import _wait_for_neo4j_healthy

        content_marker = "pg-readable-during-outage"

        # 1. Write data to BOTH PostgreSQL and Neo4j.
        item = _make_memory_item(content=content_marker)
        async with session_factory() as session:
            row = MemoryItemRow(
                id=item.id,
                content=item.content,
                status=item.status.value,
                created_at=item.created_at,
                updated_at=item.updated_at,
                metadata_=item.metadata,
            )
            session.add(row)
            await session.commit()

        adapter = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            await adapter.create_constraints()
            await adapter.project_memory_item(
                memory_id=str(item.id),
                content=content_marker,
                status=item.status.value,
                created_at=None,
                updated_at=None,
                expires_at=None,
                project_id=None,
                session_id=None,
                agent_id=None,
                knowledge_base_id=None,
                metadata={},
                idempotency_key=f"outage-test-{uuid4()}",
                event_type="memory.remembered",
                schema_version=1,
                projection_target="neo4j",
                evidence_refs=[],
                provenance=None,
            )
        finally:
            await adapter.close()

        # 2. Verify PostgreSQL readability BEFORE outage.
        async with session_factory() as session:
            result = await session.execute(select(MemoryItemRow).where(MemoryItemRow.id == item.id))
            fetched = result.scalar_one_or_none()
            assert fetched is not None
            assert fetched.content == content_marker

        # 3. Stop Neo4j container via Docker Compose.
        subprocess.run(
            [
                docker_neo4j["docker_exe"],
                "compose",
                "-p",
                docker_neo4j["compose_project"],
                "-f",
                docker_neo4j["compose_file"],
                "stop",
                "neo4j",
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )

        # 4. Perform a REAL PostgreSQL read WHILE Neo4j is unavailable.
        try:
            async with session_factory() as session:
                result = await session.execute(
                    select(MemoryItemRow).where(MemoryItemRow.id == item.id)
                )
                fetched_during_outage = result.scalar_one_or_none()
                assert fetched_during_outage is not None, (
                    "PostgreSQL must remain readable during Neo4j outage"
                )
                assert fetched_during_outage.content == content_marker
        finally:
            # 5. Restart Neo4j container.
            subprocess.run(
                [
                    docker_neo4j["docker_exe"],
                    "compose",
                    "-p",
                    docker_neo4j["compose_project"],
                    "-f",
                    docker_neo4j["compose_file"],
                    "up",
                    "-d",
                    "--wait",
                    "neo4j",
                ],
                capture_output=True,
                timeout=120,
                check=True,
            )

        _wait_for_neo4j_healthy(docker_neo4j["neo4j_bolt"], timeout_s=60)


# ---------------------------------------------------------------------------
# Claimed-row outage recovery (real worker pipeline)
# ---------------------------------------------------------------------------


class TestClaimedRowOutageRecovery:
    """End-to-end: claim -> Neo4j outage -> retryable pending ->
    recovery -> completed.

    Pins the P1-07 retry-9 requirement that the worker pipeline
    survives a real Neo4j loss mid-delivery: the claimed event must
    remain retryable in PostgreSQL (not dead-lettered, not lost), and
    a subsequent worker run after Neo4j recovery must complete it.
    """

    @pytest.mark.asyncio
    async def test_claim_outage_retryable_then_completed(
        self,
        session_factory,
        neo4j_uri,
        neo4j_user,
        neo4j_password,
        docker_neo4j,
    ):
        from tests.integration.p1_07_neo4j.conftest import _wait_for_neo4j_healthy

        item = _make_memory_item(content="claimed-row outage recovery")

        # 1. Enqueue the event to the outbox.
        async with session_factory() as session:
            await enqueue_remembered_events(session, memory_item=item, targets=["neo4j"])
            await session.commit()

        # 2. Stop Neo4j BEFORE the worker claims the row.
        subprocess.run(
            [
                docker_neo4j["docker_exe"],
                "compose",
                "-p",
                docker_neo4j["compose_project"],
                "-f",
                docker_neo4j["compose_file"],
                "stop",
                "neo4j",
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )

        # 3. Build a handler that bypasses preflight (Neo4j is down).
        #    The adapter will attempt real connections and fail with a
        #    retryable driver error during project_memory_item.
        from neo4j import AsyncGraphDatabase

        dead_driver = AsyncGraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
        dead_adapter = Neo4jProjectionAdapter(dead_driver, owns_driver=True)
        dead_handler = Neo4jProjectionHandler(dead_adapter)

        try:
            store = ProjectionOutboxStore(
                session_factory, worker_identity="test-outage-recovery-worker"
            )
            worker = ProjectionWorker(
                store=store,
                registry=ProjectionHandlerRegistry({"neo4j": dead_handler}),
                worker_identity="test-outage-recovery-worker",
                batch_size=1,
                max_concurrency=1,
                lease_duration=timedelta(seconds=30),
                delivery_timeout=timedelta(seconds=10),
                finalization_margin=timedelta(seconds=5),
                retry_policy=ExponentialBackoffPolicy(base_delay_seconds=0.001),
                target_filter={"neo4j"},
            )

            # 4. Run the worker.  Handler must fail with a retryable
            #    error (ConnectionError/ServiceUnavailable).
            result_during_outage = await worker.run_once()
            assert result_during_outage.claimed == 1
            assert result_during_outage.completed == 0
            assert result_during_outage.dead_lettered == 0
            assert (
                result_during_outage.retried == 1 or result_during_outage.state_write_errors >= 1
            ), "Expected a retryable failure disposition while Neo4j was down"
        finally:
            await dead_driver.close()

        # 5. Verify PostgreSQL records the row as pending/retryable
        #    (NOT dead_letter, NOT completed).
        async with session_factory() as session:
            rows = (await session.execute(select(OutboxRow))).scalars().all()
            assert len(rows) == 1
            row = rows[0]
            assert row.state in ("pending", "processing"), (
                f"Expected retryable state, got {row.state!r}"
            )
            assert row.attempt_count >= 1
            assert row.last_error is not None

        # 6. Restart Neo4j and wait for health.
        subprocess.run(
            [
                docker_neo4j["docker_exe"],
                "compose",
                "-p",
                docker_neo4j["compose_project"],
                "-f",
                docker_neo4j["compose_file"],
                "up",
                "-d",
                "--wait",
                "neo4j",
            ],
            capture_output=True,
            timeout=120,
            check=True,
        )
        _wait_for_neo4j_healthy(docker_neo4j["neo4j_bolt"], timeout_s=60)

        # 7. Build a healthy handler and run the worker again.
        healthy_handler = await Neo4jProjectionHandler.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            store2 = ProjectionOutboxStore(
                session_factory,
                worker_identity="test-outage-recovery-worker-2",
            )
            worker2 = ProjectionWorker(
                store=store2,
                registry=ProjectionHandlerRegistry({"neo4j": healthy_handler}),
                worker_identity="test-outage-recovery-worker-2",
                batch_size=1,
                max_concurrency=1,
                lease_duration=timedelta(seconds=30),
                delivery_timeout=timedelta(seconds=10),
                finalization_margin=timedelta(seconds=5),
                retry_policy=ExponentialBackoffPolicy(base_delay_seconds=0.001),
                target_filter={"neo4j"},
            )

            result_after_recovery = await worker2.run_once()
            assert result_after_recovery.completed == 1, "Event must complete after Neo4j recovery"
            assert result_after_recovery.dead_lettered == 0

            # 8. Final state: completed.
            async with session_factory() as session:
                rows = (await session.execute(select(OutboxRow))).scalars().all()
                assert len(rows) == 1
                assert rows[0].state == "completed"
                assert rows[0].processed_at is not None

            # 9. Data reached Neo4j.
            graph_result = await healthy_handler.adapter.get_memory_item(str(item.id))
            assert graph_result is not None
            assert graph_result["content"] == "claimed-row outage recovery"
        finally:
            await healthy_handler.close()


# ---------------------------------------------------------------------------
# Concurrent idempotency-key atomicity (regression)
# ---------------------------------------------------------------------------


class TestConcurrentIdempotencyAtomicity:
    """Regression: two concurrent projections for the same ``memory_id``
    with DIFFERENT idempotency keys cannot both succeed.

    Exactly one writer wins; the other must surface
    ``IdempotencyConflictError`` (either via the in-transaction conflict
    check or via the unique-constraint safety net mapped by the
    adapter).  The winning writer's idempotency_key must be the one
    stored in the graph.

    Boundary note: the two writers use separate ``AsyncDriver`` sessions
    (real Neo4j connections) launched by ``asyncio.gather``.  The Neo4j
    server serialises conflicting constraint writes, so the outcome is
    deterministic at the graph layer regardless of event-loop scheduling.
    """

    @pytest.mark.asyncio
    async def test_concurrent_different_keys_exactly_one_wins(
        self, neo4j_uri, neo4j_user, neo4j_password
    ):
        adapter_a = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        adapter_b = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            await adapter_a.create_constraints()
            mid = str(uuid4())
            key_a = f"alpha-concurrent-{mid}"
            key_b = f"beta-concurrent-{mid}"

            async def _write_a():
                await adapter_a.project_memory_item(
                    memory_id=mid,
                    content="writer A",
                    status="active",
                    created_at=None,
                    updated_at=None,
                    expires_at=None,
                    project_id=None,
                    session_id=None,
                    agent_id=None,
                    knowledge_base_id=None,
                    metadata={"writer": "A"},
                    idempotency_key=key_a,
                    event_type="memory.remembered",
                    schema_version=1,
                    projection_target="neo4j",
                    evidence_refs=[],
                    provenance=None,
                )

            async def _write_b():
                await adapter_b.project_memory_item(
                    memory_id=mid,
                    content="writer B",
                    status="active",
                    created_at=None,
                    updated_at=None,
                    expires_at=None,
                    project_id=None,
                    session_id=None,
                    agent_id=None,
                    knowledge_base_id=None,
                    metadata={"writer": "B"},
                    idempotency_key=key_b,
                    event_type="memory.remembered",
                    schema_version=1,
                    projection_target="neo4j",
                    evidence_refs=[],
                    provenance=None,
                )

            results = await asyncio.gather(_write_a(), _write_b(), return_exceptions=True)

            successes = [r for r in results if r is None]
            conflicts = [r for r in results if isinstance(r, IdempotencyConflictError)]
            other_errors = [
                r for r in results if r is not None and not isinstance(r, IdempotencyConflictError)
            ]

            assert len(successes) == 1, (
                f"Exactly one writer must succeed; got {len(successes)} successes. "
                f"Other errors: {other_errors}"
            )
            assert len(conflicts) == 1, (
                f"Exactly one writer must see IdempotencyConflictError; "
                f"got {len(conflicts)} conflicts."
            )
            assert len(other_errors) == 0, f"Unexpected non-conflict errors: {other_errors}"

            # The stored idempotency_key must be one of the two we sent,
            # and the stored content must match the winning writer.
            stored = await adapter_a.get_memory_item(mid)
            assert stored is not None
            assert stored["idempotency_key"] in (key_a, key_b)
            if stored["idempotency_key"] == key_a:
                assert stored["content"] == "writer A"
                assert stored["metadata"] == {"writer": "A"}
            else:
                assert stored["content"] == "writer B"
                assert stored["metadata"] == {"writer": "B"}
        finally:
            await adapter_a.close()
            await adapter_b.close()


class TestConcurrentSameKeyReplay:
    """Regression: two concurrent projections for the same ``memory_id``
    with the SAME idempotency_key must both succeed (idempotent replay).

    Exactly one physical CREATE wins the unique-constraint race; the
    losing writer's ``_resolve_after_constraint_collision`` reads the
    committed node, confirms the stored key matches, and returns
    silently.  Neither delivery may raise -- both are valid replays of
    the same deterministic outbox event.

    Pins the P1-07 retry-14 fix: same-key concurrent replay must not
    be misclassified as a permanent conflict and sent to dead-letter.
    """

    @pytest.mark.asyncio
    async def test_concurrent_same_key_both_succeed(self, neo4j_uri, neo4j_user, neo4j_password):
        adapter_a = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        adapter_b = await Neo4jProjectionAdapter.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )
        try:
            await adapter_a.create_constraints()
            mid = str(uuid4())
            shared_key = f"same-key-replay-{mid}"

            async def _write():
                await adapter_a.project_memory_item(
                    memory_id=mid,
                    content="same-key content",
                    status="active",
                    created_at=None,
                    updated_at=None,
                    expires_at=None,
                    project_id=None,
                    session_id=None,
                    agent_id=None,
                    knowledge_base_id=None,
                    metadata={"writer": "shared"},
                    idempotency_key=shared_key,
                    event_type="memory.remembered",
                    schema_version=1,
                    projection_target="neo4j",
                    evidence_refs=[],
                    provenance=None,
                )

            async def _write_b():
                await adapter_b.project_memory_item(
                    memory_id=mid,
                    content="same-key content",
                    status="active",
                    created_at=None,
                    updated_at=None,
                    expires_at=None,
                    project_id=None,
                    session_id=None,
                    agent_id=None,
                    knowledge_base_id=None,
                    metadata={"writer": "shared"},
                    idempotency_key=shared_key,
                    event_type="memory.remembered",
                    schema_version=1,
                    projection_target="neo4j",
                    evidence_refs=[],
                    provenance=None,
                )

            results = await asyncio.gather(
                _write(),
                _write_b(),
                return_exceptions=True,
            )

            errors = [r for r in results if r is not None]
            assert not errors, f"Same-key concurrent replay must not raise; got errors: {errors}"

            # The stored node must have the shared key and content.
            stored = await adapter_a.get_memory_item(mid)
            assert stored is not None
            assert stored["idempotency_key"] == shared_key
            assert stored["content"] == "same-key content"
        finally:
            await adapter_a.close()
            await adapter_b.close()
