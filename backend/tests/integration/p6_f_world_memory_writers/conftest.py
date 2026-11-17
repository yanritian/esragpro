from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from elastic_transport import HttpxAsyncHttpNode
from elasticsearch import AsyncElasticsearch
from neo4j import AsyncDriver, AsyncGraphDatabase
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.migration_config import validate_migration_test_database_url
from app.memory_core.world_memory_projection._elasticsearch_writer import (
    WorldMemoryElasticsearchWriter,
)

_ELASTICSEARCH_URL = os.environ.get(
    "P6_F_ELASTICSEARCH_URL",
    "http://127.0.0.1:9200",
)
_NEO4J_URL = os.environ.get("P6_F_NEO4J_URL", "bolt://127.0.0.1:7687")
_NEO4J_DATABASE = "neo4j"
_NEO4J_PASSWORD_ENV = "P6_F_NEO4J_PASSWORD"


async def _truncate_world_memory_tables(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await session.execute(
            text("TRUNCATE world_memory_projection_outbox, world_memory_envelopes")
        )
        await session.commit()


@pytest.fixture
async def elasticsearch_client() -> AsyncIterator[AsyncElasticsearch]:
    client = AsyncElasticsearch(
        _ELASTICSEARCH_URL,
        node_class=HttpxAsyncHttpNode,
    )
    try:
        _ = await client.info()
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture
async def neo4j_driver() -> AsyncIterator[AsyncDriver]:
    password = os.environ.get(_NEO4J_PASSWORD_ENV)
    if not password:
        pytest.skip(f"{_NEO4J_PASSWORD_ENV} not set")

    driver = AsyncGraphDatabase.driver(
        _NEO4J_URL,
        auth=("neo4j", password),
    )
    try:
        await driver.verify_connectivity()
        yield driver
    finally:
        await driver.close()


@pytest_asyncio.fixture
async def p6_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if os.environ.get("ESRAGPRO_MIGRATION_TEST_MODE") != "1":
        pytest.skip("ESRAGPRO_MIGRATION_TEST_MODE must be 1 for migration tests")
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        pytest.skip("DATABASE_URL not set")
    url = validate_migration_test_database_url(raw_url)

    engine = create_async_engine(url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _truncate_world_memory_tables(session_factory)
        yield session_factory
    finally:
        try:
            await _truncate_world_memory_tables(session_factory)
        finally:
            await engine.dispose()


@pytest.fixture
async def world_memory_writer(
    elasticsearch_client: AsyncElasticsearch,
    world_memory_index_name: str,
) -> AsyncIterator[WorldMemoryElasticsearchWriter]:
    writer = WorldMemoryElasticsearchWriter(
        elasticsearch_client,
        index_name=world_memory_index_name,
    )
    try:
        await writer.ensure_index()
        yield writer
    finally:
        _ = await elasticsearch_client.indices.delete(
            index=world_memory_index_name,
            ignore_unavailable=True,
        )


@pytest.fixture
def world_memory_index_name() -> str:
    return f"p6-f-world-memory-{uuid4().hex}"


__all__ = [
    "elasticsearch_client",
    "neo4j_driver",
    "p6_session_factory",
    "world_memory_index_name",
    "world_memory_writer",
]
