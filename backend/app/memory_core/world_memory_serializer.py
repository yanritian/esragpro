"""World-memory persistence bridge (P7-B).

Serializes and deserializes P7 world-memory domain objects to and from
JSON-safe dictionaries suitable for the ``MemoryProjectionOutbox`` payload
column.  Emits a schema-versioned, immutable event envelope with a
deterministic idempotency key for replay-safe delivery.

Design constraints:
- Does not alter existing ``MemoryItem``, ``OutboxEvent``, or ORM models.
- Does not perform database writes or require migrations.
- Preserves every field of the P7 domain kernel: UUIDs, both timestamps
  (``observed_at`` / ``ingested_at``), validity bounds, evidence IDs, status,
  version, and supersedes / contradicts links.
- Timestamps are serialized as explicit-UTC ISO-8601 strings and
  deserialized back to the kernel's naive-UTC contract.
- The envelope is compatible with the existing ``ProjectionEvent`` shape
  consumed by projection handlers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import UUID

from .world_memory import (
    Hypothesis,
    HypothesisStatus,
    RevocationRecord,
    TransitionRecord,
    ValidityInterval,
    VerifiedFact,
    WorldContextItem,
    WorldContextItemKind,
    WorldContextPack,
    WorldObservation,
    WorldState,
    _as_naive_utc,
)

# ============================================================================
# Constants
# ============================================================================

P7_SCHEMA_VERSION: int = 1

AGGREGATE_TYPE: str = "world_memory"

# Object kind discriminators stored in the snapshot ``_kind`` field.
KIND_OBSERVATION: str = "observation"
KIND_STATE: str = "state"
KIND_HYPOTHESIS: str = "hypothesis"
KIND_FACT: str = "fact"
KIND_TRANSITION: str = "transition_record"
KIND_REVOCATION: str = "revocation_record"
KIND_CONTEXT_PACK: str = "context_pack"

# Event types following the ``world.<kind>.<action>`` convention.
EVENT_OBSERVATION_INGESTED: str = "world.observation.ingested"
EVENT_STATE_FUSED: str = "world.state.fused"
EVENT_HYPOTHESIS_VERIFIED: str = "world.hypothesis.verified"
EVENT_HYPOTHESIS_REFUTED: str = "world.hypothesis.refuted"
EVENT_FACT_VERIFIED: str = "world.fact.verified"
EVENT_FACT_REVOKED: str = "world.fact.revoked"
EVENT_CONTEXT_PACKED: str = "world.context.packed"

_VALID_OBJECT_KINDS: frozenset[str] = frozenset({
    KIND_OBSERVATION,
    KIND_STATE,
    KIND_HYPOTHESIS,
    KIND_FACT,
    KIND_TRANSITION,
    KIND_REVOCATION,
    KIND_CONTEXT_PACK,
})

# Event types follow the ``world.<kind>.<action>`` convention and are
# documented as part of the public API. The catalog is intentionally
# open-ended — callers may introduce new event types for downstream
# projections without requiring a serializer change; no validation is
# performed against a closed set.

# Idempotency key template — per projection target, consistent with
# the existing ``postgres_outbox`` pattern.
_IDEMPOTENCY_TEMPLATE = "world:{aggregate_id}:{event_type}:v{version}:{target}"


# ============================================================================
# Time / UUID helpers
# ============================================================================


def _format_utc_iso(value: datetime | None) -> str | None:
    """Return an explicit-UTC ISO-8601 string, or ``None`` for null."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


