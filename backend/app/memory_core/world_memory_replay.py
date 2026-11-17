"""World-memory replay and projection mapping (P8-A).

Validates P7-B envelopes, maps them to the existing immutable
``ProjectionEvent`` shape, and replays a sequence of envelopes
deterministically — deduplicating exact idempotency-key repeats and
rejecting conflicting payloads that reuse the same key.

Design constraints:
- Pure module: no database writes, no network calls, no agent/device action.
- Does not alter ``ProjectionEvent``, ``MemoryItem``, existing ES/Neo4j
  handlers, PostgreSQL models, migrations, or worker scheduling.
- Does not make legacy projection handlers accept P7 payloads yet; that is
  a separate adapter phase after durable storage is available.
- The replay result is passive and append-only.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from .projection_worker.contracts import ProjectionEvent
from .world_memory import _as_naive_utc
from .world_memory_serializer import (
    _VALID_OBJECT_KINDS,
    AGGREGATE_TYPE,
    EVENT_CONTEXT_PACKED,
    EVENT_FACT_REVOKED,
    EVENT_FACT_VERIFIED,
    EVENT_HYPOTHESIS_REFUTED,
    EVENT_HYPOTHESIS_VERIFIED,
    EVENT_OBSERVATION_INGESTED,
    EVENT_STATE_FUSED,
    KIND_CONTEXT_PACK,
    KIND_FACT,
    KIND_OBSERVATION,
    KIND_REVOCATION,
    KIND_STATE,
    KIND_TRANSITION,
    P7_SCHEMA_VERSION,
    deserialize_world_memory,
)

# ============================================================================
# Exceptions
# ============================================================================


class EnvelopeValidationError(ValueError):
    """Raised when a P7-B envelope fails structural validation.

    The ``field`` attribute names the specific top-level field that failed,
    or ``None`` when the failure is structural (e.g. not a dict).
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        self.field_name = field
        super().__init__(message)


class IdempotencyConflictError(Exception):
    """Raised when replay encounters a conflicting payload for a known key.

    An exact duplicate (same key, same payload) is silently deduplicated.
    A conflict (same key, different payload) is rejected.
    """

    def __init__(
        self,
        idempotency_key: str,
        *,
        original_index: int,
        conflicting_index: int,
    ) -> None:
        self.idempotency_key = idempotency_key
        self.original_index = original_index
        self.conflicting_index = conflicting_index
        super().__init__(
            f"Idempotency key {idempotency_key!r} reused with conflicting payload "
            f"(original at index {original_index}, conflict at index {conflicting_index})"
        )


# ============================================================================
# Validated envelope
# ============================================================================


@dataclass(frozen=True)
class ValidatedEnvelope:
    """A P7-B envelope that has passed structural validation.

    All identity and timestamp fields are parsed into their native types.
    The snapshot is the raw dict (not yet deserialized into a domain object)
    so that mapping can decide how to wrap it.

    Use :meth:`deserialize_snapshot` to materialize the domain object.
    """

    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: UUID
    projection_target: str
    idempotency_key: str
    occurred_at: datetime
    world_memory_snapshot: dict[str, Any]

    def deserialize_snapshot(self) -> Any:
        """Deserialize the nested world-memory snapshot via the P7-B dispatcher.

        Returns the appropriate P7 domain object (``WorldObservation``,
        ``WorldState``, ``Hypothesis``, ``VerifiedFact``,
        ``WorldContextPack``, ``TransitionRecord``, or ``RevocationRecord``).
        """
        return deserialize_world_memory(self.world_memory_snapshot)


# ============================================================================
# Replay result
# ============================================================================


@dataclass(frozen=True)
class ReplayConflict:
    """Description of a conflicting duplicate encountered during replay."""

    idempotency_key: str
    original_index: int
    conflicting_index: int


@dataclass(frozen=True)
class ReplayResult:
    """Passive, append-only result of replaying a sequence of envelopes.

    ``accepted`` contains the :class:`ProjectionEvent` objects produced from
    each non-duplicate envelope, in input order.  ``duplicates`` lists the
    idempotency keys that were exact-duplicate skipped.  ``conflicts`` lists
    keys that were rejected due to payload mismatch.

    When ``strict=True`` was passed to :func:`replay_envelopes`, any conflict
    causes :class:`IdempotencyConflictError` to be raised instead of being
    recorded here.
    """

    accepted: tuple[ProjectionEvent, ...]
    duplicates: tuple[str, ...]
    conflicts: tuple[ReplayConflict, ...]


