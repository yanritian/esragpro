from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from elasticsearch import AsyncElasticsearch
from neo4j import AsyncDriver, AsyncGraphDatabase

from app.core.config import settings

if TYPE_CHECKING:
    from .world_memory_candidate_source import WorldMemoryCandidateSource


class _ElasticsearchFactory(Protocol):
    def __call__(self, url: str) -> AsyncElasticsearch: ...


class _Neo4jFactory(Protocol):
    def __call__(self, uri: str, *, auth: tuple[str, str]) -> AsyncDriver: ...


_elasticsearch_client: AsyncElasticsearch | None = None
_neo4j_driver: AsyncDriver | None = None
_candidate_sources: tuple[WorldMemoryCandidateSource, ...] | None = None


async def get_world_memory_candidate_sources(
    *,
    elasticsearch_factory: _ElasticsearchFactory | None = None,
    neo4j_factory: _Neo4jFactory | None = None,
) -> tuple[WorldMemoryCandidateSource, ...]:
    global _candidate_sources, _elasticsearch_client, _neo4j_driver
    if not settings.WORLD_MEMORY_CANDIDATE_SOURCES_ENABLED:
        return ()
    if _candidate_sources is not None:
        return _candidate_sources

    from .world_memory_candidate_adapters import (
        WorldMemoryElasticsearchCandidateSource,
        WorldMemoryNeo4jCandidateSource,
    )

    if elasticsearch_factory is None:
        client = AsyncElasticsearch(settings.ES_HOST)
    else:
        client = elasticsearch_factory(settings.ES_HOST)
    if neo4j_factory is None:
        driver = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )
    else:
        driver = neo4j_factory(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )

    _elasticsearch_client = client
    _neo4j_driver = driver
    _candidate_sources = (
        WorldMemoryElasticsearchCandidateSource(
            client,
            index_name=settings.WORLD_MEMORY_ELASTICSEARCH_INDEX_NAME,
        ),
        WorldMemoryNeo4jCandidateSource(
            driver,
            database=settings.WORLD_MEMORY_NEO4J_DATABASE,
        ),
    )
    return _candidate_sources


async def close_world_memory_candidate_sources() -> None:
    global _candidate_sources, _elasticsearch_client, _neo4j_driver
    client = _elasticsearch_client
    driver = _neo4j_driver
    _candidate_sources = None
    _elasticsearch_client = None
    _neo4j_driver = None
    if client is not None:
        try:
            await client.close()
        finally:
            if driver is not None:
                await driver.close()
    elif driver is not None:
        await driver.close()


__all__ = [
    "close_world_memory_candidate_sources",
    "get_world_memory_candidate_sources",
]