def _format_uuid(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _format_uuids(values: Sequence[UUID]) -> list[str]:
    return [str(v) for v in values]


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string back to naive UTC.

    Accepts both timezone-aware (``+00:00`` / ``Z``) and naive strings.
    Aware values are converted to UTC before stripping ``tzinfo``.
    """
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    return _as_naive_utc(dt)


def _require_datetime(value: str | None, *, field: str) -> datetime:
    """Parse a required ISO-8601 timestamp; raise on ``None``."""
    if value is None:
        raise ValueError(f"required field {field!r} is missing or null")
    dt = datetime.fromisoformat(value)
    return _as_naive_utc(dt)


def _parse_uuid(value: str | None) -> UUID | None:
    if value is None:
        return None
    return UUID(value)


def _parse_uuids(values: Sequence[str] | None) -> tuple[UUID, ...]:
    if values is None:
        return ()
    return tuple(UUID(v) for v in values)


# ============================================================================
# Serialize — domain → JSON-safe dict
# ============================================================================


def serialize_transition_record(record: TransitionRecord) -> dict[str, Any]:
    """Serialize a :class:`TransitionRecord` to a JSON-safe dict."""
    return {
        "from_status": record.from_status.value,
        "to_status": record.to_status.value,
        "evidence_ids": _format_uuids(record.evidence_ids),
        "reason": record.reason,
        "timestamp": _format_utc_iso(record.timestamp),
        "supersedes": _format_uuids(record.supersedes),
        "contradicts": _format_uuids(record.contradicts),
    }


def serialize_revocation_record(record: RevocationRecord) -> dict[str, Any]:
    """Serialize a :class:`RevocationRecord` to a JSON-safe dict."""
    return {
        "evidence_ids": _format_uuids(record.evidence_ids),
        "reason": record.reason,
        "timestamp": _format_utc_iso(record.timestamp),
        "supersedes": _format_uuids(record.supersedes),
        "contradicts": _format_uuids(record.contradicts),
    }


def serialize_world_observation(obs: WorldObservation) -> dict[str, Any]:
    """Serialize a :class:`WorldObservation` to a JSON-safe dict.

    Preserves both independent timestamps, UUIDs, evidence reference,
    version, and metadata.
    """
    return {
        "_kind": KIND_OBSERVATION,
        "id": _format_uuid(obs.id),
        "content": obs.content,
        "observed_at": _format_utc_iso(obs.observed_at),
        "ingested_at": _format_utc_iso(obs.ingested_at),
        "source": obs.source,
        "version": obs.version,
        "raw_evidence_ref": obs.raw_evidence_ref,
        "sensor_id": obs.sensor_id,
        "metadata": dict(obs.metadata),
    }


def serialize_world_state(state: WorldState) -> dict[str, Any]:
    """Serialize a :class:`WorldState` to a JSON-safe dict.

    Preserves the validity interval (including open-ended ``end=None``),
    observation IDs, fusion/projection versions, and supersedes/contradicts
    links.
    """
    return {
        "_kind": KIND_STATE,
        "id": _format_uuid(state.id),
        "content": state.content,
        "observation_ids": _format_uuids(state.observation_ids),
        "validity": {
            "start": _format_utc_iso(state.validity.start),
            "end": _format_utc_iso(state.validity.end),
        },
        "fusion_version": state.fusion_version,
        "projection_version": state.projection_version,
        "supersedes": _format_uuids(state.supersedes),
        "contradicts": _format_uuids(state.contradicts),
        "metadata": dict(state.metadata),
    }


def serialize_hypothesis(hyp: Hypothesis) -> dict[str, Any]:
    """Serialize a :class:`Hypothesis` to a JSON-safe dict.

    Preserves status, evidence IDs, the full transition-record audit trail,
    and supersedes/contradicts links.
    """
    return {
        "_kind": KIND_HYPOTHESIS,
        "id": _format_uuid(hyp.id),
        "content": hyp.content,
        "observation_ids": _format_uuids(hyp.observation_ids),
        "status": hyp.status.value,
        "evidence_ids": _format_uuids(hyp.evidence_ids),
        "supersedes": _format_uuids(hyp.supersedes),
        "contradicts": _format_uuids(hyp.contradicts),
        "created_at": _format_utc_iso(hyp.created_at),
        "transition_record": [
            serialize_transition_record(r) for r in hyp.transition_record
        ],
        "metadata": dict(hyp.metadata),
    }


def serialize_verified_fact(fact: VerifiedFact) -> dict[str, Any]:
    """Serialize a :class:`VerifiedFact` to a JSON-safe dict.

    Preserves the hypothesis link, evidence IDs, fusion version,
    supersedes/contradicts links, and optional revocation record.
    """
    return {
        "_kind": KIND_FACT,
        "id": _format_uuid(fact.id),
        "content": fact.content,
        "hypothesis_id": _format_uuid(fact.hypothesis_id),
        "evidence_ids": _format_uuids(fact.evidence_ids),
        "verified_at": _format_utc_iso(fact.verified_at),
        "fusion_version": fact.fusion_version,
        "supersedes": _format_uuids(fact.supersedes),
        "contradicts": _format_uuids(fact.contradicts),
        "revocation": (
            serialize_revocation_record(fact.revocation)
            if fact.revocation is not None
            else None
        ),
        "metadata": dict(fact.metadata),
    }


def serialize_world_context_item(item: WorldContextItem) -> dict[str, Any]:
    """Serialize a :class:`WorldContextItem` to a JSON-safe dict."""
    return {
        "kind": item.kind.value,
        "id": _format_uuid(item.id),
        "content": item.content,
        "evidence_ids": _format_uuids(item.evidence_ids),
    }


def serialize_world_context_pack(pack: WorldContextPack) -> dict[str, Any]:
    """Serialize a :class:`WorldContextPack` to a JSON-safe dict.

    Preserves item kinds, evidence IDs, query ID, and token budget.
    """
    return {
        "_kind": KIND_CONTEXT_PACK,
        "query_id": _format_uuid(pack.query_id),
        "items": [serialize_world_context_item(item) for item in pack.items],
        "token_budget": pack.token_budget,
        "token_used": pack.token_used,
        "created_at": _format_utc_iso(pack.created_at),
    }


# ============================================================================
# Deserialize — JSON-safe dict → domain
# ============================================================================


def deserialize_transition_record(data: dict[str, Any]) -> TransitionRecord:
    """Deserialize a :class:`TransitionRecord` from a JSON-safe dict."""
    if not isinstance(data, dict):
        raise ValueError("transition record payload must be a dict")
    return TransitionRecord(
        from_status=HypothesisStatus(data["from_status"]),
        to_status=HypothesisStatus(data["to_status"]),
        evidence_ids=_parse_uuids(data.get("evidence_ids")),
        reason=data.get("reason", ""),
        timestamp=_require_datetime(data.get("timestamp"), field="timestamp"),
        supersedes=_parse_uuids(data.get("supersedes")),
        contradicts=_parse_uuids(data.get("contradicts")),
    )


def deserialize_revocation_record(data: dict[str, Any]) -> RevocationRecord:
    """Deserialize a :class:`RevocationRecord` from a JSON-safe dict."""
    if not isinstance(data, dict):
        raise ValueError("revocation record payload must be a dict")
    return RevocationRecord(
        evidence_ids=_parse_uuids(data.get("evidence_ids")),
        reason=data.get("reason", ""),
        timestamp=_require_datetime(data.get("timestamp"), field="timestamp"),
        supersedes=_parse_uuids(data.get("supersedes")),
        contradicts=_parse_uuids(data.get("contradicts")),
    )


def deserialize_world_observation(data: dict[str, Any]) -> WorldObservation:
    """Deserialize a :class:`WorldObservation` from a JSON-safe dict."""
    if not isinstance(data, dict):
        raise ValueError("observation payload must be a dict")
    return WorldObservation(
        id=UUID(data["id"]),
        content=data["content"],
        observed_at=_require_datetime(data.get("observed_at"), field="observed_at"),
        source=data["source"],
        ingested_at=_require_datetime(data.get("ingested_at"), field="ingested_at"),
        version=data.get("version", 1),
        raw_evidence_ref=data.get("raw_evidence_ref"),
        sensor_id=data.get("sensor_id"),
        metadata=dict(data.get("metadata", {})),
    )


def deserialize_world_state(data: dict[str, Any]) -> WorldState:
    """Deserialize a :class:`WorldState` from a JSON-safe dict."""
    if not isinstance(data, dict):
        raise ValueError("state payload must be a dict")
    validity_data = data["validity"]
    validity = ValidityInterval(
        start=_require_datetime(validity_data.get("start"), field="validity.start"),
        end=_parse_datetime(validity_data.get("end")),
    )
    return WorldState(
        id=UUID(data["id"]),
        content=data["content"],
        observation_ids=_parse_uuids(data.get("observation_ids")),
        validity=validity,
        fusion_version=data.get("fusion_version", 1),
        projection_version=data.get("projection_version", 1),
        supersedes=_parse_uuids(data.get("supersedes")),
        contradicts=_parse_uuids(data.get("contradicts")),
        metadata=dict(data.get("metadata", {})),
    )


def deserialize_hypothesis(data: dict[str, Any]) -> Hypothesis:
    """Deserialize a :class:`Hypothesis` from a JSON-safe dict."""
    if not isinstance(data, dict):
        raise ValueError("hypothesis payload must be a dict")
    return Hypothesis(
        id=UUID(data["id"]),
        content=data["content"],
        observation_ids=_parse_uuids(data.get("observation_ids")),
        status=HypothesisStatus(data["status"]),
        evidence_ids=_parse_uuids(data.get("evidence_ids")),
        supersedes=_parse_uuids(data.get("supersedes")),
        contradicts=_parse_uuids(data.get("contradicts")),
        created_at=_require_datetime(data.get("created_at"), field="created_at"),
        transition_record=tuple(
            deserialize_transition_record(r)
            for r in data.get("transition_record", [])
        ),
        metadata=dict(data.get("metadata", {})),
    )


def deserialize_verified_fact(data: dict[str, Any]) -> VerifiedFact:
    """Deserialize a :class:`VerifiedFact` from a JSON-safe dict."""
    if not isinstance(data, dict):
        raise ValueError("fact payload must be a dict")
    revocation_data = data.get("revocation")
    revocation = (
        deserialize_revocation_record(revocation_data)
        if revocation_data is not None
        else None
    )
    return VerifiedFact(
        id=UUID(data["id"]),
        content=data["content"],
        hypothesis_id=UUID(data["hypothesis_id"]),
        evidence_ids=_parse_uuids(data.get("evidence_ids")),
        verified_at=_require_datetime(data.get("verified_at"), field="verified_at"),
        fusion_version=data.get("fusion_version", 1),
        supersedes=_parse_uuids(data.get("supersedes")),
        contradicts=_parse_uuids(data.get("contradicts")),
        revocation=revocation,
        metadata=dict(data.get("metadata", {})),
    )


def deserialize_world_context_item(data: dict[str, Any]) -> WorldContextItem:
    """Deserialize a :class:`WorldContextItem` from a JSON-safe dict."""
    if not isinstance(data, dict):
        raise ValueError("context item payload must be a dict")
    return WorldContextItem(
        kind=WorldContextItemKind(data["kind"]),
        id=UUID(data["id"]),
        content=data["content"],
        evidence_ids=_parse_uuids(data.get("evidence_ids")),
    )


def deserialize_world_context_pack(data: dict[str, Any]) -> WorldContextPack:
    """Deserialize a :class:`WorldContextPack` from a JSON-safe dict."""
    if not isinstance(data, dict):
        raise ValueError("context pack payload must be a dict")
    return WorldContextPack(
        query_id=UUID(data["query_id"]),
        items=tuple(
            deserialize_world_context_item(item)
            for item in data.get("items", [])
        ),
        token_budget=data.get("token_budget", 4000),
        token_used=data.get("token_used", 0),
        created_at=_require_datetime(data.get("created_at"), field="created_at"),
    )


# ============================================================================
# Dispatch helpers
# ============================================================================


def _snapshot_for(
    obj: (
        WorldObservation
        | WorldState
        | Hypothesis
        | VerifiedFact
        | WorldContextPack
        | TransitionRecord
        | RevocationRecord
    ),
) -> tuple[str, dict[str, Any]]:
    """Return ``(kind, snapshot)`` for any supported P7 object."""
    if isinstance(obj, WorldObservation):
        return KIND_OBSERVATION, serialize_world_observation(obj)
    if isinstance(obj, WorldState):
        return KIND_STATE, serialize_world_state(obj)
    if isinstance(obj, Hypothesis):
        return KIND_HYPOTHESIS, serialize_hypothesis(obj)
    if isinstance(obj, VerifiedFact):
        return KIND_FACT, serialize_verified_fact(obj)
    if isinstance(obj, WorldContextPack):
        return KIND_CONTEXT_PACK, serialize_world_context_pack(obj)
    if isinstance(obj, TransitionRecord):
        return KIND_TRANSITION, serialize_transition_record(obj)
    if isinstance(obj, RevocationRecord):
        return KIND_REVOCATION, serialize_revocation_record(obj)
    raise TypeError(f"Unsupported P7 object type: {type(obj).__name__}")


def deserialize_world_memory(data: dict[str, Any]) -> Any:
    """Deserialize a P7 snapshot by its ``_kind`` discriminator.

    Raises :class:`ValueError` for unknown or missing kinds.
    """
    if not isinstance(data, dict):
        raise ValueError("world memory payload must be a dict")
    kind = data.get("_kind")
    if kind not in _VALID_OBJECT_KINDS:
        raise ValueError(f"unknown or missing _kind: {kind!r}")
    dispatch: dict[str, Any] = {
        KIND_OBSERVATION: deserialize_world_observation,
        KIND_STATE: deserialize_world_state,
        KIND_HYPOTHESIS: deserialize_hypothesis,
        KIND_FACT: deserialize_verified_fact,
        KIND_TRANSITION: deserialize_transition_record,
        KIND_REVOCATION: deserialize_revocation_record,
        KIND_CONTEXT_PACK: deserialize_world_context_pack,
    }
    return dispatch[kind](data)


# ============================================================================
# Envelope + idempotency key
# ============================================================================


def build_world_memory_idempotency_key(
    aggregate_id: UUID,
    event_type: str,
    projection_target: str,
    schema_version: int = P7_SCHEMA_VERSION,
) -> str:
    """Deterministic idempotency key for a world-memory event.

    The same ``(aggregate_id, event_type, projection_target, schema_version)``
    always produces the same key.  Different values produce different keys.
    """
    if schema_version < 1:
        raise ValueError(f"schema_version must be >= 1, got {schema_version}")
    if not event_type:
        raise ValueError("event_type must not be empty")
    if not projection_target:
        raise ValueError("projection_target must not be empty")
    return _IDEMPOTENCY_TEMPLATE.format(
        aggregate_id=str(aggregate_id),
        event_type=event_type,
        version=schema_version,
        target=projection_target,
    )


def build_world_memory_envelope(
    obj: (
        WorldObservation
        | WorldState
        | Hypothesis
        | VerifiedFact
        | WorldContextPack
        | TransitionRecord
        | RevocationRecord
    ),
    *,
    event_type: str,
    projection_target: str,
    schema_version: int = P7_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Build a versioned event envelope for the projection outbox.

    The returned dict is compatible with the ``payload`` column of
    ``MemoryProjectionOutbox`` and the :class:`ProjectionEvent` envelope
    consumed by projection handlers.

    Raises:
        TypeError: If *obj* is not a supported P7 domain object.
        ValueError: If *schema_version* < 1 or *event_type* is empty.
    """
    if schema_version < 1:
        raise ValueError(f"schema_version must be >= 1, got {schema_version}")
    if not event_type:
        raise ValueError("event_type must not be empty")
    if not projection_target:
        raise ValueError("projection_target must not be empty")

    kind, snapshot = _snapshot_for(obj)

    # Determine aggregate ID and occurred_at from the domain object.
    if isinstance(obj, WorldContextPack):
        aggregate_id = obj.query_id
        occurred_at = obj.created_at
    elif isinstance(obj, TransitionRecord):
        aggregate_id = UUID(int=0)
        occurred_at = obj.timestamp
    elif isinstance(obj, RevocationRecord):
        aggregate_id = UUID(int=0)
        occurred_at = obj.timestamp
    else:
        aggregate_id = obj.id
        occurred_at = _first_timestamp(obj)

    idempotency_key = build_world_memory_idempotency_key(
        aggregate_id=aggregate_id,
        event_type=event_type,
        projection_target=projection_target,
        schema_version=schema_version,
    )

    return {
        "event_type": event_type,
        "schema_version": schema_version,
        "aggregate_type": AGGREGATE_TYPE,
        "aggregate_id": _format_uuid(aggregate_id),
        "projection_target": projection_target,
        "idempotency_key": idempotency_key,
        "occurred_at": _format_utc_iso(occurred_at),
        "world_memory_snapshot": snapshot,
    }


def _first_timestamp(obj: Any) -> datetime:
    """Return the primary timestamp for a P7 domain object."""
    if isinstance(obj, WorldObservation):
        return obj.observed_at
    if isinstance(obj, Hypothesis):
        return obj.created_at
    if isinstance(obj, VerifiedFact):
        return obj.verified_at
    # WorldState has no single timestamp; use validity start.
    if isinstance(obj, WorldState):
        return obj.validity.start
    raise TypeError(f"no primary timestamp for {type(obj).__name__}")


# ============================================================================
# Public API
# ============================================================================


__all__ = [
    # Constants
    "P7_SCHEMA_VERSION",
    "AGGREGATE_TYPE",
    # Kind discriminators
    "KIND_OBSERVATION",
    "KIND_STATE",
    "KIND_HYPOTHESIS",
    "KIND_FACT",
    "KIND_TRANSITION",
    "KIND_REVOCATION",
    "KIND_CONTEXT_PACK",
    # Event types
    "EVENT_OBSERVATION_INGESTED",
    "EVENT_STATE_FUSED",
    "EVENT_HYPOTHESIS_VERIFIED",
    "EVENT_HYPOTHESIS_REFUTED",
    "EVENT_FACT_VERIFIED",
    "EVENT_FACT_REVOKED",
    "EVENT_CONTEXT_PACKED",
    # Serialize
    "serialize_world_observation",
    "serialize_world_state",
    "serialize_hypothesis",
    "serialize_verified_fact",
    "serialize_world_context_pack",
    "serialize_world_context_item",
    "serialize_transition_record",
    "serialize_revocation_record",
    # Deserialize
    "deserialize_world_observation",
    "deserialize_world_state",
    "deserialize_hypothesis",
    "deserialize_verified_fact",
    "deserialize_world_context_pack",
    "deserialize_world_context_item",
    "deserialize_transition_record",
    "deserialize_revocation_record",
    "deserialize_world_memory",
    # Envelope + idempotency
    "build_world_memory_envelope",
    "build_world_memory_idempotency_key",
]
