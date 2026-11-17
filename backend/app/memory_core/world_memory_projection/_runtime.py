from __future__ import annotations

from datetime import timedelta
from typing import Final

from elasticsearch import AsyncElasticsearch
from neo4j import AsyncDriver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..projection_worker.world_memory_store import WorldMemoryProjectionStore
from ..projection_worker.world_memory_worker import WorldMemoryProjectionWorker
from ._elasticsearch_writer import WorldMemoryElasticsearchWriter
from ._event_history_elasticsearch_writer import (
    WorldMemoryElasticsearchEventFanoutWriter,
    WorldMemoryEventHistoryElasticsearchWriter,
)
from ._neo4j_writer import WorldMemoryNeo4jWriter
from ._registry import build_world_memory_handler_registry

_DEFAULT_LEASE_DURATION: Final = timedelta(seconds=30)
_DEFAULT_DELIVERY_TIMEOUT: Final = timedelta(seconds=10)


async def create_world_memory_projection_worker(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    elasticsearch_client: AsyncElasticsearch,
    neo4j_driver: AsyncDriver,
    worker_identity: str,
    elasticsearch_index_name: str = "world-memory-history-v1",
    event_history_index_name: str = "world-memory-events-v2",
    neo4j_database: str = "neo4j",
    batch_size: int = 10,
    lease_duration: timedelta = _DEFAULT_LEASE_DURATION,
    delivery_timeout: timedelta = _DEFAULT_DELIVERY_TIMEOUT,
) -> WorldMemoryProjectionWorker:
    """Build and initialize the dual-target world-memory worker."""
    elasticsearch_writer = WorldMemoryElasticsearchWriter(
        elasticsearch_client,
        index_name=elasticsearch_index_name,
        owns_client=False,
    )
    event_history_writer = WorldMemoryEventHistoryElasticsearchWriter(
        elasticsearch_client,
        index_name=event_history_index_name,
        owns_client=False,
    )
    neo4j_writer = WorldMemoryNeo4jWriter(
        neo4j_driver,
        database=neo4j_database,
        owns_driver=False,
    )

    registry = build_world_memory_handler_registry(
        elasticsearch_writer=WorldMemoryElasticsearchEventFanoutWriter(
            elasticsearch_writer,
            event_history_writer,
        ),
        neo4j_writer=neo4j_writer,
    )
    store = WorldMemoryProjectionStore(session_factory, worker_identity)
    worker = WorldMemoryProjectionWorker(
        store=store,
        registry=registry,
        worker_identity=worker_identity,
        target_filter=frozenset(registry.targets),
        batch_size=batch_size,
        lease_duration=lease_duration,
        delivery_timeout=delivery_timeout,
    )

    await elasticsearch_writer.ensure_index()
    await event_history_writer.ensure_index()
    await neo4j_writer.ensure_schema()
    return worker


__all__ = ["create_world_memory_projection_worker"]
