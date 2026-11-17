"""Deterministic encoding for the Neo4j world-memory writer."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Final

from ..projection_worker.contracts import PermanentProjectionError
from ._commands import Neo4jCommand
from ._write_codec import JsonValue

_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SEPARATORS: Final[tuple[str, str]] = (",", ":")


def _command_json(command: Neo4jCommand) -> dict[str, JsonValue]:
    metadata = command.metadata
    payload = command.payload
    return {
        "metadata": {
            "outbox_id": str(metadata.outbox_id),
            "aggregate_id": str(metadata.aggregate_id),
            "event_type": metadata.event_type,
            "schema_version": metadata.schema_version,
            "projection_target": metadata.projection_target,
            "idempotency_key": metadata.idempotency_key,
        },
        "primary_node": {
            "labels": list(payload.primary_node.labels),
            "identity": payload.primary_node.identity,
            "properties": payload.primary_node.properties,
        },
        "related_nodes": [
            {
                "labels": list(node.labels),
                "identity": node.identity,
                "properties": node.properties,
            }
            for node in payload.related_nodes
        ],
        "relationships": [
            {
                "rel_type": relationship.rel_type,
                "from_identity": relationship.from_identity,
                "to_identity": relationship.to_identity,
                "properties": relationship.properties,
            }
            for relationship in payload.relationships
        ],
        "source_envelope": payload.source_envelope,
    }


def canonical_json(value: Mapping[str, JsonValue]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=_SEPARATORS,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise PermanentProjectionError(
            "neo4j projection: command serialization failed", target="neo4j"
        ) from exc


def encode_command(command: Neo4jCommand) -> tuple[str, str, dict[str, JsonValue]]:
    """Return canonical command JSON, its SHA-256 fingerprint, and its data."""
    command_data = _command_json(command)
    encoded = canonical_json(command_data)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), command_data


def validate_identifier(identifier: str) -> None:
    """Reject identifiers before they can be interpolated into Cypher."""
    if not _IDENTIFIER_RE.fullmatch(identifier):
        raise PermanentProjectionError(
            f"neo4j projection: invalid identifier {identifier!r}", target="neo4j"
        )


def materialize_properties(properties: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Keep Neo4j-safe scalar/list values while audit JSON preserves everything."""
    materialized: dict[str, JsonValue] = {}
    for key, value in properties.items():
        if key in {"identity", "properties_json"}:
            continue
        if (
            value is None
            or isinstance(value, (str, int, float, bool))
            or (
                isinstance(value, list)
                and all(item is None or isinstance(item, (str, int, float, bool)) for item in value)
            )
        ):
            materialized[key] = value
    return materialized


__all__ = ["canonical_json", "encode_command", "materialize_properties", "validate_identifier"]
