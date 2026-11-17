"""Payload validation for the Neo4j projection handler (P1-07).

Validates the JSON-safe projection event payload and cross-checks it
against the envelope fields.  Never logs raw payloads or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class PermanentNeo4jPayloadError(Exception):
    """Non-retryable payload validation error.

    Wraps data-shape problems that no retry can fix (missing required
    fields, type mismatches, idempotency-key conflicts).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class Neo4jMemorySnapshot:
    """Validated, typed view of the memory_snapshot portion of a payload."""

    id: str
    content: str
    status: str
    created_at: str | None
    updated_at: str | None
    expires_at: str | None
    project_id: str | None
    session_id: str | None
    agent_id: str | None
    knowledge_base_id: str | None
    metadata: dict[str, Any]
    provenance: dict[str, Any] | None
    evidence_refs: list[dict[str, Any]]


@dataclass(frozen=True)
class Neo4jParsedPayload:
    """Validated, typed view of a complete Neo4j projection payload."""

    event_type: str
    schema_version: int
    aggregate_id: str
    projection_target: str
    memory_snapshot: Neo4jMemorySnapshot


def _require_str(payload: dict[str, Any], key: str) -> str:
    val = payload.get(key)
    if not isinstance(val, str) or not val:
        raise PermanentNeo4jPayloadError(
            f"payload field {key!r} must be a non-empty string, got {type(val).__name__}"
        )
    return val


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    val = payload.get(key)
    if val is None:
        return None
    if isinstance(val, str):
        return val
    raise PermanentNeo4jPayloadError(
        f"payload field {key!r} must be a string or null, got {type(val).__name__}"
    )


def _require_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    val = payload.get(key)
    if not isinstance(val, dict):
        raise PermanentNeo4jPayloadError(
            f"payload field {key!r} must be a dict, got {type(val).__name__}"
        )
    return val


def _require_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    val = payload.get(key)
    if not isinstance(val, list):
        raise PermanentNeo4jPayloadError(
            f"payload field {key!r} must be a list, got {type(val).__name__}"
        )
    for i, item in enumerate(val):
        if not isinstance(item, dict):
            raise PermanentNeo4jPayloadError(
                f"payload field {key!r}[{i}] must be a dict, got {type(item).__name__}"
            )
    return val


def _validate_memory_snapshot(snapshot: dict[str, Any]) -> Neo4jMemorySnapshot:
    mid = _require_str(snapshot, "id")
    content = _require_str(snapshot, "content")
    status = _require_str(snapshot, "status")

    if status not in ("active", "expired", "archived", "deleted"):
        raise PermanentNeo4jPayloadError(f"payload field 'status' has invalid value {status!r}")

    evidence_refs = _require_list(snapshot, "evidence_refs")
    provenance_val = snapshot.get("provenance")
    if provenance_val is not None and not isinstance(provenance_val, dict):
        raise PermanentNeo4jPayloadError(
            f"payload field 'provenance' must be a dict or null, "
            f"got {type(provenance_val).__name__}"
        )
    provenance = dict(provenance_val) if provenance_val else None

    metadata_val = snapshot.get("metadata", {})
    if not isinstance(metadata_val, dict):
        raise PermanentNeo4jPayloadError(
            f"payload field 'metadata' must be a dict, got {type(metadata_val).__name__}"
        )

    return Neo4jMemorySnapshot(
        id=mid,
        content=content,
        status=status,
        created_at=_optional_str(snapshot, "created_at"),
        updated_at=_optional_str(snapshot, "updated_at"),
        expires_at=_optional_str(snapshot, "expires_at"),
        project_id=_optional_str(snapshot, "project_id"),
        session_id=_optional_str(snapshot, "session_id"),
        agent_id=_optional_str(snapshot, "agent_id"),
        knowledge_base_id=_optional_str(snapshot, "knowledge_base_id"),
        metadata=dict(metadata_val),
        provenance=provenance,
        evidence_refs=[dict(ref) for ref in evidence_refs],
    )


def validate_payload_cross_check(
    payload: dict[str, Any],
    *,
    envelope_idempotency_key: str,
    envelope_aggregate_id: str,
    envelope_event_type: str,
    envelope_schema_version: int,
    envelope_target: str,
) -> Neo4jParsedPayload:
    """Validate a Neo4j projection payload and cross-check with the envelope.

    Raises ``PermanentNeo4jPayloadError`` on any validation failure.
    """
    if not isinstance(payload, dict):
        raise PermanentNeo4jPayloadError("payload must be a dict")

    event_type = _require_str(payload, "event_type")
    if event_type != envelope_event_type:
        raise PermanentNeo4jPayloadError(
            f"payload event_type {event_type!r} != envelope {envelope_event_type!r}"
        )

    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, int) or schema_version < 1:
        raise PermanentNeo4jPayloadError(
            f"payload schema_version must be a positive int, got {schema_version!r}"
        )
    if schema_version != envelope_schema_version:
        raise PermanentNeo4jPayloadError(
            f"payload schema_version {schema_version} != envelope {envelope_schema_version}"
        )

    aggregate_id = _require_str(payload, "aggregate_id")
    if aggregate_id != envelope_aggregate_id:
        raise PermanentNeo4jPayloadError(
            f"payload aggregate_id {aggregate_id!r} != envelope {envelope_aggregate_id!r}"
        )

    projection_target = _require_str(payload, "projection_target")
    if projection_target != envelope_target:
        raise PermanentNeo4jPayloadError(
            f"payload projection_target {projection_target!r} != envelope {envelope_target!r}"
        )

    snapshot_raw = _require_dict(payload, "memory_snapshot")
    snapshot = _validate_memory_snapshot(snapshot_raw)

    if snapshot.id != aggregate_id:
        raise PermanentNeo4jPayloadError(
            f"snapshot.id {snapshot.id!r} != aggregate_id {aggregate_id!r}"
        )

    return Neo4jParsedPayload(
        event_type=event_type,
        schema_version=schema_version,
        aggregate_id=aggregate_id,
        projection_target=projection_target,
        memory_snapshot=snapshot,
    )


__all__ = [
    "Neo4jMemorySnapshot",
    "Neo4jParsedPayload",
    "PermanentNeo4jPayloadError",
    "validate_payload_cross_check",
]