# ============================================================================
# Validation
# ============================================================================

_REQUIRED_FIELDS: tuple[str, ...] = (
    "event_type",
    "schema_version",
    "aggregate_type",
    "aggregate_id",
    "projection_target",
    "idempotency_key",
    "occurred_at",
    "world_memory_snapshot",
)

# Map event types to snapshot kind discriminators.  Used to infer the kind
# for transition and revocation records whose standalone serialised forms
# omit the ``_kind`` field (they are only discriminated inside the envelope
# by the event type).
_EVENT_TO_SNAPSHOT_KIND: dict[str, str] = {
    EVENT_OBSERVATION_INGESTED: KIND_OBSERVATION,
    EVENT_STATE_FUSED: KIND_STATE,
    EVENT_HYPOTHESIS_VERIFIED: KIND_TRANSITION,
    EVENT_HYPOTHESIS_REFUTED: KIND_TRANSITION,
    EVENT_FACT_VERIFIED: KIND_FACT,
    EVENT_FACT_REVOKED: KIND_REVOCATION,
    EVENT_CONTEXT_PACKED: KIND_CONTEXT_PACK,
}


def _parse_occurred_at(raw: Any) -> datetime:
    """Parse an ``occurred_at`` ISO-8601 string to naive UTC."""
    if not isinstance(raw, str) or not raw:
        raise EnvelopeValidationError(
            "occurred_at must be a non-empty ISO-8601 string",
            field="occurred_at",
        )
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError) as exc:
        raise EnvelopeValidationError(
            f"occurred_at is not a valid ISO-8601 datetime: {raw!r}",
            field="occurred_at",
        ) from exc
    return _as_naive_utc(dt)


def _parse_aggregate_id(raw: Any) -> UUID:
    """Parse an ``aggregate_id`` UUID string."""
    if not isinstance(raw, str) or not raw:
        raise EnvelopeValidationError(
            "aggregate_id must be a non-empty UUID string",
            field="aggregate_id",
        )
    try:
        return UUID(raw)
    except (ValueError, TypeError) as exc:
        raise EnvelopeValidationError(
            f"aggregate_id is not a valid UUID: {raw!r}",
            field="aggregate_id",
        ) from exc


