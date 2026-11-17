"""Shared fixtures for P1-06 Elasticsearch projection integration tests.

Provides isolated PostgreSQL and Elasticsearch connections for testing
real worker projection, round-trip fidelity, BM25/kNN/hybrid search,
filters, duplicate/conflict handling, outage/recovery, persistence
restart, rebuild, and latency measurements.

No ACL/permission/tenant fields anywhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import pytest
from elasticsearch import AsyncElasticsearch
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.migration_config import validate_migration_test_database_url
from app.memory_core.repositories.elasticsearch_projection.index import (
    ElasticsearchProjectionIndex,
)
from app.memory_core.repositories.elasticsearch_projection.mapping import (
    DEFAULT_DIMS,
    MAPPING_VERSION,
)
from app.memory_core.unpermissioned_contracts import EvidenceRef


def _database_url() -> Optional[str]:
    if os.environ.get("ESRAGPRO_MIGRATION_TEST_MODE") != "1":
        return None
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        return None
    return validate_migration_test_database_url(raw)


def _elasticsearch_url() -> Optional[str]:
    if os.environ.get("ESRAGPRO_MIGRATION_TEST_MODE") != "1":
        return None
    return os.environ.get("ELASTICSEARCH_URL")


class DeterministicEmbeddingProvider:
    """Deterministic embedding provider for tests.

    Generates a fixed vector based on content hash so tests are
    reproducible and vectors are comparable.
    """

    def __init__(self, dims: int = DEFAULT_DIMS):
        self._dims = dims

    async def embed(self, text: str) -> list[float]:
        hash_val = hash(text) % (10**9)
        base = hash_val / 10**9
        vec = [(base + i * 0.001) % 1.0 for i in range(self._dims)]
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


@pytest.fixture
async def session_factory():
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL not set (run via run-p1-06-elasticsearch-test.ps1)")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
async def db_engine():
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL not set")
    engine = create_async_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def es_client():
    url = _elasticsearch_url()
    if not url:
        pytest.skip("ELASTICSEARCH_URL not set")
    client = AsyncElasticsearch(url)
    try:
        await client.info()
        yield client
    finally:
        await client.close()


@pytest.fixture
def embedding_provider():
    return DeterministicEmbeddingProvider(dims=DEFAULT_DIMS)


@pytest.fixture
async def es_index(es_client, embedding_provider):
    index_name = f"test_p1_06_{uuid4().hex[:8]}"
    idx = ElasticsearchProjectionIndex(
        es_client,
        index_name=index_name,
        dims=DEFAULT_DIMS,
        mapping_version=MAPPING_VERSION,
    )
    await idx.ensure_index()
    try:
        yield idx
    finally:
        try:
            await es_client.indices.delete(index=index_name)
        except Exception:
            pass


@pytest.fixture
def evidence_factory():
    def _make(
        source_id: str = "doc-1",
        source_type: str = "document",
        confidence: float = 0.9,
        chunk_id: Optional[str] = None,
    ) -> EvidenceRef:
        return EvidenceRef(
            source_id=source_id,
            source_type=source_type,
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            chunk_id=chunk_id,
            confidence=confidence,
        )

    return _make


@pytest.fixture(autouse=True)
async def _isolate_tables(session_factory):
    async with session_factory() as session:
        await session.execute(
            text("TRUNCATE memory_projection_outbox, evidence_refs, memory_items")
        )
        await session.commit()
    yield
    async with session_factory() as session:
        await session.execute(
            text("TRUNCATE memory_projection_outbox, evidence_refs, memory_items")
        )
        await session.commit()


@pytest.fixture
def docker_es():
    """Provide real Docker stop/start for the ES container.

    Reads P1_06_DOCKER_EXE, P1_06_COMPOSE_FILE, and P1_06_COMPOSE_PROJECT
    from the environment (set by run-p1-06-elasticsearch-test.ps1).
    Skips when Docker control is not available.
    """
    docker_exe = os.environ.get("P1_06_DOCKER_EXE")
    compose_file = os.environ.get("P1_06_COMPOSE_FILE")
    compose_project = os.environ.get("P1_06_COMPOSE_PROJECT")
    es_url = os.environ.get("ELASTICSEARCH_URL")
    if not all([docker_exe, compose_file, compose_project, es_url]):
        pytest.skip("Docker control env not set (run via test runner script)")
    yield {
        "docker_exe": docker_exe,
        "compose_file": compose_file,
        "compose_project": compose_project,
        "es_url": es_url,
    }
    # Always ensure ES is running after the test (cleanup guarantee).
    try:
        subprocess.run(
            [
                docker_exe,
                "compose",
                "-p",
                compose_project,
                "-f",
                compose_file,
                "up",
                "-d",
                "--wait",
                "elasticsearch",
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
    except Exception:
        pass


def _wait_for_es_healthy(
    es_url: str,
    timeout_s: float = 60.0,
    index_name: Optional[str] = None,
) -> None:
    """Block until ES is ready for the target index, or timeout.

    When ``index_name`` is provided the function requires more than just
    process-level HTTP health (``/_cluster/health`` returning 200). After a
    Docker restart the HTTP layer can respond before the target index's
    primary shard has finished initialising, which causes ``no_shard_available_action_exception``
    on the next GET. Therefore we poll ``/_cluster/health/<index_name>`` and
    require:

    * status is ``green`` or ``yellow`` (at least one active primary shard);
    * ``active_primary_shards >= 1``;
    * ``initializing_shards == 0``.

    When ``index_name`` is omitted the original behaviour is preserved: wait
    for ``/_cluster/health`` to return HTTP 200.
    """
    import urllib.request

    deadline = time.monotonic() + timeout_s
    last_error: Optional[str] = None

    if index_name is None:
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{es_url}/_cluster/health", timeout=3) as resp:
                    if resp.status == 200:
                        return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2)
        raise TimeoutError(
            f"ES at {es_url} did not become healthy within {timeout_s}s (last error: {last_error})"
        )

    # Index-scoped readiness: wait for the target index's primary shard.
    health_url = f"{es_url}/_cluster/health/{index_name}"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=3) as resp:
                if resp.status != 200:
                    last_error = f"HTTP {resp.status}"
                    time.sleep(1)
                    continue
                body = json.loads(resp.read().decode("utf-8"))
                status = body.get("status")
                active_primary = int(body.get("active_primary_shards", 0))
                initializing = int(body.get("initializing_shards", 0))
                if status in ("green", "yellow") and active_primary >= 1 and initializing == 0:
                    return
                last_error = (
                    f"status={status} active_primary={active_primary} initializing={initializing}"
                )
        except (TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    raise TimeoutError(
        f"ES index {index_name!r} at {es_url} did not have an active primary "
        f"shard within {timeout_s}s (last observation: {last_error})"
    )
