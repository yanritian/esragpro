"""Shared helpers for the P6-D world-memory projection adapters.

Pure utility functions used by both the Elasticsearch adapter
(:mod:`.adapters`) and the Neo4j adapter (:mod:`._neo4j_adapter`).
No adapter logic lives here — only snapshot extraction, type coercion,
and defensive copying.
"""

from __future__ import annotations

import copy
from typing import Any

from ..projection_worker.contracts import ProjectionEvent
from .errors import WorldMemoryProjectionAdapterError


def _require_dict(value: Any, *, field: str) -> dict[str, Any]:
    """Return *value* as a dict, or raise an adapter error."""
    if not isinstance(value, dict):
        raise WorldMemoryProjectionAdapterError(
            f"expected dict for {field!r}, got {type(value).__name__}",
        )
    return value


def _require_str(value: Any, *, field: str) -> str:
    """Return *value* as a non-empty string, or raise an adapter error."""
    if not isinstance(value, str) or not value:
        raise WorldMemoryProjectionAdapterError(
            f"expected non-empty string for {field!r}, got {value!r}",
        )
    return value


def _as_tuple_str(value: Any) -> tuple[str, ...]:
    """Coerce a list/tuple to a tuple of strings."""
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return ()


def _extract_snapshot(event: ProjectionEvent) -> dict[str, Any]:
    """Extract the world-memory snapshot from a ProjectionEvent payload.

    Raises :class:`WorldMemoryProjectionAdapterError` if the payload is
    missing or does not contain a ``world_memory_snapshot`` dict.
    """
    payload = event.payload
    if not isinstance(payload, dict):
        raise WorldMemoryProjectionAdapterError(
            "ProjectionEvent payload must be a dict",
        )
    snapshot = payload.get("world_memory_snapshot")
    if not isinstance(snapshot, dict):
        raise WorldMemoryProjectionAdapterError(
            "payload is missing 'world_memory_snapshot' dict",
        )
    return snapshot


def _kind_of(snapshot: dict[str, Any]) -> str:
    """Return the ``_kind`` discriminator of a snapshot."""
    kind = snapshot.get("_kind")
    if not isinstance(kind, str) or not kind:
        raise WorldMemoryProjectionAdapterError(
            "snapshot is missing '_kind' discriminator",
        )
    return kind


def _copy_mutable(value: Any) -> Any:
    """Return a defensive deep copy of a JSON-safe value."""
    return copy.deepcopy(value)


__all__ = [
    "_as_tuple_str",
    "_copy_mutable",
    "_extract_snapshot",
    "_kind_of",
    "_require_dict",
    "_require_str",
]
