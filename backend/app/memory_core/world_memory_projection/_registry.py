"""Composition entry point for the world-memory handler registry (P6-E).

Builds a :class:`ProjectionHandlerRegistry` exposing exactly the
``elasticsearch`` and ``neo4j`` world-memory targets. The registry is
intended for the separate world-memory worker (P6-C) — it does not
register or alter the legacy ``memory_snapshot`` handlers.

This module performs no I/O and never writes to Elasticsearch, Neo4j,
PostgreSQL, Redis, or MinIO.
"""
from __future__ import annotations

from typing import Final

from ..projection_worker.contracts import ProjectionHandlerRegistry
from ._handlers import (
    _ELASTICSEARCH_TARGET,
    _NEO4J_TARGET,
    WorldMemoryElasticsearchHandler,
    WorldMemoryNeo4jHandler,
)
from ._writer_protocols import ElasticsearchWriter, Neo4jWriter

#: Projection target name for the world-memory Elasticsearch handler.
WORLD_MEMORY_ELASTICSEARCH_TARGET: Final[str] = _ELASTICSEARCH_TARGET

#: Projection target name for the world-memory Neo4j handler.
WORLD_MEMORY_NEO4J_TARGET: Final[str] = _NEO4J_TARGET


def build_world_memory_handler_registry(
    *,
    elasticsearch_writer: ElasticsearchWriter,
    neo4j_writer: Neo4jWriter,
) -> ProjectionHandlerRegistry:
    """Return a :class:`ProjectionHandlerRegistry` with both world-memory
    targets bound to the supplied writers.

    The registry contains exactly two entries — ``elasticsearch`` and
    ``neo4j``. Callers must construct one world-memory worker per
    invocation; the registry is never merged with legacy handlers here.
    """
    es_handler = WorldMemoryElasticsearchHandler(
        elasticsearch_writer,
    )
    neo4j_handler = WorldMemoryNeo4jHandler(
        neo4j_writer,
    )
    return ProjectionHandlerRegistry(
        {
            WORLD_MEMORY_ELASTICSEARCH_TARGET: es_handler,
            WORLD_MEMORY_NEO4J_TARGET: neo4j_handler,
        }
    )


__all__ = [
    "WORLD_MEMORY_ELASTICSEARCH_TARGET",
    "WORLD_MEMORY_NEO4J_TARGET",
    "build_world_memory_handler_registry",
]
