"""World-memory projection payload adapters and handlers (P6-D / P6-E).

This package converts validated P7-B envelopes into typed ES / Neo4j
target payloads (P6-D) and exposes the world-memory handler boundary
that composes those adapters with narrow writer protocols (P6-E).

Public API (P6-D)
-----------------
- :class:`ElasticsearchPayload` — typed, frozen ES index payload.
- :class:`Neo4jPayload`, :class:`Neo4jNode`, :class:`Neo4jRelationship`
  — typed, frozen Neo4j graph payload.
- :class:`WorldMemoryProjectionAdapterError` — typed adapter error.
- :func:`to_elasticsearch_payload` — adapter from :class:`ProjectionEvent`
  to :class:`ElasticsearchPayload`.
- :func:`to_neo4j_payload` — adapter from :class:`ProjectionEvent`
  to :class:`Neo4jPayload`.

Public API (P6-E)
-----------------
- :class:`ProjectionMetadata` — immutable event-scoped identity.
- :class:`ElasticsearchCommand`, :class:`Neo4jCommand` — typed target
  commands pairing metadata with the P6-D payload.
- :class:`ElasticsearchWriter`, :class:`Neo4jWriter` — narrow async
  writer protocols.
- :class:`WorldMemoryElasticsearchHandler`,
  :class:`WorldMemoryNeo4jHandler` — handler boundary callables.
- :func:`build_world_memory_handler_registry` — composition returning
  a :class:`ProjectionHandlerRegistry` with both world-memory targets.
"""

from __future__ import annotations

from ._commands import (
    ElasticsearchCommand,
    Neo4jCommand,
    ProjectionMetadata,
    metadata_from_event,
)
from ._elasticsearch_writer import WorldMemoryElasticsearchWriter
from ._event_history_elasticsearch_writer import (
    WorldMemoryElasticsearchEventFanoutWriter,
    WorldMemoryEventHistoryElasticsearchWriter,
    encode_event_history_document,
)
from ._handlers import (
    WorldMemoryElasticsearchHandler,
    WorldMemoryNeo4jHandler,
)
from ._neo4j_adapter import to_neo4j_payload
from ._neo4j_writer import WorldMemoryNeo4jWriter
from ._registry import (
    WORLD_MEMORY_ELASTICSEARCH_TARGET,
    WORLD_MEMORY_NEO4J_TARGET,
    build_world_memory_handler_registry,
)
from ._runtime import create_world_memory_projection_worker
from ._writer_protocols import ElasticsearchWriter, Neo4jWriter
from .adapters import to_elasticsearch_payload
from .errors import WorldMemoryProjectionAdapterError
from .payloads import (
    ElasticsearchPayload,
    Neo4jNode,
    Neo4jPayload,
    Neo4jRelationship,
)

__all__ = [
    "WORLD_MEMORY_ELASTICSEARCH_TARGET",
    "WORLD_MEMORY_NEO4J_TARGET",
    # P6-E
    "ElasticsearchCommand",
    # P6-D
    "ElasticsearchPayload",
    "ElasticsearchWriter",
    "Neo4jCommand",
    "Neo4jNode",
    "Neo4jPayload",
    "Neo4jRelationship",
    "Neo4jWriter",
    "ProjectionMetadata",
    "WorldMemoryElasticsearchEventFanoutWriter",
    "WorldMemoryElasticsearchHandler",
    "WorldMemoryElasticsearchWriter",
    "WorldMemoryEventHistoryElasticsearchWriter",
    "WorldMemoryNeo4jHandler",
    "WorldMemoryNeo4jWriter",
    "WorldMemoryProjectionAdapterError",
    "build_world_memory_handler_registry",
    "create_world_memory_projection_worker",
    "encode_event_history_document",
    "metadata_from_event",
    "to_elasticsearch_payload",
    "to_neo4j_payload",
]