def validate_world_memory_envelope(data: dict[str, Any]) -> ValidatedEnvelope:
    """Validate a P7-B envelope dict and return a :class:`ValidatedEnvelope`.

    Checks:
    - *data* is a dict.
    - All required top-level fields are present.
    - ``schema_version`` equals :data:`P7_SCHEMA_VERSION`.
    - ``aggregate_type`` equals :data:`AGGREGATE_TYPE` (``"world_memory"``).
    - ``aggregate_id`` is a valid UUID string.
    - ``projection_target`` is a non-empty string.
    - ``event_type`` is a non-empty string.
    - ``idempotency_key`` is a non-empty string.
    - ``occurred_at`` is a valid ISO-8601 datetime string.
    - ``world_memory_snapshot`` is a dict with a recognised ``_kind``.

    Raises:
        EnvelopeValidationError: If any check fails.
    """
    if not isinstance(data, dict):
        raise EnvelopeValidationError("envelope must be a dict")

    # -- required fields -----------------------------------------------------
    missing = [f for f in _REQUIRED_FIELDS if f not in data]
    if missing:
        raise EnvelopeValidationError(
            f"missing required fields: {', '.join(missing)}",
            field=missing[0],
        )

    # -- event_type ----------------------------------------------------------
    event_type = data["event_type"]
    if not isinstance(event_type, str) or not event_type:
        raise EnvelopeValidationError(
            "event_type must be a non-empty string",
            field="event_type",
        )

    # -- schema_version ------------------------------------------------------
    schema_version = data["schema_version"]
    if not isinstance(schema_version, int) or schema_version < 1:
        raise EnvelopeValidationError(
            f"schema_version must be a positive integer, got {schema_version!r}",
            field="schema_version",
        )
    if schema_version != P7_SCHEMA_VERSION:
        raise EnvelopeValidationError(
            f"unsupported schema_version: {schema_version} "
            f"(expected {P7_SCHEMA_VERSION})",
            field="schema_version",
        )

    # -- aggregate_type ------------------------------------------------------
    aggregate_type = data["aggregate_type"]
    if aggregate_type != AGGREGATE_TYPE:
        raise EnvelopeValidationError(
            f"aggregate_type must be {AGGREGATE_TYPE!r}, got {aggregate_type!r}",
            field="aggregate_type",
        )

    # -- aggregate_id --------------------------------------------------------
    aggregate_id = _parse_aggregate_id(data["aggregate_id"])

    # -- projection_target ---------------------------------------------------
    projection_target = data["projection_target"]
    if not isinstance(projection_target, str) or not projection_target:
        raise EnvelopeValidationError(
            "projection_target must be a non-empty string",
            field="projection_target",
        )

    # -- idempotency_key -----------------------------------------------------
    idempotency_key = data["idempotency_key"]
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise EnvelopeValidationError(
            "idempotency_key must be a non-empty string",
            field="idempotency_key",
        )

    # -- occurred_at ---------------------------------------------------------
    occurred_at = _parse_occurred_at(data["occurred_at"])

    # -- world_memory_snapshot -----------------------------------------------
    snapshot = data["world_memory_snapshot"]
    if not isinstance(snapshot, dict):
        raise EnvelopeValidationError(
            "world_memory_snapshot must be a dict",
            field="world_memory_snapshot",
        )

    # Determine the snapshot kind.  Most objects carry an explicit ``_kind``
    # field, but ``TransitionRecord`` and ``RevocationRecord`` serialised
    # standalone omit it — their kind is inferred from the envelope's event
    # type instead.  For all other kinds, a missing ``_kind`` is an error.
    kind = snapshot.get("_kind")
    if kind is None:
        inferred_kind = _EVENT_TO_SNAPSHOT_KIND.get(event_type)
        # Only allow inference for kinds whose standalone serialisation
        # legitimately omits ``_kind``: transition and revocation records.
        if inferred_kind in (KIND_TRANSITION, KIND_REVOCATION):
            # Inject the inferred kind into the snapshot copy so that the
            # dispatcher and downstream consumers see a consistent shape.
            snapshot = {**snapshot, "_kind": inferred_kind}
            kind = inferred_kind
        else:
            raise EnvelopeValidationError(
                "world_memory_snapshot has unknown or missing _kind: None",
                field="world_memory_snapshot",
            )
    elif kind not in _VALID_OBJECT_KINDS:
        raise EnvelopeValidationError(
            f"world_memory_snapshot has unknown or missing _kind: {kind!r}",
            field="world_memory_snapshot",
        )

    return ValidatedEnvelope(
        event_type=event_type,
        schema_version=schema_version,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        projection_target=projection_target,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at,
        world_memory_snapshot=copy.deepcopy(snapshot),
    )


# ============================================================================
# Mapping to ProjectionEvent
# ============================================================================


def map_to_projection_event(
    envelope: ValidatedEnvelope,
    *,
    outbox_id: UUID,
    attempt_count: int = 0,
    max_attempts: int = 8,
) -> ProjectionEvent:
    """Map a validated P7-B envelope to an immutable :class:`ProjectionEvent`.

    The envelope's fields are forwarded to the ``ProjectionEvent`` constructor.
    The ``payload`` is the full envelope dict (including the nested snapshot),
    matching the shape that legacy outbox rows carry.  The payload is
    defensively copied by ``ProjectionEvent.__init__``.

    Args:
        envelope: A validated P7-B envelope.
        outbox_id: The outbox row ID (supplied by the caller / adapter).
        attempt_count: Current attempt count (default 0).
        max_attempts: Maximum retry attempts (default 8, matching legacy).

    Returns:
        An immutable :class:`ProjectionEvent`.
    """
    if attempt_count < 0:
        raise ValueError(f"attempt_count must be >= 0, got {attempt_count}")
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    # Reconstruct the full payload dict that ProjectionEvent would carry.
    # This mirrors the shape produced by build_world_memory_envelope.
    payload: dict[str, Any] = {
        "event_type": envelope.event_type,
        "schema_version": envelope.schema_version,
        "aggregate_type": envelope.aggregate_type,
        "aggregate_id": str(envelope.aggregate_id),
        "projection_target": envelope.projection_target,
        "idempotency_key": envelope.idempotency_key,
        "occurred_at": envelope.occurred_at.replace(
            tzinfo=timezone.utc
        ).isoformat(),
        "world_memory_snapshot": copy.deepcopy(envelope.world_memory_snapshot),
    }

    return ProjectionEvent(
        outbox_id=outbox_id,
        aggregate_id=envelope.aggregate_id,
        event_type=envelope.event_type,
        schema_version=envelope.schema_version,
        projection_target=envelope.projection_target,
        idempotency_key=envelope.idempotency_key,
        payload=payload,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
    )


