"""Neo4j payload adapter for the world-memory projection (P6-D).

Converts a validated :class:`ProjectionEvent` (carrying a P7-B envelope)
into a typed :class:`Neo4jPayload` suitable for downstream Neo4j handlers.

Supported snapshot kinds
------------------------
- ``observation``  — :class:`WorldObservation`
- ``state``        — :class:`WorldState`
- ``hypothesis``   — :class:`Hypothesis`
- ``fact``         — :class:`VerifiedFact`
- ``context_pack`` — :class:`WorldContextPack`
- ``transition_record`` — :class:`TransitionRecord`
- ``revocation_record`` — :class:`RevocationRecord`
"""

from __future__ import annotations

from typing import Any

from ..projection_worker.contracts import ProjectionEvent
from ..world_memory_serializer import (
    KIND_CONTEXT_PACK,
    KIND_FACT,
    KIND_HYPOTHESIS,
    KIND_OBSERVATION,
    KIND_REVOCATION,
    KIND_STATE,
    KIND_TRANSITION,
)
from ._helpers import (
    _as_tuple_str,
    _copy_mutable,
    _extract_snapshot,
    _kind_of,
)
from .errors import WorldMemoryProjectionAdapterError
from .payloads import Neo4jNode, Neo4jPayload, Neo4jRelationship

# Map snapshot kinds to primary Neo4j labels.
_KIND_TO_LABELS: dict[str, tuple[str, ...]] = {
    KIND_OBSERVATION: ("WorldObservation",),
    KIND_STATE: ("WorldState",),
    KIND_HYPOTHESIS: ("Hypothesis",),
    KIND_FACT: ("VerifiedFact",),
    KIND_CONTEXT_PACK: ("WorldContextPack",),
    KIND_TRANSITION: ("TransitionRecord",),
    KIND_REVOCATION: ("RevocationRecord",),
}


def _resolve_identity(
    snapshot: dict[str, Any],
    source_envelope: dict[str, Any],
    event: ProjectionEvent,
    *,
    kind: str,
) -> str:
    """Resolve the stable identity for a snapshot.

    Priority: ``snapshot.id`` → ``envelope.aggregate_id`` →
    ``event.aggregate_id``.  ``ProjectionEvent`` always carries a UUID
    ``aggregate_id``; it is the stable identity fallback even when a
    minimal payload omits duplicate metadata.
    """
    identity = snapshot.get("id")
    if identity:
        return str(identity)
    agg = source_envelope.get("aggregate_id") or event.aggregate_id
    if not agg:
        raise WorldMemoryProjectionAdapterError(
            "cannot determine node identity: snapshot has no 'id' and "
            "event has no 'aggregate_id'",
            kind=kind,
        )
    return str(agg)


def _build_primary_node(
    kind: str,
    snapshot: dict[str, Any],
    source_envelope: dict[str, Any],
    identity: str,
) -> Neo4jNode:
    """Build the primary Neo4j node for a snapshot."""
    labels = _KIND_TO_LABELS.get(kind)
    if labels is None:
        raise WorldMemoryProjectionAdapterError(
            f"unsupported snapshot kind for Neo4j: {kind!r}",
            kind=kind,
        )

    properties: dict[str, Any] = _copy_mutable(snapshot)
    # Remove the ``_kind`` discriminator — it is expressed as a label.
    properties.pop("_kind", None)
    # Normalise nested lists to tuples for frozen dataclass compatibility.
    for key, val in list(properties.items()):
        if isinstance(val, list):
            properties[key] = tuple(val)
        elif isinstance(val, dict):
            properties[key] = _copy_mutable(val)

    return Neo4jNode(
        labels=labels,
        identity=identity,
        properties=properties,
    )


