"""Versioned index settings/mapping builder (P1-06).

Builds the Elasticsearch index body for memory projection.
The index name includes a version suffix to allow safe re-mapping
without collision with the legacy knowledge base index.

Mapping/version mismatch must fail loudly; never silently mutate an
incompatible index.
"""

from __future__ import annotations

from typing import Any

DEFAULT_INDEX_NAME = "esragpro_memory_projection"
MAPPING_VERSION = 1
DEFAULT_DIMS = 1024
_MIN_DIMS = 2
_MAX_DIMS = 4096


def build_versioned_index_name(
    *,
    base_name: str = DEFAULT_INDEX_NAME,
    version: int = MAPPING_VERSION,
) -> str:
    """Return a versioned index name: ``{base_name}_v{version}``."""
    if not base_name or not base_name.strip():
        raise ValueError("base_name must be non-empty")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("version must be a positive integer")
    return f"{base_name.strip().rstrip('_')}_v{version}"


def _validate_dims(dims: int) -> int:
    if not isinstance(dims, int) or isinstance(dims, bool):
        raise TypeError("dims must be an integer")
    if dims < _MIN_DIMS or dims > _MAX_DIMS:
        raise ValueError(f"dims must be in [{_MIN_DIMS}, {_MAX_DIMS}], got {dims}")
    return dims


def build_index_body(
    *,
    dims: int = DEFAULT_DIMS,
    version: int = MAPPING_VERSION,
) -> dict[str, Any]:
    """Build the full index body (settings + mappings).

    The ``_meta.mapping_version`` field lets us detect incompatible
    indices at startup.
    """
    _validate_dims(dims)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("version must be a positive integer")

    return {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "refresh_interval": "1s",
        },
        "mappings": {
            "_meta": {
                "mapping_version": version,
                "purpose": "memory_projection",
            },
            "dynamic": "strict",
            "properties": {
                "content": {"type": "text"},
                "content_embedding": {
                    "type": "dense_vector",
                    "dims": dims,
                    "index": True,
                    "similarity": "cosine",
                },
                "memory_id": {"type": "keyword"},
                "status": {"type": "keyword"},
                "project_id": {"type": "keyword"},
                "session_id": {"type": "keyword"},
                "agent_id": {"type": "keyword"},
                "knowledge_base_id": {"type": "keyword"},
                "created_at": {"type": "date"},
                "updated_at": {"type": "date"},
                "expires_at": {"type": "date"},
                "event_type": {"type": "keyword"},
                "schema_version": {"type": "integer"},
                "idempotency_key": {"type": "keyword"},
                # Opaque object fields: arbitrary payloads accepted without
                # rejection from the strict root dynamic setting. Inner
                # fields are stored in _source but not indexed/searchable.
                "metadata": {"type": "object", "dynamic": False},
                "provenance": {"type": "object", "dynamic": False},
                "evidence_refs": {
                    "type": "nested",
                    "properties": {
                        "source_id": {"type": "keyword"},
                        "source_type": {"type": "keyword"},
                        "timestamp": {"type": "date"},
                        "chunk_id": {"type": "keyword"},
                        "confidence": {"type": "float"},
                        "metadata": {"type": "object", "dynamic": False},
                    },
                },
            },
        },
    }


def assert_mapping_compatible(
    *,
    actual_meta: dict[str, Any] | None,
    expected_version: int = MAPPING_VERSION,
) -> None:
    """Raise if the live index mapping version does not match expected.

    This is the loud-fail guard: we never silently mutate an
    incompatible index.
    """
    if not actual_meta:
        raise ValueError(f"index _meta is missing; expected mapping_version={expected_version}")
    live_version = actual_meta.get("mapping_version")
    if live_version != expected_version:
        raise ValueError(
            f"mapping version mismatch: index has {live_version!r}, expected {expected_version!r}"
        )
    purpose = actual_meta.get("purpose")
    if purpose != "memory_projection":
        raise ValueError(
            f"mapping purpose mismatch: index has {purpose!r}, expected 'memory_projection'"
        )


__all__ = [
    "DEFAULT_DIMS",
    "DEFAULT_INDEX_NAME",
    "MAPPING_VERSION",
    "assert_mapping_compatible",
    "build_index_body",
    "build_versioned_index_name",
]
