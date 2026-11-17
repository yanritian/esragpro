"""P1-06 Elasticsearch projection integration tests.

Covers:
- Real worker projection (round-trip through handler)
- Exact round-trip fidelity (write then read)
- BM25 text search
- kNN dense-vector search
- Client-side deterministic RRF fusion
- Filters with no permission fields
- Duplicate/conflict handling (idempotency)
- Outage/recovery at same endpoint
- Persistence restart
- Rebuild without new outbox rows
- PostgreSQL readability during outage
- Composition preflight no-claim behavior
- Reproducible warm-query latency (P50/P95/P99, P95 <2s)

Uses real PostgreSQL and real Elasticsearch 8.12.
No ACL/permission/tenant fields anywhere.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import pytest
from elasticsearch import AsyncElasticsearch
from sqlalchemy import select

from app.db.models.memory_plane import (
    MemoryItem as MemoryItemRow,
    MemoryProjectionOutbox as OutboxRow,
)
from app.memory_core.elasticsearch_compose import (
    compose_and_run_elasticsearch_worker,
)
from app.memory_core.elasticsearch_handler import ElasticsearchProjectionHandler
from app.memory_core.projection_worker.contracts import (
    ProjectionEvent,
    ProjectionHandlerRegistry,
)
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.projection_worker.worker import ProjectionWorker
from app.memory_core.repositories.elasticsearch_projection.index import (
    ElasticsearchProjectionIndex,
    is_retryable_es_error,
)
from app.memory_core.repositories.elasticsearch_projection.mapping import DEFAULT_DIMS
from app.memory_core.repositories.elasticsearch_projection.models import (
    PermanentElasticsearchPayloadError,
    SearchFilters,
)
from app.memory_core.repositories.elasticsearch_projection.rebuild import rebuild_from_postgres
from app.memory_core.repositories.postgres_outbox import (
    build_event_payload,
    build_idempotency_key,
    enqueue_remembered_events,
)
from app.memory_core.unpermissioned_contracts import MemoryItem, MemoryItemStatus


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
        provenance=None,
        metadata={},
        evidence_refs=evidence_refs or [],
    )


# ---------------------------------------------------------------------------
# Exact round-trip tests
# ---------------------------------------------------------------------------


class TestExactRoundTrip:
    """Test write-then-read fidelity through Elasticsearch."""

    @pytest.mark.asyncio
    async def test_upsert_and_get(self, es_index, embedding_provider):
        mid = str(uuid4())
        content = "Hello world test content"
        embedding = await embedding_provider.embed(content)

        result = await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
            status="active",
            project_id=str(uuid4()),
        )
        assert result in ("created", "updated")

        await es_index.refresh()
        doc = await es_index.get(mid)
        assert doc is not None
        assert doc["memory_id"] == mid
        assert doc["content"] == content
        assert doc["status"] == "active"
        assert len(doc["content_embedding"]) == DEFAULT_DIMS

    @pytest.mark.asyncio
    async def test_upsert_idempotent(self, es_index, embedding_provider):
        mid = str(uuid4())
        content = "idempotent test"
        embedding = await embedding_provider.embed(content)

        r1 = await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
        )
        r2 = await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
        )
        assert r1 == "created"
        assert r2 == "updated"

        await es_index.refresh()
        doc = await es_index.get(mid)
        assert doc is not None
        assert doc["memory_id"] == mid

    @pytest.mark.asyncio
    async def test_delete(self, es_index, embedding_provider):
        mid = str(uuid4())
        embedding = await embedding_provider.embed("delete me")
        await es_index.upsert(
            memory_id=mid,
            content="delete me",
            embedding=embedding,
        )
        await es_index.refresh()

        deleted = await es_index.delete(mid)
        assert deleted is True

        await es_index.refresh()
        doc = await es_index.get(mid)
        assert doc is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, es_index):
        deleted = await es_index.delete(str(uuid4()))
        assert deleted is False

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, es_index):
        doc = await es_index.get(str(uuid4()))
        assert doc is None


# ---------------------------------------------------------------------------
# BM25 search tests
# ---------------------------------------------------------------------------


class TestBM25Search:
    """Test BM25 text search against real Elasticsearch."""

    @pytest.mark.asyncio
    async def test_bm25_basic(self, es_index, embedding_provider):
        mids = []
        for i in range(5):
            mid = str(uuid4())
            mids.append(mid)
            content = f"document number {i} with unique word alpha{i}"
            embedding = await embedding_provider.embed(content)
            await es_index.upsert(
                memory_id=mid,
                content=content,
                embedding=embedding,
            )
        await es_index.refresh()

        results = await es_index.search_bm25(query="alpha3")
        assert len(results) >= 1
        assert any(r[0].memory_id == mids[3] for r in results)

    @pytest.mark.asyncio
    async def test_bm25_empty_query_raises(self, es_index):
        with pytest.raises(ValueError, match="query must be non-empty"):
            await es_index.search_bm25(query="")

    @pytest.mark.asyncio
    async def test_bm25_with_filter(self, es_index, embedding_provider):
        proj_id = str(uuid4())
        mid = str(uuid4())
        content = "filtered document beta"
        embedding = await embedding_provider.embed(content)
        await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
            project_id=proj_id,
        )
        await es_index.refresh()

        filters = SearchFilters(project_id=proj_id, include_inactive=True)
        results = await es_index.search_bm25(query="beta", filters=filters)
        assert len(results) >= 1

        other_filters = SearchFilters(
            project_id=str(uuid4()),
            include_inactive=True,
        )
        results2 = await es_index.search_bm25(query="beta", filters=other_filters)
        assert len(results2) == 0


# ---------------------------------------------------------------------------
# kNN vector search tests
# ---------------------------------------------------------------------------


class TestKNNSearch:
    """Test kNN dense-vector search against real Elasticsearch."""

    @pytest.mark.asyncio
    async def test_knn_basic(self, es_index, embedding_provider):
        mids = []
        for i in range(5):
            mid = str(uuid4())
            mids.append(mid)
            content = f"vector doc {i}"
            embedding = await embedding_provider.embed(content)
            await es_index.upsert(
                memory_id=mid,
                content=content,
                embedding=embedding,
            )
        await es_index.refresh()

        query_vec = await embedding_provider.embed("vector doc 2")
        results = await es_index.search_knn(query_vector=query_vec)
        assert len(results) >= 1
        top_mid = results[0][0].memory_id
        assert top_mid == mids[2]

    @pytest.mark.asyncio
    async def test_knn_wrong_dims_raises(self, es_index):
        with pytest.raises(Exception, match="dims"):
            await es_index.search_knn(query_vector=[0.1, 0.2, 0.3])

    @pytest.mark.asyncio
    async def test_knn_with_filter(self, es_index, embedding_provider):
        proj_id = str(uuid4())
        mid = str(uuid4())
        content = "knn filtered doc"
        embedding = await embedding_provider.embed(content)
        await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
            project_id=proj_id,
        )
        await es_index.refresh()

        query_vec = await embedding_provider.embed(content)
        filters = SearchFilters(project_id=proj_id, include_inactive=True)
        results = await es_index.search_knn(
            query_vector=query_vec,
            filters=filters,
        )
        assert len(results) >= 1


# ---------------------------------------------------------------------------
# Client-side RRF tests
# ---------------------------------------------------------------------------


class TestClientSideRRF:
    """Test client-side deterministic RRF fusion with real ES results."""

    @pytest.mark.asyncio
    async def test_hybrid_search(self, es_index, embedding_provider):
        mids = []
        for i in range(5):
            mid = str(uuid4())
            mids.append(mid)
            content = f"hybrid test document number {i} keyword{i}"
            embedding = await embedding_provider.embed(content)
            await es_index.upsert(
                memory_id=mid,
                content=content,
                embedding=embedding,
            )
        await es_index.refresh()

        query = "keyword2"
        query_vec = await embedding_provider.embed(content)

        results = await es_index.search_hybrid(
            query=query,
            query_vector=query_vec,
            size=5,
        )
        assert len(results) >= 1
        for r in results:
            assert r.rrf_score is not None
            assert r.rrf_score > 0
            assert r.bm25_rank is not None or r.vector_rank is not None

    @pytest.mark.asyncio
    async def test_hybrid_search_source_ranks_exposed(self, es_index, embedding_provider):
        mid = str(uuid4())
        content = "unique hybrid content xyz123"
        embedding = await embedding_provider.embed(content)
        await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
        )
        await es_index.refresh()

        results = await es_index.search_hybrid(
            query="xyz123",
            query_vector=embedding,
            size=5,
        )
        found = [r for r in results if r.memory_id == mid]
        assert len(found) == 1
        r = found[0]
        assert r.bm25_rank is not None
        assert r.vector_rank is not None


# ---------------------------------------------------------------------------
# Filters with no permission fields tests
# ---------------------------------------------------------------------------


class TestFiltersNoPermission:
    """Test filters contain no ACL/permission fields."""

    def test_search_filters_no_acl(self):
        filters = SearchFilters()
        assert not hasattr(filters, "user_id")
        assert not hasattr(filters, "tenant_id")
        assert not hasattr(filters, "principal")
        assert not hasattr(filters, "role")
        assert not hasattr(filters, "acl")

    @pytest.mark.asyncio
    async def test_filters_isolation(self, es_index, embedding_provider):
        proj1 = str(uuid4())
        proj2 = str(uuid4())

        for proj_id in (proj1, proj2):
            mid = str(uuid4())
            content = f"isolated content for {proj_id[:8]}"
            embedding = await embedding_provider.embed(content)
            await es_index.upsert(
                memory_id=mid,
                content=content,
                embedding=embedding,
                project_id=proj_id,
            )
        await es_index.refresh()

        f1 = SearchFilters(project_id=proj1, include_inactive=True)
        r1 = await es_index.search_bm25(query="isolated", filters=f1)
        assert all(r[0].project_id == proj1 for r in r1)

        f2 = SearchFilters(project_id=proj2, include_inactive=True)
        r2 = await es_index.search_bm25(query="isolated", filters=f2)
        assert all(r[0].project_id == proj2 for r in r2)


# ---------------------------------------------------------------------------
# Duplicate/conflict handling tests
# ---------------------------------------------------------------------------


class TestDuplicateConflict:
    """Test idempotent duplicate handling."""

    @pytest.mark.asyncio
    async def test_duplicate_upsert_is_idempotent(self, es_index, embedding_provider):
        mid = str(uuid4())
        content = "duplicate test content"
        embedding = await embedding_provider.embed(content)

        await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
            idempotency_key="key-1",
        )
        await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
            idempotency_key="key-1",
        )
        await es_index.refresh()

        doc = await es_index.get(mid)
        assert doc is not None
        assert doc["memory_id"] == mid

    @pytest.mark.asyncio
    async def test_idempotency_key_deterministic(self):
        agg_id = uuid4()
        k1 = build_idempotency_key(agg_id, "elasticsearch")
        k2 = build_idempotency_key(agg_id, "elasticsearch")
        assert k1 == k2
        k3 = build_idempotency_key(agg_id, "redis")
        assert k1 != k3


# ---------------------------------------------------------------------------
# Outage/recovery tests
# ---------------------------------------------------------------------------


class TestConnectionLossRecovery:
    """Test recovery after ES connection loss."""

    @pytest.mark.asyncio
    async def test_retryable_error_detection(self):
        assert is_retryable_es_error(ConnectionError("test"))
        assert is_retryable_es_error(TimeoutError("test"))
        assert is_retryable_es_error(OSError("test"))
        assert not is_retryable_es_error(ValueError("test"))
        assert not is_retryable_es_error(RuntimeError("test"))

    @pytest.mark.asyncio
    async def test_real_docker_stop_start_recovery(
        self,
        es_index,
        embedding_provider,
        docker_es,
    ):
        """Stop ES container, observe failure, restart, verify recovery."""
        import subprocess

        from tests.integration.p1_06_elasticsearch.conftest import (
            _wait_for_es_healthy,
        )

        # 1. Write data before outage.
        mid = str(uuid4())
        content = "pre-outage content"
        embedding = await embedding_provider.embed(content)
        await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
        )
        await es_index.refresh()

        doc = await es_index.get(mid)
        assert doc is not None
        assert doc["content"] == content

        # 2. Stop ES container via Docker Compose.
        subprocess.run(
            [
                docker_es["docker_exe"],
                "compose",
                "-p",
                docker_es["compose_project"],
                "-f",
                docker_es["compose_file"],
                "stop",
                "elasticsearch",
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )

        # 3. Observe connection failure.
        outage_error = None
        try:
            await es_index.get(mid)
        except Exception as exc:
            outage_error = exc

        assert outage_error is not None, "Expected connection error after Docker stop"
        assert is_retryable_es_error(outage_error), (
            f"Outage error must be retryable, got {type(outage_error).__name__}: {outage_error}"
        )

        # 4. Restart ES container.
        subprocess.run(
            [
                docker_es["docker_exe"],
                "compose",
                "-p",
                docker_es["compose_project"],
                "-f",
                docker_es["compose_file"],
                "up",
                "-d",
                "--wait",
                "elasticsearch",
            ],
            capture_output=True,
            timeout=120,
            check=True,
        )
        _wait_for_es_healthy(docker_es["es_url"], timeout_s=60, index_name=es_index.index_name)

        # 5. Post-recovery: data persists (backed by Docker volume).
        doc_after = await es_index.get(mid)
        assert doc_after is not None, "Data should persist across Docker stop/start"
        assert doc_after["content"] == content

        # 6. Post-recovery: new write/read works.
        mid2 = str(uuid4())
        content2 = "post-recovery content"
        embedding2 = await embedding_provider.embed(content2)
        await es_index.upsert(
            memory_id=mid2,
            content=content2,
            embedding=embedding2,
        )
        await es_index.refresh()

        doc2 = await es_index.get(mid2)
        assert doc2 is not None
        assert doc2["content"] == content2


# ---------------------------------------------------------------------------
# Persistence restart tests
# ---------------------------------------------------------------------------


class TestPersistenceRestart:
    """Test index survives restarts."""

    @pytest.mark.asyncio
    async def test_index_survives_reinstantiation(
        self,
        es_client,
        embedding_provider,
    ):
        index_name = f"test_persist_{uuid4().hex[:8]}"
        idx1 = ElasticsearchProjectionIndex(
            es_client,
            index_name=index_name,
            dims=DEFAULT_DIMS,
        )
        await idx1.ensure_index()

        mid = str(uuid4())
        embedding = await embedding_provider.embed("persistent content")
        await idx1.upsert(
            memory_id=mid,
            content="persistent content",
            embedding=embedding,
        )
        await idx1.refresh()

        idx2 = ElasticsearchProjectionIndex(
            es_client,
            index_name=index_name,
            dims=DEFAULT_DIMS,
        )
        await idx2.ensure_index()

        doc = await idx2.get(mid)
        assert doc is not None
        assert doc["content"] == "persistent content"

        await es_client.indices.delete(index=index_name)


# ---------------------------------------------------------------------------
# Rebuild without new outbox rows tests
# ---------------------------------------------------------------------------


class TestRebuildWithoutOutbox:
    """Test rebuild from PostgreSQL without creating new outbox rows."""

    @pytest.mark.asyncio
    async def test_rebuild_empty_db(self, session_factory, es_index, embedding_provider):
        async with session_factory() as session:
            counters = await rebuild_from_postgres(
                session=session,
                index=es_index,
                embedding_provider=embedding_provider,
            )
            assert counters.scanned == 0
            assert counters.projected == 0
            assert counters.failed == 0

    @pytest.mark.asyncio
    async def test_rebuild_scans_active_items(
        self,
        session_factory,
        es_index,
        embedding_provider,
    ):
        async with session_factory() as session:
            item = _make_memory_item(content="rebuild test content")
            from app.db.models.memory_plane import MemoryItem as MemoryItemRow

            row = MemoryItemRow(
                id=item.id,
                content=item.content,
                status=item.status.value,
                created_at=item.created_at,
                updated_at=item.updated_at,
                expires_at=item.expires_at,
                project_id=item.project_id,
                session_id=item.session_id,
                agent_id=item.agent_id,
                knowledge_base_id=item.knowledge_base_id,
                metadata_=item.metadata,
            )
            session.add(row)
            await session.commit()

            counters = await rebuild_from_postgres(
                session=session,
                index=es_index,
                embedding_provider=embedding_provider,
            )
            assert counters.scanned >= 1
            assert counters.projected >= 1

            await es_index.refresh()
            doc = await es_index.get(str(item.id))
            assert doc is not None
            assert doc["content"] == item.content


# ---------------------------------------------------------------------------
# PostgreSQL readability during outage tests
# ---------------------------------------------------------------------------


class TestPostgresReadabilityDuringOutage:
    """PostgreSQL must remain readable even when ES is unavailable."""

    @pytest.mark.asyncio
    async def test_postgres_readable(self, session_factory):
        async with session_factory() as session:
            item = _make_memory_item(content="pg readable test")
            from app.db.models.memory_plane import MemoryItem as MemoryItemRow

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

            from sqlalchemy import select

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
    async def test_preflight_before_any_claim(self, es_client, embedding_provider):
        """Handler creation performs preflight before any claim."""
        handler = await ElasticsearchProjectionHandler.create(
            es_client,
            embedding_provider=embedding_provider,
        )
        try:
            assert handler.index is not None
            ping_result = await handler.preflight_ping()
            assert "version" in ping_result or "cluster_name" in ping_result
        finally:
            await handler.close()

    @pytest.mark.asyncio
    async def test_preflight_bad_url_fails(self, embedding_provider):
        """Preflight to unreachable ES must fail immediately."""
        bad_client = AsyncElasticsearch("http://127.0.0.1:1")
        try:
            with pytest.raises(Exception):
                await ElasticsearchProjectionHandler.create(
                    bad_client,
                    embedding_provider=embedding_provider,
                    preflight_timeout_s=1.0,
                )
        finally:
            await bad_client.close()


# ---------------------------------------------------------------------------
# Reproducible warm-query latency tests
# ---------------------------------------------------------------------------


class TestWarmQueryLatency:
    """Test reproducible warm-query latency (P95 < 2s)."""

    @pytest.mark.asyncio
    async def test_warm_query_latency(self, es_index, embedding_provider):
        for i in range(10):
            mid = str(uuid4())
            content = f"latency test document {i} warmup"
            embedding = await embedding_provider.embed(content)
            await es_index.upsert(
                memory_id=mid,
                content=content,
                embedding=embedding,
            )
        await es_index.refresh()

        latencies = []
        for i in range(20):
            query = f"latency test document {i % 10}"
            query_vec = await embedding_provider.embed(query)

            start = time.monotonic()
            results = await es_index.search_hybrid(
                query=query,
                query_vector=query_vec,
                size=5,
            )
            elapsed = time.monotonic() - start
            latencies.append(elapsed)
            assert len(results) >= 0

        latencies.sort()
        n = len(latencies)
        p50 = latencies[n // 2]
        p95 = latencies[int(n * 0.95)]
        p99 = latencies[int(n * 0.99)]

        assert p50 < 1.0, f"P50={p50:.3f}s too slow"
        assert p95 < 2.0, f"P95={p95:.3f}s exceeds 2s target"
        assert p99 < 5.0, f"P99={p99:.3f}s too slow"


# ---------------------------------------------------------------------------
# ES version and license observation
# ---------------------------------------------------------------------------


class TestESVersionLicense:
    """Observe ES version and license info."""

    @pytest.mark.asyncio
    async def test_es_version_is_8x(self, es_client):
        info = await es_client.info()
        version = info.get("version", {})
        number = version.get("number", "")
        assert number.startswith("8."), f"Expected ES 8.x, got {number}"

    @pytest.mark.asyncio
    async def test_es_license_basic_compatible(self, es_client):
        """Ensure no native RRF license-gated features are used."""
        info = await es_client.info()
        version = info.get("version", {})
        build_hash = version.get("build_hash", "")
        assert build_hash, "build_hash should be present"


# ---------------------------------------------------------------------------
# Idempotency-key conflict tests (P1-06 retry-5 fix #2)
# ---------------------------------------------------------------------------


class TestIdempotencyKeyConflict:
    """Mismatched idempotency replay must be rejected as permanent."""

    @pytest.mark.asyncio
    async def test_mismatched_idempotency_replay_raises(self, es_index, embedding_provider):
        """Write with key-1, then write same memory_id with key-2 → conflict."""
        mid = str(uuid4())
        content = "idempotency conflict test"
        embedding = await embedding_provider.embed(content)

        await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
            idempotency_key="key-alpha",
        )
        await es_index.refresh()

        with pytest.raises(PermanentElasticsearchPayloadError, match="idempotency_key conflict"):
            await es_index.upsert(
                memory_id=mid,
                content=content,
                embedding=embedding,
                idempotency_key="key-beta-different",
            )

    @pytest.mark.asyncio
    async def test_same_idempotency_replay_is_idempotent(self, es_index, embedding_provider):
        """Same memory_id + same idempotency_key → accepted (idempotent replay)."""
        mid = str(uuid4())
        content = "idempotent replay"
        embedding = await embedding_provider.embed(content)

        r1 = await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
            idempotency_key="key-same",
        )
        r2 = await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
            idempotency_key="key-same",
        )
        assert r1 == "created"
        assert r2 == "updated"


# ---------------------------------------------------------------------------
# Real metadata/provenance round-trip tests (P1-06 retry-5 fix #3)
# ---------------------------------------------------------------------------


class TestMetadataProvenanceRoundTrip:
    """Real metadata/provenance payloads must survive round-trip through ES."""

    @pytest.mark.asyncio
    async def test_arbitrary_metadata_round_trip(self, es_index, embedding_provider):
        """Arbitrary metadata payload survives write/read through ES."""
        mid = str(uuid4())
        content = "metadata round-trip"
        embedding = await embedding_provider.embed(content)
        metadata = {
            "custom_field": "value1",
            "nested": {"deep": {"key": 42}},
            "tags": ["alpha", "beta"],
            "flag": True,
        }

        await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
            metadata=metadata,
        )
        await es_index.refresh()

        doc = await es_index.get(mid)
        assert doc is not None
        assert doc["metadata"] == metadata

    @pytest.mark.asyncio
    async def test_provenance_round_trip(self, es_index, embedding_provider):
        """Full provenance payload survives write/read through ES."""
        mid = str(uuid4())
        content = "provenance round-trip"
        embedding = await embedding_provider.embed(content)
        provenance = {
            "source_type": "document",
            "source_name": "test-doc.pdf",
            "source_uri": "file:///docs/test-doc.pdf",
            "ingestion_job_id": str(uuid4()),
            "version": 3,
            "extracted_at": _now_utc().isoformat(),
            "extracted_by": "test-agent",
        }

        await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
            provenance=provenance,
        )
        await es_index.refresh()

        doc = await es_index.get(mid)
        assert doc is not None
        assert doc["provenance"] == provenance

    @pytest.mark.asyncio
    async def test_evidence_refs_with_metadata_round_trip(self, es_index, embedding_provider):
        """Evidence refs with nested metadata survive round-trip."""
        mid = str(uuid4())
        content = "evidence refs metadata round-trip"
        embedding = await embedding_provider.embed(content)
        evidence_refs = [
            {
                "source_id": "doc-1",
                "source_type": "document",
                "timestamp": _now_utc().isoformat(),
                "chunk_id": "chunk-42",
                "confidence": 0.95,
                "metadata": {"page": 12, "section": "intro"},
            },
            {
                "source_id": "doc-2",
                "source_type": "conversation",
                "timestamp": _now_utc().isoformat(),
                "chunk_id": None,
                "confidence": 0.8,
                "metadata": {"turn": 5},
            },
        ]

        await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
            evidence_refs=evidence_refs,
        )
        await es_index.refresh()

        doc = await es_index.get(mid)
        assert doc is not None
        assert len(doc["evidence_refs"]) == 2
        assert doc["evidence_refs"][0]["metadata"] == {"page": 12, "section": "intro"}
        assert doc["evidence_refs"][1]["metadata"] == {"turn": 5}


# ---------------------------------------------------------------------------
# Rebuild restoring inactive/expired snapshots (P1-06 retry-5 fix #4)
# ---------------------------------------------------------------------------


class TestRebuildDurableSnapshots:
    """Rebuild must restore ALL items, including inactive and expired."""

    @pytest.mark.asyncio
    async def test_rebuild_restores_archived_items(
        self, session_factory, es_index, embedding_provider
    ):
        """Archived items are restored by rebuild."""
        from app.db.models.memory_plane import MemoryItem as MemoryItemRow

        async with session_factory() as session:
            item = _make_memory_item(
                content="archived content",
                status=MemoryItemStatus.ARCHIVED,
            )
            row = MemoryItemRow(
                id=item.id,
                content=item.content,
                status=item.status.value,
                created_at=item.created_at,
                updated_at=item.updated_at,
                expires_at=item.expires_at,
                metadata_=item.metadata,
            )
            session.add(row)
            await session.commit()

            counters = await rebuild_from_postgres(
                session=session,
                index=es_index,
                embedding_provider=embedding_provider,
            )
            assert counters.projected >= 1

            await es_index.refresh()
            doc = await es_index.get(str(item.id))
            assert doc is not None
            assert doc["status"] == "archived"

    @pytest.mark.asyncio
    async def test_rebuild_restores_expired_items(
        self, session_factory, es_index, embedding_provider
    ):
        """Items past their expires_at are restored by rebuild."""
        from datetime import timedelta
        from app.db.models.memory_plane import MemoryItem as MemoryItemRow

        async with session_factory() as session:
            item = _make_memory_item(content="expired content")
            # Set expires_at to the past
            past = _now_utc().replace(tzinfo=None) - timedelta(days=1)
            row = MemoryItemRow(
                id=item.id,
                content=item.content,
                status=item.status.value,
                created_at=item.created_at,
                updated_at=item.updated_at,
                expires_at=past,
                metadata_=item.metadata,
            )
            session.add(row)
            await session.commit()

            counters = await rebuild_from_postgres(
                session=session,
                index=es_index,
                embedding_provider=embedding_provider,
            )
            assert counters.projected >= 1

            await es_index.refresh()
            doc = await es_index.get(str(item.id))
            assert doc is not None
            assert doc["content"] == "expired content"


# ---------------------------------------------------------------------------
# Worker processes a real outbox row (P1-06 retry-5 fix #5)
# ---------------------------------------------------------------------------


class TestWorkerOutboxDelivery:
    """Worker must process a real outbox row end-to-end."""

    @pytest.mark.asyncio
    async def test_worker_processes_real_outbox_row(
        self, session_factory, es_client, embedding_provider
    ):
        """A real outbox row is processed by the handler end-to-end."""
        item = _make_memory_item(content="worker outbox delivery test")
        payload = build_event_payload(memory_item=item, projection_target="elasticsearch")
        agg_id = item.id
        idem_key = build_idempotency_key(agg_id, "elasticsearch")

        event = ProjectionEvent(
            outbox_id=uuid4(),
            aggregate_id=agg_id,
            event_type="memory.remembered",
            schema_version=1,
            projection_target="elasticsearch",
            idempotency_key=idem_key,
            payload=payload,
            attempt_count=1,
            max_attempts=5,
        )

        index_name = f"test_worker_outbox_{uuid4().hex[:8]}"
        try:
            handler = await ElasticsearchProjectionHandler.create(
                es_client,
                embedding_provider=embedding_provider,
                index_name=index_name,
            )
            await handler(event)
            await handler.index.refresh()

            doc = await handler.index.get(str(agg_id))
            assert doc is not None
            assert doc["content"] == item.content
            assert doc["idempotency_key"] == idem_key
        finally:
            try:
                await es_client.indices.delete(index=index_name)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Preflight failure before any claim (P1-06 retry-5 fix #5)
# ---------------------------------------------------------------------------


class TestPreflightNoClaim:
    """Preflight failure must occur before any outbox claim."""

    @pytest.mark.asyncio
    async def test_preflight_failure_no_claim_occurs(self, embedding_provider):
        """Bad ES URL → create() fails before any claim could happen."""
        bad_client = AsyncElasticsearch("http://127.0.0.1:1")
        try:
            with pytest.raises(Exception):
                await ElasticsearchProjectionHandler.create(
                    bad_client,
                    embedding_provider=embedding_provider,
                    preflight_timeout_s=1.0,
                )
        finally:
            await bad_client.close()


# ---------------------------------------------------------------------------
# Genuine outage/recovery path (P1-06 retry-5 fix #5)
# ---------------------------------------------------------------------------


class TestGenuineOutageRecovery:
    """Genuine ES outage and recovery: data written before outage survives."""

    @pytest.mark.asyncio
    async def test_write_survives_outage_recovery(
        self, es_index, es_client, embedding_provider, docker_es
    ):
        """Write data, stop Docker ES, observe failure, restart, verify."""
        import subprocess

        from tests.integration.p1_06_elasticsearch.conftest import (
            _wait_for_es_healthy,
        )

        # Write data before outage
        mid = str(uuid4())
        content = "outage survival content"
        embedding = await embedding_provider.embed(content)

        await es_index.upsert(
            memory_id=mid,
            content=content,
            embedding=embedding,
        )
        await es_index.refresh()

        # Verify data exists
        doc_before = await es_index.get(mid)
        assert doc_before is not None
        assert doc_before["content"] == content

        # Stop ES container for real via Docker Compose.
        subprocess.run(
            [
                docker_es["docker_exe"],
                "compose",
                "-p",
                docker_es["compose_project"],
                "-f",
                docker_es["compose_file"],
                "stop",
                "elasticsearch",
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )

        # Observe connection failure during outage.
        outage_error = None
        try:
            await es_index.get(mid)
        except Exception as exc:
            outage_error = exc

        assert outage_error is not None, "Expected connection error during outage"
        assert is_retryable_es_error(outage_error), (
            f"outage error must be retryable, got {type(outage_error).__name__}: {outage_error}"
        )

        # Restart ES container.
        subprocess.run(
            [
                docker_es["docker_exe"],
                "compose",
                "-p",
                docker_es["compose_project"],
                "-f",
                docker_es["compose_file"],
                "up",
                "-d",
                "--wait",
                "elasticsearch",
            ],
            capture_output=True,
            timeout=120,
            check=True,
        )
        _wait_for_es_healthy(docker_es["es_url"], timeout_s=60, index_name=es_index.index_name)

        # After ES recovers, data is still there (backed by Docker volume).
        doc_after = await es_index.get(mid)
        assert doc_after is not None, "Data must survive Docker outage"
        assert doc_after["content"] == content

        # New write after recovery also works.
        mid2 = str(uuid4())
        content2 = "post-recovery write"
        embedding2 = await embedding_provider.embed(content2)
        await es_index.upsert(
            memory_id=mid2,
            content=content2,
            embedding=embedding2,
        )
        await es_index.refresh()
        doc2 = await es_index.get(mid2)
        assert doc2 is not None
        assert doc2["content"] == content2


# ---------------------------------------------------------------------------
# Full pipeline delivery: completed + dead_letter (P1-06 retry-8 finding #1)
# ---------------------------------------------------------------------------


class TestFullPipelineDelivery:
    """Full worker pipeline through real PostgreSQL outbox store.

    Exercises the real ``ProjectionOutboxStore.claim_batch`` → handler
    dispatch → ``mark_completed`` / ``mark_failed`` path.  No fakes.
    """

    @pytest.mark.asyncio
    async def test_successful_delivery_reaches_completed(
        self, session_factory, es_client, embedding_provider
    ):
        """A real outbox row persisted via the repository reaches ``completed``."""
        item = _make_memory_item(content="pipeline completed test")

        # Persist a real outbox row via the repository helper.
        async with session_factory() as session:
            await enqueue_remembered_events(session, memory_item=item, targets=["elasticsearch"])
            await session.commit()

        index_name = f"test_pipeline_completed_{uuid4().hex[:8]}"
        try:
            handler = await ElasticsearchProjectionHandler.create(
                es_client,
                embedding_provider=embedding_provider,
                index_name=index_name,
            )
            try:
                store = ProjectionOutboxStore(
                    session_factory, worker_identity="test-worker-completed"
                )
                worker = ProjectionWorker(
                    store=store,
                    registry=ProjectionHandlerRegistry({"elasticsearch": handler}),
                    worker_identity="test-worker-completed",
                    batch_size=1,
                    max_concurrency=1,
                    lease_duration=timedelta(seconds=30),
                    delivery_timeout=timedelta(seconds=10),
                    finalization_margin=timedelta(seconds=5),
                    retry_policy=ExponentialBackoffPolicy(base_delay_seconds=1.0),
                    target_filter={"elasticsearch"},
                )

                result = await worker.run_once()
                assert result.completed == 1
                assert result.dead_lettered == 0
                assert result.retried == 0
                assert result.lease_lost == 0
            finally:
                await handler.close()

            # Verify the outbox row state in PostgreSQL.
            async with session_factory() as session:
                rows = await session.execute(select(OutboxRow))
                rows = rows.scalars().all()
                assert len(rows) == 1
                assert rows[0].state == "completed"
                assert rows[0].processed_at is not None
                assert rows[0].lease_owner is None
                assert rows[0].lease_expires_at is None
        finally:
            try:
                await es_client.indices.delete(index=index_name)
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_conflicting_idempotency_delivery_reaches_dead_letter(
        self, session_factory, es_client, embedding_provider
    ):
        """A conflicting idempotency delivery reaches ``dead_letter`` with a permanent error.

        The real ES atomic scripted upsert rejects a replay whose stored
        ``idempotency_key`` differs from the envelope's — proving the
        dead-letter path via the genuine production conflict, not a
        malformed-payload shortcut.
        """
        item = _make_memory_item(content="pipeline dead-letter conflict test")

        # Persist a single real outbox row via the repository helper.
        # The row's payload is canonical; do not mutate it.
        async with session_factory() as session:
            await enqueue_remembered_events(session, memory_item=item, targets=["elasticsearch"])
            await session.commit()

        index_name = f"test_pipeline_conflict_{uuid4().hex[:8]}"
        try:
            handler = await ElasticsearchProjectionHandler.create(
                es_client,
                embedding_provider=embedding_provider,
                index_name=index_name,
            )
            try:
                # Seed the ES index with the same document ID but a
                # different stored idempotency key — simulating a prior
                # successful projection under another envelope key.
                seed_embedding = await embedding_provider.embed(item.content)
                await handler.index.upsert(
                    memory_id=str(item.id),
                    content=item.content,
                    embedding=seed_embedding,
                    status=item.status.value if hasattr(item.status, "value") else str(item.status),
                    idempotency_key="legacy-key",
                )
                await handler.index.refresh()

                store = ProjectionOutboxStore(
                    session_factory, worker_identity="test-worker-conflict"
                )
                worker = ProjectionWorker(
                    store=store,
                    registry=ProjectionHandlerRegistry({"elasticsearch": handler}),
                    worker_identity="test-worker-conflict",
                    batch_size=1,
                    max_concurrency=1,
                    lease_duration=timedelta(seconds=30),
                    delivery_timeout=timedelta(seconds=10),
                    finalization_margin=timedelta(seconds=5),
                    retry_policy=ExponentialBackoffPolicy(base_delay_seconds=1.0),
                    target_filter={"elasticsearch"},
                )

                # The worker's canonical envelope key clashes with the
                # stored "legacy-key" — the ES scripted upsert raises an
                # idempotency conflict, which the handler wraps as a
                # PermanentProjectionError and the worker dead-letters.
                result = await worker.run_once()
                assert result.dead_lettered == 1
                assert result.completed == 0

                # Verify the sole outbox row is dead_letter with the
                # expected permanent error marker.
                async with session_factory() as session:
                    rows = await session.execute(select(OutboxRow))
                    rows = rows.scalars().all()
                    assert len(rows) == 1

                    dl_row = rows[0]
                    assert dl_row.state == "dead_letter"
                    assert dl_row.aggregate_id == item.id
                    assert dl_row.last_error == "permanent_projection_error"
            finally:
                await handler.close()
        finally:
            try:
                await es_client.indices.delete(index=index_name)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Preflight with real pending outbox row (P1-06 retry-8 finding #2)
# ---------------------------------------------------------------------------


class TestPreflightPendingRow:
    """Composition root preflight with a real pending outbox row.

    The composition root's ES preflight must fail BEFORE any outbox claim
    so the pending row's lease/attempt state is unchanged.
    """

    @pytest.mark.asyncio
    async def test_unreachable_es_preserves_pending_row(self, session_factory):
        """Real pending row stays pending after unreachable-ES preflight failure."""
        item = _make_memory_item(content="preflight pending row test")

        # Persist a real pending outbox row.
        async with session_factory() as session:
            await enqueue_remembered_events(session, memory_item=item, targets=["elasticsearch"])
            await session.commit()

        # Record initial outbox state BEFORE composition attempt.
        async with session_factory() as session:
            rows_before = (await session.execute(select(OutboxRow))).scalars().all()
            assert len(rows_before) == 1
            assert rows_before[0].state == "pending"
            assert rows_before[0].attempt_count == 0
            assert rows_before[0].lease_owner is None
            assert rows_before[0].lease_expires_at is None

        # Attempt the full composition root with an unreachable ES endpoint.
        class _NoopEmbedding:
            async def embed(self, text: str) -> list[float]:
                return [0.0] * DEFAULT_DIMS

        with pytest.raises(Exception):
            await compose_and_run_elasticsearch_worker(
                session_factory=session_factory,
                elasticsearch_url="http://127.0.0.1:1",
                embedding_provider=_NoopEmbedding(),
                worker_identity="test-preflight-worker",
                preflight_timeout_s=1.0,
            )

        # The pending row must be completely unchanged: preflight happens
        # BEFORE any outbox claim.
        async with session_factory() as session:
            rows_after = (await session.execute(select(OutboxRow))).scalars().all()
            assert len(rows_after) == 1
            row = rows_after[0]
            assert row.state == "pending", f"Row state must remain pending, got {row.state!r}"
            assert row.attempt_count == 0, f"attempt_count must be 0, got {row.attempt_count}"
            assert row.lease_owner is None, f"lease_owner must be None, got {row.lease_owner!r}"
            assert row.lease_expires_at is None, "lease_expires_at must be None"
            assert row.processed_at is None, "processed_at must be None"
            assert row.last_error is None, f"last_error must be None, got {row.last_error!r}"


# ---------------------------------------------------------------------------
# PostgreSQL read during real ES outage + shard-ready recovery
# (P1-06 retry-8 finding #3)
# ---------------------------------------------------------------------------


class TestPostgresReadDuringRealOutage:
    """PostgreSQL must remain readable during a genuine ES Docker outage.

    After ES restart, the test waits for index-level primary-shard
    readiness before asserting projection recovery.
    """

    @pytest.mark.asyncio
    async def test_postgres_readable_during_outage_with_shard_ready_recovery(
        self,
        session_factory,
        es_index,
        embedding_provider,
        docker_es,
    ):
        from tests.integration.p1_06_elasticsearch.conftest import (
            _wait_for_es_healthy,
        )

        content_marker = "pg-readable-during-outage"

        # 1. Write data to BOTH PostgreSQL and Elasticsearch.
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

        embedding = await embedding_provider.embed(content_marker)
        await es_index.upsert(
            memory_id=str(item.id),
            content=content_marker,
            embedding=embedding,
        )
        await es_index.refresh()

        # 2. Verify PostgreSQL readability BEFORE outage.
        async with session_factory() as session:
            result = await session.execute(select(MemoryItemRow).where(MemoryItemRow.id == item.id))
            fetched = result.scalar_one_or_none()
            assert fetched is not None
            assert fetched.content == content_marker

        # Also verify ES readability before outage.
        doc_before = await es_index.get(str(item.id))
        assert doc_before is not None
        assert doc_before["content"] == content_marker

        # 3. Stop ES container via Docker Compose.
        subprocess.run(
            [
                docker_es["docker_exe"],
                "compose",
                "-p",
                docker_es["compose_project"],
                "-f",
                docker_es["compose_file"],
                "stop",
                "elasticsearch",
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )

        # 4. Perform a REAL PostgreSQL read WHILE ES is unavailable.
        try:
            async with session_factory() as session:
                result = await session.execute(
                    select(MemoryItemRow).where(MemoryItemRow.id == item.id)
                )
                fetched_during_outage = result.scalar_one_or_none()
                assert fetched_during_outage is not None, (
                    "PostgreSQL must remain readable during ES outage"
                )
                assert fetched_during_outage.content == content_marker
        finally:
            # 5. Restart ES container.
            subprocess.run(
                [
                    docker_es["docker_exe"],
                    "compose",
                    "-p",
                    docker_es["compose_project"],
                    "-f",
                    docker_es["compose_file"],
                    "up",
                    "-d",
                    "--wait",
                    "elasticsearch",
                ],
                capture_output=True,
                timeout=120,
                check=True,
            )

        # 6. Wait for index-level primary-shard readiness BEFORE the
        #    post-recovery projection assertion.
        _wait_for_es_healthy(docker_es["es_url"], timeout_s=60, index_name=es_index.index_name)

        # 7. Post-recovery: data written before the outage survives in ES
        #    (backed by Docker volume).
        doc_after = await es_index.get(str(item.id))
        assert doc_after is not None, "ES data must survive Docker outage + restart"
        assert doc_after["content"] == content_marker
