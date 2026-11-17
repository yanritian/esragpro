"""Typed projection/search/filter/result contracts (P1-06).

Pydantic models for Elasticsearch payload validation, search filter
construction, and typed search results. No permission/ACL fields.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.memory_core.repositories.postgres_outbox import (
    AGGREGATE_TYPE,
    EVENT_TYPE_REMEMBERED,
    SCHEMA_VERSION,
)


class PermanentElasticsearchPayloadError(ValueError):
    """Malformed or contradictory Elasticsearch payload."""


class _SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str
    content: str
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    expires_at: Optional[str] = None
    project_id: Optional[str] = None
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    knowledge_base_id: Optional[str] = None
    provenance: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)


class ElasticsearchPayload(BaseModel):
    """Strict payload model for Elasticsearch projection."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: str
    projection_target: str
    occurred_at: str
    memory_snapshot: _SnapshotModel


class SearchFilters(BaseModel):
    """Optional search dimension filters (no ACL/permission fields).

    All fields default to ``None`` meaning "no filter".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: Optional[str] = None
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    knowledge_base_id: Optional[str] = None
    status: Optional[str] = "active"
    include_inactive: bool = False
    updated_after: Optional[str] = None

    def to_es_filter(self) -> list[dict[str, Any]]:
        """Build Elasticsearch filter clauses."""
        clauses: list[dict[str, Any]] = []
        if self.project_id is not None:
            clauses.append({"term": {"project_id": self.project_id}})
        if self.session_id is not None:
            clauses.append({"term": {"session_id": self.session_id}})
        if self.agent_id is not None:
            clauses.append({"term": {"agent_id": self.agent_id}})
        if self.knowledge_base_id is not None:
            clauses.append(
                {"term": {"knowledge_base_id": self.knowledge_base_id}},
            )
        if not self.include_inactive:
            if self.status:
                clauses.append({"term": {"status": self.status}})
            else:
                clauses.append({"term": {"status": "active"}})
            now_iso = datetime.now(timezone.utc).isoformat()
            clauses.append(
                {
                    "bool": {
                        "should": [
                            {
                                "bool": {
                                    "must_not": {
                                        "exists": {"field": "expires_at"},
                                    },
                                },
                            },
                            {"range": {"expires_at": {"gt": now_iso}}},
                        ],
                        "minimum_should_match": 1,
                    },
                },
            )
        if self.updated_after is not None:
            clauses.append(
                {"range": {"updated_at": {"gte": self.updated_after}}},
            )
        return clauses


class ElasticsearchSearchResult(BaseModel):
    """Typed search result from Elasticsearch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str
    content: str
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    expires_at: Optional[str] = None
    project_id: Optional[str] = None
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    knowledge_base_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: Optional[dict[str, Any]] = None
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    bm25_rank: Optional[int] = None
    vector_rank: Optional[int] = None
    rrf_score: Optional[float] = None
    es_index: Optional[str] = None
    es_id: Optional[str] = None


def validate_embedding(
    vector: list[float] | Any,
    *,
    expected_dims: int,
) -> list[float]:
    """Validate a vector for ES write/query.

    - Must be a list/tuple of finite numbers.
    - Must have exactly ``expected_dims`` elements.
    - Returns the validated list.
    Raises PermanentElasticsearchPayloadError on failure.
    """
    if not isinstance(vector, (list, tuple)):
        raise PermanentElasticsearchPayloadError(
            "embedding must be a list or tuple",
        )
    if len(vector) != expected_dims:
        raise PermanentElasticsearchPayloadError(
            f"embedding has {len(vector)} dims, expected {expected_dims}",
        )
    for i, v in enumerate(vector):
        if isinstance(v, bool):
            raise PermanentElasticsearchPayloadError(
                f"embedding[{i}] is a bool, must be float",
            )
        if not isinstance(v, (int, float)):
            raise PermanentElasticsearchPayloadError(
                f"embedding[{i}] is {type(v).__name__}, must be numeric",
            )
        if not math.isfinite(float(v)):
            raise PermanentElasticsearchPayloadError(
                f"embedding[{i}] is not finite",
            )
    return [float(v) for v in vector]


