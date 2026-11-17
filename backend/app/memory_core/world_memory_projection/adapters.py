"""Elasticsearch adapter for the world-memory projection (P6-D).

Converts a validated :class:`ProjectionEvent` (carrying a P7-B envelope)
into a typed :class:`ElasticsearchPayload` suitable for downstream ES
handlers.  The adapter is *pure*: no I/O, no database writes, no mutation
of caller state.

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
from .payloads import ElasticsearchPayload

# Explicit whitelist of the seven P7-B snapshot kinds the ES adapter can
# serialize. Any other ``_kind`` discriminator is refused with a typed error
# rather than silently passed through.
_SUPPORTED_ES_KINDS: frozenset[str] = frozenset({
    KIND_OBSERVATION,
    KIND_STATE,
    KIND_HYPOTHESIS,
    KIND_FACT,
    KIND_CONTEXT_PACK,
    KIND_TRANSITION,
    KIND_REVOCATION,
})


def to_elasticsearch_payload(
    event: ProjectionEvent,
) -> ElasticsearchPayload:
    """Convert a :class:`ProjectionEvent` to an :class:`ElasticsearchPayload`.

    Preserves every field of the source envelope — object identity, kind,
    content, evidence IDs, dual timestamps, validity interval,
    supersedes/contradicts links, version fields, status, and metadata.
    The full source envelope is defensively copied into ``source_envelope``
    for audit and replay.

    Raises:
        WorldMemoryProjectionAdapterError: If the event payload is
            malformed or the snapshot kind is unsupported.
    """
    source_envelope = _copy_mutable(event.payload)
    snapshot = _extract_snapshot(event)
    kind = _kind_of(snapshot)

    if kind not in _SUPPORTED_ES_KINDS:
        raise WorldMemoryProjectionAdapterError(
            f"unsupported snapshot kind for ES projection: {kind!r}",
            kind=kind,
        )

    # -- object id -----------------------------------------------------------
    # Identity priority: snapshot.id > envelope.aggregate_id > event.aggregate_id.
    # ProjectionEvent always carries a UUID aggregate_id; it is the stable
    # identity fallback even when a minimal payload omits duplicate metadata.
    obj_id = snapshot.get("id")
    if not obj_id:
        agg = source_envelope.get("aggregate_id") or event.aggregate_id
        if not agg:
            raise WorldMemoryProjectionAdapterError(
                "cannot determine object id: snapshot has no 'id' and "
                "event has no 'aggregate_id'",
                kind=kind,
            )
        obj_id = str(agg)
    else:
        obj_id = str(obj_id)

    # -- content -------------------------------------------------------------
    content = snapshot.get("content", "")
    if kind in (KIND_TRANSITION, KIND_REVOCATION):
        content = snapshot.get("reason", content)

    # -- evidence ids --------------------------------------------------------
    evidence_ids = _as_tuple_str(snapshot.get("evidence_ids"))
    if not evidence_ids and kind == KIND_STATE:
        evidence_ids = _as_tuple_str(snapshot.get("observation_ids"))
    if not evidence_ids and kind == KIND_HYPOTHESIS:
        evidence_ids = _as_tuple_str(snapshot.get("observation_ids"))
    if not evidence_ids and kind == KIND_CONTEXT_PACK:
        items = snapshot.get("items", [])
        collected: list[str] = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    collected.extend(
                        _as_tuple_str(item.get("evidence_ids"))
                    )
        evidence_ids = tuple(collected)

    # -- timestamps ----------------------------------------------------------
    timestamps: dict[str, str | None] = {}
    for ts_field in (
        "observed_at",
        "ingested_at",
        "created_at",
        "verified_at",
        "timestamp",
    ):
        if ts_field in snapshot:
            timestamps[ts_field] = snapshot[ts_field]

    # -- validity ------------------------------------------------------------
    validity_raw = snapshot.get("validity")
    validity: dict[str, str | None] | None = None
    if isinstance(validity_raw, dict):
        validity = {
            "start": validity_raw.get("start"),
            "end": validity_raw.get("end"),
        }

    # -- supersedes / contradicts --------------------------------------------
    supersedes = _as_tuple_str(snapshot.get("supersedes"))
    contradicts = _as_tuple_str(snapshot.get("contradicts"))

    # -- versions ------------------------------------------------------------
    version = snapshot.get("version")
    if not isinstance(version, int):
        version = None
    fusion_version = snapshot.get("fusion_version")
    if not isinstance(fusion_version, int):
        fusion_version = None
    projection_version = snapshot.get("projection_version")
    if not isinstance(projection_version, int):
        projection_version = None

    # -- status --------------------------------------------------------------
    status = snapshot.get("status")
    if not isinstance(status, str):
        status = None

    # -- metadata ------------------------------------------------------------
    metadata_raw = snapshot.get("metadata")
    metadata = (
        _copy_mutable(metadata_raw)
        if isinstance(metadata_raw, dict)
        else {}
    )

    return ElasticsearchPayload(
        id=obj_id,
        object_kind=kind,
        content=content,
        evidence_ids=evidence_ids,
        timestamps=timestamps,
        validity=validity,
        supersedes=supersedes,
        contradicts=contradicts,
        version=version,
        fusion_version=fusion_version,
        projection_version=projection_version,
        status=status,
        metadata=metadata,
        source_envelope=source_envelope,
    )


__all__ = ["to_elasticsearch_payload"]
