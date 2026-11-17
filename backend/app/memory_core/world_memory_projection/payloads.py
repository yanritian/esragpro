"""Typed, JSON-safe target payload contracts for P6-D adapters.

Defines the immutable, frozen dataclass payloads that the Elasticsearch and
Neo4j adapters produce from a validated P7-B envelope.  Both payloads carry
the complete source envelope (defensively copied) for audit and replay, so
downstream handlers can reconstruct or verify any field without accessing
external state.

Design constraints:
- All fields are JSON-safe: ``str``, ``int``, ``float``, ``bool``, ``None``,
  nested ``dict`` / ``list`` of the same.  UUIDs and datetimes are stored
  as their ISO-8601 string form.
- Every payload is frozen and slots-based to prevent accidental mutation.
- The ``source_envelope`` field is a defensive deep copy — mutating the
  caller's dict after adapter invocation does not affect the payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Elasticsearch target payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ElasticsearchPayload:
    """A typed, JSON-safe Elasticsearch index payload.

    Carries a stable ``id`` for document upsert, an ``object_kind``
    discriminator, the searchable ``content``, evidence references, both
    or either of the time fields (``timestamps`` for observed/ingested,
    ``validity`` for intervals), relationship links (``supersedes``,
    ``contradicts``), version fields, metadata, and the full source
    envelope for audit / replay.
    """

    id: str
    object_kind: str
    content: str
    evidence_ids: tuple[str, ...]
    timestamps: dict[str, str | None]
    validity: dict[str, str | None] | None
    supersedes: tuple[str, ...]
    contradicts: tuple[str, ...]
    version: int | None
    fusion_version: int | None
    projection_version: int | None
    status: str | None
    metadata: dict[str, Any]
    source_envelope: dict[str, Any]


# ---------------------------------------------------------------------------
# Neo4j target payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Neo4jNode:
    """A typed Neo4j node: labels, properties, and optional identity."""

    labels: tuple[str, ...]
    identity: str
    properties: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Neo4jRelationship:
    """A typed, directed Neo4j relationship."""

    rel_type: str
    from_identity: str
    to_identity: str
    properties: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Neo4jPayload:
    """A typed, JSON-safe Neo4j graph payload.

    ``primary_node`` carries the projected object.  ``related_nodes`` carries
    auxiliary nodes (e.g. context-pack items, transition audit nodes).
    ``relationships`` carries every directed edge (supersedes, contradicts,
    evidence, hypothesis-link, etc.).

    The ``source_envelope`` is the full, defensively copied source envelope
    for audit / replay.
    """

    primary_node: Neo4jNode
    related_nodes: tuple[Neo4jNode, ...]
    relationships: tuple[Neo4jRelationship, ...]
    source_envelope: dict[str, Any]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "ElasticsearchPayload",
    "Neo4jNode",
    "Neo4jPayload",
    "Neo4jRelationship",
]