def validate_uuid_field(
    value: Optional[str],
    label: str,
) -> Optional[UUID]:
    """Parse a UUID string or raise PermanentElasticsearchPayloadError."""
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        raise PermanentElasticsearchPayloadError(
            f"{label}: invalid UUID",
        )


def validate_payload_cross_check(
    payload: dict[str, Any] | ElasticsearchPayload,
    *,
    envelope_idempotency_key: str,
    envelope_aggregate_id: str,
    envelope_event_type: str,
    envelope_schema_version: int,
    envelope_target: str,
) -> ElasticsearchPayload:
    """Validate payload and cross-check envelope fields.

    Raises PermanentProjectionError (via the caller) for all data errors.
    """
    from app.memory_core.projection_worker.contracts import (
        PermanentProjectionError,
    )
    from app.memory_core.repositories.postgres_outbox import (
        build_idempotency_key,
    )

    try:
        if isinstance(payload, ElasticsearchPayload):
            parsed = payload
        else:
            parsed = ElasticsearchPayload.model_validate(payload)
    except Exception:
        raise PermanentProjectionError(
            "elasticsearch projection: malformed payload",
        )

    if parsed.event_type != envelope_event_type:
        raise PermanentProjectionError(
            "elasticsearch projection: event_type mismatch",
        )
    if parsed.schema_version != envelope_schema_version:
        raise PermanentProjectionError(
            "elasticsearch projection: schema_version mismatch",
        )
    if parsed.projection_target != envelope_target:
        raise PermanentProjectionError(
            "elasticsearch projection: projection_target mismatch",
        )
    if parsed.event_type != EVENT_TYPE_REMEMBERED:
        raise PermanentProjectionError(
            "elasticsearch projection: unsupported event_type",
        )
    if parsed.schema_version != SCHEMA_VERSION:
        raise PermanentProjectionError(
            "elasticsearch projection: unsupported schema_version",
        )
    if parsed.aggregate_type != AGGREGATE_TYPE:
        raise PermanentProjectionError(
            "elasticsearch projection: unsupported aggregate_type",
        )
    if parsed.projection_target != "elasticsearch":
        raise PermanentProjectionError(
            "elasticsearch projection: unsupported target",
        )
    if parsed.aggregate_id != envelope_aggregate_id:
        raise PermanentProjectionError(
            "elasticsearch projection: aggregate_id mismatch",
        )

    try:
        agg_uuid = UUID(parsed.aggregate_id)
    except (TypeError, ValueError):
        raise PermanentProjectionError(
            "elasticsearch projection: aggregate_id is not a valid UUID",
        )

    expected_idem = build_idempotency_key(agg_uuid, "elasticsearch")
    if expected_idem != envelope_idempotency_key:
        raise PermanentProjectionError(
            "elasticsearch projection: idempotency_key mismatch",
        )

    if parsed.memory_snapshot.id != parsed.aggregate_id:
        raise PermanentProjectionError(
            "elasticsearch projection: snapshot.id does not match aggregate_id",
        )

    snap = parsed.memory_snapshot
    for label, val in [
        ("snapshot.project_id", snap.project_id),
        ("snapshot.session_id", snap.session_id),
        ("snapshot.agent_id", snap.agent_id),
        ("snapshot.knowledge_base_id", snap.knowledge_base_id),
    ]:
        validate_uuid_field(val, label)

    return parsed


__all__ = [
    "ElasticsearchPayload",
    "ElasticsearchSearchResult",
    "PermanentElasticsearchPayloadError",
    "SearchFilters",
    "validate_embedding",
    "validate_payload_cross_check",
    "validate_uuid_field",
]