def _build_related_nodes(
    kind: str,
    snapshot: dict[str, Any],
) -> tuple[Neo4jNode, ...]:
    """Build auxiliary nodes (e.g. context-pack items)."""
    related: list[Neo4jNode] = []

    if kind == KIND_CONTEXT_PACK:
        items = snapshot.get("items", [])
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id")
                if not item_id:
                    continue
                item_kind = item.get("kind", "context_item")
                properties = _copy_mutable(item)
                # Normalise nested lists.
                for key, val in list(properties.items()):
                    if isinstance(val, list):
                        properties[key] = tuple(val)
                related.append(
                    Neo4jNode(
                        labels=(
                            f"WorldContextItem_{item_kind}".title().replace(
                                "_", ""
                            ),
                        ),
                        identity=str(item_id),
                        properties=properties,
                    )
                )

    return tuple(related)


def _build_relationships(
    kind: str,
    snapshot: dict[str, Any],
    from_identity: str,
) -> tuple[Neo4jRelationship, ...]:
    """Build directed edges (supersedes, contradicts, evidence links)."""
    rels: list[Neo4jRelationship] = []

    # Supersedes edges.
    for target in _as_tuple_str(snapshot.get("supersedes")):
        rels.append(
            Neo4jRelationship(
                rel_type="SUPERSEDES",
                from_identity=from_identity,
                to_identity=target,
                properties={},
            )
        )

    # Contradicts edges.
    for target in _as_tuple_str(snapshot.get("contradicts")):
        rels.append(
            Neo4jRelationship(
                rel_type="CONTRADICTS",
                from_identity=from_identity,
                to_identity=target,
                properties={},
            )
        )

    # Evidence edges — for objects that carry evidence_ids.
    evidence_ids = _as_tuple_str(snapshot.get("evidence_ids"))
    if not evidence_ids and kind in (KIND_STATE, KIND_HYPOTHESIS):
        evidence_ids = _as_tuple_str(snapshot.get("observation_ids"))
    for eid in evidence_ids:
        rels.append(
            Neo4jRelationship(
                rel_type="HAS_EVIDENCE",
                from_identity=from_identity,
                to_identity=eid,
                properties={},
            )
        )

    # Context-pack → item edges.
    if kind == KIND_CONTEXT_PACK:
        items = snapshot.get("items", [])
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    rels.append(
                        Neo4jRelationship(
                            rel_type="CONTAINS_ITEM",
                            from_identity=from_identity,
                            to_identity=str(item["id"]),
                            properties={
                                "item_kind": item.get("kind", "unknown")
                            },
                        )
                    )

    # Fact → hypothesis edge.
    if kind == KIND_FACT and snapshot.get("hypothesis_id"):
        rels.append(
            Neo4jRelationship(
                rel_type="DERIVED_FROM",
                from_identity=from_identity,
                to_identity=str(snapshot["hypothesis_id"]),
                properties={},
            )
        )

    return tuple(rels)


def to_neo4j_payload(event: ProjectionEvent) -> Neo4jPayload:
    """Convert a :class:`ProjectionEvent` to a :class:`Neo4jPayload`.

    The primary node carries the projected object with labels derived from
    the snapshot kind.  Related nodes carry auxiliary entities (e.g.
    context-pack items).  Relationships carry every directed edge
    (supersedes, contradicts, evidence, derivation, containment).

    Raises:
        WorldMemoryProjectionAdapterError: If the event payload is
            malformed or the snapshot kind is unsupported.
    """
    source_envelope = _copy_mutable(event.payload)
    snapshot = _extract_snapshot(event)
    kind = _kind_of(snapshot)

    identity = _resolve_identity(snapshot, source_envelope, event, kind=kind)
    primary = _build_primary_node(kind, snapshot, source_envelope, identity)
    related = _build_related_nodes(kind, snapshot)
    relationships = _build_relationships(kind, snapshot, identity)

    return Neo4jPayload(
        primary_node=primary,
        related_nodes=related,
        relationships=relationships,
        source_envelope=source_envelope,
    )


__all__ = ["to_neo4j_payload"]