# ============================================================================
# Replay
# ============================================================================


def _payloads_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Compare two envelope payloads for structural equality.

    Uses sorted-item comparison to avoid ordering sensitivity in lists that
    are semantically ordered (e.g. transition records).
    """
    return _canonicalize(a) == _canonicalize(b)


def _canonicalize(obj: Any) -> Any:
    """Recursively canonicalize a JSON-like structure for comparison."""
    if isinstance(obj, dict):
        return {k: _canonicalize(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_canonicalize(item) for item in obj]
    return obj


def replay_envelopes(
    envelopes: Sequence[dict[str, Any]],
    *,
    outbox_ids: Sequence[UUID],
    strict: bool = True,
    max_attempts: int = 8,
) -> ReplayResult:
    """Replay a sequence of P7-B envelopes deterministically.

    Validates each envelope, maps it to a :class:`ProjectionEvent`, and
    deduplicates by idempotency key:

    - **Exact duplicate** (same key, same payload): silently skipped; the
      key is recorded in :attr:`ReplayResult.duplicates`.
    - **Conflicting duplicate** (same key, different payload): rejected.
      If *strict* is ``True`` (default), raises
      :class:`IdempotencyConflictError` immediately.  If *strict* is
      ``False``, the conflict is recorded in :attr:`ReplayResult.conflicts`
      and the envelope is skipped.

    Args:
        envelopes: Sequence of raw envelope dicts (as from
            :func:`build_world_memory_envelope`).
        outbox_ids: One outbox ID per envelope.  Must be the same length
            as *envelopes*.  Each outbox ID is used for the corresponding
            :class:`ProjectionEvent`.
        strict: If ``True`` (default), raise on conflicting duplicates.
            If ``False``, record them in the result.
        max_attempts: Maximum retry attempts for produced events.

    Returns:
        A :class:`ReplayResult` with accepted events, dedup keys, and
        (if not strict) conflicts.

    Raises:
        EnvelopeValidationError: If any envelope fails validation.
        IdempotencyConflictError: If *strict* is ``True`` and a conflicting
            duplicate is encountered.
        ValueError: If *outbox_ids* length does not match *envelopes* length.
    """
    if len(outbox_ids) != len(envelopes):
        raise ValueError(
            f"outbox_ids length ({len(outbox_ids)}) must match "
            f"envelopes length ({len(envelopes)})"
        )

    accepted: list[ProjectionEvent] = []
    duplicates: list[str] = []
    conflicts: list[ReplayConflict] = []

    # Track seen idempotency keys → (index, payload) for dedup/conflict.
    seen: dict[str, tuple[int, dict[str, Any]]] = {}

    for idx, (envelope_data, outbox_id) in enumerate(
        zip(envelopes, outbox_ids)
    ):
        validated = validate_world_memory_envelope(envelope_data)
        key = validated.idempotency_key

        if key in seen:
            original_idx, original_payload = seen[key]
            if _payloads_equal(envelope_data, original_payload):
                # Exact duplicate — skip silently.
                duplicates.append(key)
                continue
            # Conflicting duplicate.
            conflict = ReplayConflict(
                idempotency_key=key,
                original_index=original_idx,
                conflicting_index=idx,
            )
            if strict:
                raise IdempotencyConflictError(
                    key,
                    original_index=original_idx,
                    conflicting_index=idx,
                )
            conflicts.append(conflict)
            continue

        # First occurrence — record and map.
        seen[key] = (idx, copy.deepcopy(envelope_data))
        event = map_to_projection_event(
            validated,
            outbox_id=outbox_id,
            attempt_count=0,
            max_attempts=max_attempts,
        )
        accepted.append(event)

    return ReplayResult(
        accepted=tuple(accepted),
        duplicates=tuple(duplicates),
        conflicts=tuple(conflicts),
    )


# ============================================================================
# Public API
# ============================================================================


__all__ = [
    "EnvelopeValidationError",
    "IdempotencyConflictError",
    "ReplayConflict",
    "ReplayResult",
    "ValidatedEnvelope",
    "map_to_projection_event",
    "replay_envelopes",
    "validate_world_memory_envelope",
]
