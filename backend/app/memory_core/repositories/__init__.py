"""Memory repository adapters.

This package exposes the unpermissioned PostgreSQL adapter alongside
the pure mapping helpers, typed query filter value objects, and the
P1-03 outbox event builder.
"""

from app.memory_core.repositories.postgres_filters import (
    MemoryQueryFilters,
    build_memory_predicates,
)
from app.memory_core.repositories.postgres_mapping import (
    dict_to_provenance,
    domain_to_evidence_row,
    evidence_row_to_domain,
    evidence_rows_to_domain,
    memory_row_to_domain,
    provenance_to_dict,
)
from app.memory_core.repositories.postgres_outbox import (
    DEFAULT_PROJECTION_TARGETS,
    EVENT_TYPE_REMEMBERED,
    SCHEMA_VERSION,
    build_event_payload,
    build_idempotency_key,
    enqueue_remembered_events,
    validate_projection_targets,
)
from app.memory_core.repositories.postgres_repository import (
    PostgresMemoryRepository,
)
from app.memory_core.repositories.redis_hot import (
    DEFAULT_HOT_TTL_SECONDS,
    DEFAULT_PREFIX,
    HotItemRead,
    HotSessionRead,
    HotStatePayload,
    MAX_SESSION_ITEMS,
    READ_PAGE_WINDOW,
    PermanentPayloadError,
    RebuildCounters,
    RebuildInfrastructureError,
    RedisHotStateReader,
    RedisHotStateWriter,
    build_keys,
    canonical_json,
    effective_expiry_ms,
    item_key,
    rebuild_from_postgres,
    session_key,
)

__all__ = [
    "DEFAULT_HOT_TTL_SECONDS",
    "DEFAULT_PREFIX",
    "DEFAULT_PROJECTION_TARGETS",
    "EVENT_TYPE_REMEMBERED",
    "HotItemRead",
    "HotSessionRead",
    "HotStatePayload",
    "MAX_SESSION_ITEMS",
    "MemoryQueryFilters",
    "READ_PAGE_WINDOW",
    "PermanentPayloadError",
    "PostgresMemoryRepository",
    "RebuildCounters",
    "RebuildInfrastructureError",
    "RedisHotStateReader",
    "RedisHotStateWriter",
    "SCHEMA_VERSION",
    "build_event_payload",
    "build_idempotency_key",
    "build_keys",
    "build_memory_predicates",
    "canonical_json",
    "dict_to_provenance",
    "domain_to_evidence_row",
    "effective_expiry_ms",
    "enqueue_remembered_events",
    "evidence_row_to_domain",
    "evidence_rows_to_domain",
    "item_key",
    "memory_row_to_domain",
    "provenance_to_dict",
    "rebuild_from_postgres",
    "session_key",
    "validate_projection_targets",
]
