from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.memory_core.world_memory_event_history_diagnostics import EventHistoryTimingSample

COORDINATOR_ENDPOINT_PATH: Final[str] = "/internal/world-memory/event-history/query"
COORDINATOR_STATS_PATH: Final[str] = "/internal/world-memory/event-history/stats"
EVENT_HISTORY_COORDINATOR_UNAVAILABLE: Final[str] = "event_history_authority_unavailable"


class EventHistorySingleflightStats(BaseModel):
    """Typed read-only snapshot of the coordinator singleflight runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    leader_count: int = Field(default=0, ge=0, description="Lifetime leader tasks created")
    in_flight: int = Field(default=0, ge=0, description="Live non-completed query tasks")
    join_count: int = Field(default=0, ge=0, description="Lifetime callers joining a live task")
    last_sequence: int = Field(default=0, ge=0, description="Latest completed caller sample")
    timing_samples: tuple[EventHistoryTimingSample, ...] = ()


class EventHistoryCoordinatorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(..., description="Trimmed nonblank historical query")
    event_time_lte: AwareDatetime
    kind: str | None = None
    aggregate_id: UUID | None = None
    require_evidence: bool = False
    max_items: int = Field(10, ge=1, le=200)

    @field_validator("query")
    @classmethod
    def _trim_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must be nonblank")
        return normalized


class EventHistoryTemporalTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_occurred_at: datetime
    observed_at: datetime | None = None
    ingested_at: datetime | None = None
    validity_start: datetime | None = None
    validity_end: datetime | None = None
    created_at: datetime | None = None
    verified_at: datetime | None = None


class PreparedEventHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    envelope: dict[str, Any]
    kind: str
    id: UUID
    content: str
    evidence_ids: tuple[UUID, ...] = ()
    temporal: EventHistoryTemporalTrace


class EventHistoryCoordinatorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[PreparedEventHistoryEntry, ...] = ()
    candidate_count: int = Field(default=0, ge=0)
    candidate_latency_ms: float = Field(default=0.0, ge=0)
    refusal_reason: str | None = None


__all__ = [
    "COORDINATOR_ENDPOINT_PATH",
    "COORDINATOR_STATS_PATH",
    "EVENT_HISTORY_COORDINATOR_UNAVAILABLE",
    "EventHistoryCoordinatorRequest",
    "EventHistoryCoordinatorResponse",
    "EventHistorySingleflightStats",
    "EventHistoryTemporalTrace",
    "PreparedEventHistoryEntry",
]
