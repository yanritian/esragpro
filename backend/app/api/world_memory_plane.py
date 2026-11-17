"""
World-Memory Plane API Router (P4-W)

Provides unauthenticated HTTP endpoints for the World-Memory contract.
These endpoints do NOT require PrincipalContext, ACL, OAuth, or scope checks.

Data dimensions (project_id, session_id, agent_id) are for organization and
query scoping - NOT for permission checking.

Endpoints:
- POST /world-memory/remember - Store a P7-B world-memory envelope
- POST /world-memory/query - Query world-memory items, return typed context pack

Storage
-------
When the memory-plane PostgreSQL session factory is configured
(``memory_plane_session.is_configured()``), this module uses
:class:`WorldMemoryAuthorityRepository` as the durable authority. When
the session factory is not configured (isolated unit tests), it falls
back to the explicit in-memory :class:`_ReferenceStore`. The fallback
is a test seam only and is never silently claimed to be durable.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, HTTPException, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.memory_core.world_memory import (
    WorldContextItem,
    WorldContextItemKind,
)
from app.memory_core.world_memory_event_history_diagnostics import (
    EventHistoryDiagnosticSink,
    EventHistoryRetrievalTiming,
)
from app.memory_core.world_memory_replay import (
    EnvelopeValidationError,
    validate_world_memory_envelope,
)
from app.memory_core.world_memory_serializer import (
    deserialize_world_memory,
)

router = APIRouter(
    prefix="/world-memory",
    tags=["world-memory"],
)

# Scenario labels accepted by POST /world-memory/scenario-context. A label
# identifies the downstream consumer class only (P9-A); it never encodes a
# mission, platform, controller, decision policy, or safety logic.
ScenarioContextLabel = Literal["rescue", "embodied", "autonomous_driving"]


# ============================================================================
# Reference in-memory store (TEST/DEV seam — NOT durable)
# ============================================================================


class _StoredEnvelope:
    """Internal record stored in the reference store."""

    __slots__ = (
        "aggregate_id",
        "envelope",
        "event_type",
        "idempotency_key",
        "kind",
        "occurred_at",
        "stored_at",
    )

    def __init__(
        self,
        *,
        idempotency_key: str,
        aggregate_id: UUID,
        event_type: str,
        occurred_at: datetime,
        kind: str,
        envelope: dict[str, Any],
    ) -> None:
        self.idempotency_key = idempotency_key
        self.aggregate_id = aggregate_id
        self.event_type = event_type
        self.occurred_at = occurred_at
        self.kind = kind
        self.envelope = envelope
        self.stored_at = datetime.now(timezone.utc)


class _ReferenceStore:
    """Thread-safe in-memory store for world-memory envelopes.

    NOT durable, NOT multi-process. This is a reference/test seam only.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Keyed by idempotency_key for dedup
        self._by_key: dict[str, _StoredEnvelope] = {}
        # Ordered list for iteration
        self._items: list[_StoredEnvelope] = []

    def store(self, envelope_dict: dict[str, Any]) -> dict[str, Any]:
        """Validate and store a P7-B envelope.

        Returns the stored envelope dict (with metadata).
        Idempotent: exact duplicate (same key, same payload) returns the
        existing record. Conflicting reuse of the same key raises.
        """
        validated = validate_world_memory_envelope(envelope_dict)
        key = validated.idempotency_key

        with self._lock:
            existing = self._by_key.get(key)
            if existing is not None:
                # Exact duplicate — return existing
                if existing.envelope == envelope_dict:
                    return existing.envelope
                # Conflicting duplicate
                raise ValueError(f"Idempotency key {key!r} reused with conflicting payload")

            record = _StoredEnvelope(
                idempotency_key=key,
                aggregate_id=validated.aggregate_id,
                event_type=validated.event_type,
                occurred_at=validated.occurred_at,
                kind=validated.world_memory_snapshot.get("_kind", "unknown"),
                envelope=envelope_dict,
            )
            self._by_key[key] = record
            self._items.append(record)

        return envelope_dict

    def query(
        self,
        *,
        kind: str | None = None,
        aggregate_id: UUID | None = None,
        event_time_lte: datetime | None = None,
        require_evidence: bool = False,
        max_items: int | None = None,
    ) -> list[_StoredEnvelope]:
        """Return matching stored envelopes.

        When *max_items* is ``None`` all matching envelopes are returned
        (used when the caller needs to apply a secondary filter such as
        ``require_evidence`` before capping the result count).
        """
        with self._lock:
            items = list(self._items)

        results: list[_StoredEnvelope] = []
        for item in items:
            if kind is not None and item.kind != kind:
                continue
            if aggregate_id is not None and item.aggregate_id != aggregate_id:
                continue
            if event_time_lte is not None:
                item_event_time = (
                    item.occurred_at.replace(tzinfo=timezone.utc)
                    if item.occurred_at.tzinfo is None
                    else item.occurred_at.astimezone(timezone.utc)
                )
                if item_event_time > event_time_lte:
                    continue
            if require_evidence and not _has_evidence(item):
                continue
            results.append(item)
            if max_items is not None and len(results) >= max_items:
                break

        return results

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def state_at(
        self,
        *,
        aggregate_id: UUID,
        valid_at: datetime,
    ) -> list[_StoredEnvelope]:
        from app.memory_core.repositories.world_memory_authority import (
            resolve_state_at_candidates,
        )

        candidates = self.query(
            kind="state",
            aggregate_id=aggregate_id,
            require_evidence=False,
            max_items=None,
        )
        return resolve_state_at_candidates(candidates, valid_at=valid_at)

    def clear(self) -> None:
        """Clear the store (test helper)."""
        with self._lock:
            self._by_key.clear()
            self._items.clear()


def _has_evidence(item: Any) -> bool:
    """Check whether a stored envelope carries evidence.

    Accepts either a reference-store ``_StoredEnvelope`` or a
    :class:`StoredEnvelope` from the authority repository — both expose
    an ``envelope`` dict attribute.

    Evidence can be:
    - ``raw_evidence_ref`` on observations
    - ``evidence_ids`` on states, hypotheses, facts
    - ``observation_ids`` on states (indirect evidence via observations)
    """
    envelope_dict = getattr(item, "envelope", {}) or {}
    snapshot = envelope_dict.get("world_memory_snapshot", {})
    # Direct evidence_ids
    evidence_ids = snapshot.get("evidence_ids")
    if isinstance(evidence_ids, list) and len(evidence_ids) > 0:
        return True
    # Observation-level raw_evidence_ref. Only a nonblank string counts as
    # evidence — a whitespace-only ref would pass a ``!= ""`` check while the
    # public evidence-ID mapper (``_extract_domain_evidence_ids``) rejects it,
    # yielding an evidence-required item with empty evidence_ids.
    raw_ref = snapshot.get("raw_evidence_ref")
    if isinstance(raw_ref, str) and raw_ref.strip():
        return True
    # State-level observation_ids (indirect evidence via observations)
    obs_ids = snapshot.get("observation_ids")
    return isinstance(obs_ids, list) and len(obs_ids) > 0


# Module-level reference store
_REFERENCE_STORE = _ReferenceStore()


def _get_store() -> _ReferenceStore:
    """Return the reference store (test seam)."""
    return _REFERENCE_STORE


# ============================================================================
# PostgreSQL authority seam (P6-A)
# ============================================================================


def _is_pg_authority_configured() -> bool:
    """Return True when the memory-plane PostgreSQL session is available."""
    try:
        from app.memory_core import memory_plane_session as _mps

        return _mps.is_configured()
    except (ImportError, RuntimeError):
        return False


async def _pg_remember(
    envelope_dict: dict[str, Any],
) -> dict[str, Any]:
    """Persist via PostgreSQL authority + default projection outbox atomically.

    Both the authority row and the default per-target projection rows are
    created in the same transaction. A rollback leaves neither.
    """
    from sqlalchemy import select

    from app.db.models.world_memory import WorldMemoryEnvelope
    from app.memory_core import memory_plane_session as _mps
    from app.memory_core.repositories.world_memory_authority import (
        WorldMemoryAuthorityRepository,
        WorldMemoryIdempotencyConflict,
    )
    from app.memory_core.repositories.world_memory_projection_outbox import (
        DEFAULT_WORLD_PROJECTION_TARGETS,
        WorldMemoryProjectionOutboxRepository,
    )

    state = _mps._state()
    session_factory = state["session_factory"]
    if session_factory is None:
        await _mps.init_session_factory()
        state = _mps._state()
        session_factory = state["session_factory"]
    if session_factory is None:
        raise RuntimeError("world_memory_plane: session factory unavailable after init")

    session = session_factory()
    try:
        authority_repo = WorldMemoryAuthorityRepository(session)
        stored = await authority_repo.remember(envelope_dict)

        # Retrieve the ORM row for the projection outbox repository
        result = await session.execute(
            select(WorldMemoryEnvelope).where(
                WorldMemoryEnvelope.idempotency_key == stored.idempotency_key
            )
        )
        orm_row = result.scalar_one()

        projection_repo = WorldMemoryProjectionOutboxRepository(session)
        await projection_repo.enqueue_for_targets(
            orm_row,
            targets=DEFAULT_WORLD_PROJECTION_TARGETS,
        )

        await session.commit()
        return stored.envelope
    except WorldMemoryIdempotencyConflict as exc:
        await session.rollback()
        raise ValueError(str(exc)) from exc
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def _pg_query(
    *,
    kind: str | None,
    aggregate_id: UUID | None,
    event_time_lte: datetime | None = None,
    require_evidence: bool,
    max_items: int | None,
) -> list[Any]:
    """Query via PostgreSQL authority. Returns StoredEnvelope instances."""
    from app.memory_core import memory_plane_session as _mps
    from app.memory_core.repositories.world_memory_authority import (
        WorldMemoryAuthorityRepository,
    )

    state = _mps._state()
    session_factory = state["session_factory"]
    if session_factory is None:
        await _mps.init_session_factory()
        state = _mps._state()
        session_factory = state["session_factory"]
    if session_factory is None:
        raise RuntimeError("world_memory_plane: session factory unavailable after init")

    session = session_factory()
    try:
        repo = WorldMemoryAuthorityRepository(session)
        if require_evidence:
            # Scan all matching items before applying the evidence filter
            # and max_items cap, so evidence-backed rows are never silently
            # dropped by an early limit.
            results = await repo.query(
                kind=kind,
                aggregate_id=aggregate_id,
                event_time_lte=event_time_lte,
                require_evidence=False,
                max_items=None,
            )
            results = [r for r in results if _has_evidence(r)]
            if max_items is not None:
                results = results[:max_items]
        else:
            results = await repo.query(
                kind=kind,
                aggregate_id=aggregate_id,
                event_time_lte=event_time_lte,
                require_evidence=False,
                max_items=max_items,
            )
        return results
    finally:
        await session.close()


async def _pg_state_at(
    *,
    aggregate_id: UUID,
    valid_at: datetime,
) -> list[Any]:
    from app.memory_core import memory_plane_session as _mps
    from app.memory_core.repositories.world_memory_authority import (
        WorldMemoryAuthorityRepository,
    )

    state = _mps._state()
    session_factory = state["session_factory"]
    if session_factory is None:
        await _mps.init_session_factory()
        state = _mps._state()
        session_factory = state["session_factory"]
    if session_factory is None:
        raise RuntimeError("world_memory_plane: session factory unavailable after init")

    session = session_factory()
    try:
        return await WorldMemoryAuthorityRepository(session).resolve_state_at(
            aggregate_id=aggregate_id,
            valid_at=valid_at,
        )
    finally:
        await session.close()


async def _pg_query_composed(
    *,
    query: str,
    kind: str | None,
    aggregate_id: UUID | None,
    max_items: int,
) -> tuple[list[Any], list[WorldMemorySourceTrace], float]:
    """Run a nonblank candidate query through the P6-G coordinator.

    Uses a single authority repository/session so candidate rehydration
    reads the same PostgreSQL snapshot. Returns only coordinator-
    rehydrated PostgreSQL envelopes, per-source trace metadata, and the
    nonnegative total retrieval latency.

    Candidate stores propose object IDs only; their payload content never
    enters this function's return value.
    """
    from app.memory_core import memory_plane_session as _mps
    from app.memory_core.repositories.world_memory_authority import (
        WorldMemoryAuthorityRepository,
    )
    from app.memory_core.world_memory_candidate_runtime import (
        get_world_memory_candidate_sources,
    )
    from app.memory_core.world_memory_candidate_source import (
        WorldMemoryCandidateQuery,
    )
    from app.memory_core.world_memory_query_coordinator import (
        WorldMemoryQueryCoordinator,
    )

    state = _mps._state()
    session_factory = state["session_factory"]
    if session_factory is None:
        await _mps.init_session_factory()
        state = _mps._state()
        session_factory = state["session_factory"]
    if session_factory is None:
        raise RuntimeError("world_memory_plane: session factory unavailable after init")

    session = session_factory()
    try:
        authority_repo = WorldMemoryAuthorityRepository(session)
        sources = await get_world_memory_candidate_sources()
        coordinator = WorldMemoryQueryCoordinator(authority_repo, sources)
        result = await coordinator.query(
            WorldMemoryCandidateQuery(
                query=query,
                kind=kind,
                aggregate_id=aggregate_id,
                max_items=max_items,
            )
        )
    finally:
        await session.close()

    traces: list[WorldMemorySourceTrace] = [
        WorldMemorySourceTrace(
            source=source_result.kind.value,
            status="ok" if source_result.error is None else "degraded",
            candidate_count=len(source_result.proposals),
            latency_ms=max(0.0, source_result.latency_ms),
            error=(str(source_result.error) if source_result.error is not None else None),
        )
        for source_result in result.sources
    ]
    return list(result.envelopes), traces, max(0.0, result.latency_ms)


async def _pg_event_history_query(
    *,
    query: str,
    event_time_lte: datetime,
    kind: str | None,
    aggregate_id: UUID | None,
    require_evidence: bool,
    max_items: int,
    diagnostic_sink: EventHistoryDiagnosticSink | None = None,
) -> tuple[list[Any], list[WorldMemorySourceTrace], float, str | None]:
    from app.core.config import settings
    from app.memory_core import memory_plane_session as _mps
    from app.memory_core.repositories.world_memory_authority import (
        WorldMemoryAuthorityRepository,
    )
    from app.memory_core.world_memory_event_history_candidates import (
        EventHistoryCandidateQuery,
        EventHistoryElasticsearchCandidateSource,
        EventHistoryQueryCoordinator,
    )
    from app.memory_core.world_memory_event_history_readiness import (
        EventHistoryReadiness,
        EventHistoryReadinessGate,
    )
    from app.memory_core.world_memory_event_history_runtime import (
        EventHistoryQueryExecution,
        EventHistoryQueryKey,
        assess_world_memory_event_history_readiness,
        coalesce_world_memory_event_history_query,
        get_world_memory_event_history_client,
    )

    started = perf_counter()

    def report_timing(execution: EventHistoryQueryExecution) -> None:
        if diagnostic_sink is None:
            return
        diagnostic_sink.record_retrieval_timing(
            EventHistoryRetrievalTiming(
                retrieval_ms=max(0.0, execution.retrieval_latency_ms),
                readiness_ms=max(0.0, execution.readiness_latency_ms),
                candidate_ms=max(0.0, execution.candidate_latency_ms),
                authority_ms=max(0.0, execution.authority_latency_ms),
                query_unattributed_ms=max(0.0, execution.query_unattributed_ms),
            )
        )

    client = await get_world_memory_event_history_client()
    if client is None:
        refusal_reason = "event_history_disabled"
        report_timing(
            EventHistoryQueryExecution(
                authorities=(),
                retrieval_latency_ms=max(0.0, (perf_counter() - started) * 1000.0),
                refusal_reason=refusal_reason,
            )
        )
        return (
            [],
            [
                WorldMemorySourceTrace(
                    source="elasticsearch_event_history",
                    status="refused",
                    error=refusal_reason,
                )
            ],
            (perf_counter() - started) * 1000.0,
            refusal_reason,
        )

    state = _mps._state()
    session_factory = state["session_factory"]
    if session_factory is None:
        await _mps.init_session_factory()
        session_factory = _mps._state()["session_factory"]
    if session_factory is None:
        raise RuntimeError("world_memory_plane: session factory unavailable after init")

    async def execute_query() -> EventHistoryQueryExecution:
        async def assess_readiness() -> EventHistoryReadiness:
            async with session_factory() as readiness_session:
                readiness_authority_repo = WorldMemoryAuthorityRepository(readiness_session)
                return await EventHistoryReadinessGate(
                    enabled=settings.WORLD_MEMORY_EVENT_HISTORY_ENABLED,
                    authority_repository=readiness_authority_repo,
                    client=client,
                    index_name=settings.WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME,
                ).assess()

        readiness_started = perf_counter()
        readiness = await assess_world_memory_event_history_readiness(assess_readiness)
        readiness_latency_ms = max(0.0, (perf_counter() - readiness_started) * 1000.0)
        if not readiness.is_ready:
            return EventHistoryQueryExecution(
                authorities=(),
                retrieval_latency_ms=readiness_latency_ms,
                readiness_latency_ms=readiness_latency_ms,
                refusal_reason=f"event_history_{readiness.status.value}",
            )

        session = session_factory()
        try:
            authority_repo = WorldMemoryAuthorityRepository(session)
            result = await EventHistoryQueryCoordinator(
                authority_repo,
                EventHistoryElasticsearchCandidateSource(
                    client,
                    index_name=settings.WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME,
                ),
            ).query(
                EventHistoryCandidateQuery(
                    query=query,
                    event_time_lte=event_time_lte,
                    kind=kind,
                    aggregate_id=aggregate_id,
                    require_evidence=require_evidence,
                    max_items=max_items,
                )
            )
        finally:
            await session.close()

        source = result.source
        retrieval_latency_ms = max(0.0, (perf_counter() - readiness_started) * 1000.0)
        candidate_latency_ms = 0.0 if source is None else max(0.0, source.latency_ms)
        if source is not None and source.error is not None:
            return EventHistoryQueryExecution(
                authorities=(),
                candidate_latency_ms=candidate_latency_ms,
                retrieval_latency_ms=retrieval_latency_ms,
                readiness_latency_ms=readiness_latency_ms,
                authority_latency_ms=max(0.0, result.authority_latency_ms),
                query_unattributed_ms=max(0.0, result.query_unattributed_ms),
                refusal_reason=f"event_history_candidate_{source.error.category}",
            )
        if require_evidence and source is not None and source.proposals and not result.items:
            return EventHistoryQueryExecution(
                authorities=(),
                candidate_count=len(source.proposals),
                candidate_latency_ms=candidate_latency_ms,
                retrieval_latency_ms=retrieval_latency_ms,
                readiness_latency_ms=readiness_latency_ms,
                authority_latency_ms=max(0.0, result.authority_latency_ms),
                query_unattributed_ms=max(0.0, result.query_unattributed_ms),
                refusal_reason="insufficient_evidence",
            )
        return EventHistoryQueryExecution(
            authorities=tuple(item.authority for item in result.items),
            candidate_count=(0 if source is None else len(source.proposals)),
            candidate_latency_ms=candidate_latency_ms,
            retrieval_latency_ms=retrieval_latency_ms,
            readiness_latency_ms=readiness_latency_ms,
            authority_latency_ms=max(0.0, result.authority_latency_ms),
            query_unattributed_ms=max(0.0, result.query_unattributed_ms),
        )

    execution = await coalesce_world_memory_event_history_query(
        EventHistoryQueryKey(
            query=query,
            event_time_lte=event_time_lte,
            kind=kind,
            aggregate_id=aggregate_id,
            require_evidence=require_evidence,
            max_items=max_items,
        ),
        execute_query,
    )
    report_timing(execution)
    if execution.refusal_reason is not None:
        return (
            [],
            [
                WorldMemorySourceTrace(
                    source="elasticsearch_event_history",
                    status="refused",
                    candidate_count=execution.candidate_count,
                    latency_ms=execution.candidate_latency_ms,
                    error=execution.refusal_reason,
                )
            ],
            (perf_counter() - started) * 1000.0,
            execution.refusal_reason,
        )
    return (
        list(execution.authorities),
        [
            WorldMemorySourceTrace(
                source="elasticsearch_event_history",
                status="ok",
                candidate_count=execution.candidate_count,
                latency_ms=execution.candidate_latency_ms,
            )
        ],
        (perf_counter() - started) * 1000.0,
        None,
    )


# ============================================================================
# Pydantic request/response models
# ============================================================================


class WorldMemoryRememberRequest(BaseModel):
    """Request body for POST /world-memory/remember.

    The envelope must be a P7-B envelope dict. Validation is performed by
    the P7-B envelope validator before storage.
    """

    envelope: dict[str, Any] = Field(
        ..., description="P7-B world-memory envelope (from build_world_memory_envelope)"
    )


class WorldMemoryRememberResponse(BaseModel):
    """Response for POST /world-memory/remember."""

    success: bool = True
    idempotency_key: str
    aggregate_id: str
    kind: str
    event_type: str
    message: str = "World-memory envelope stored successfully"


class WorldMemoryQueryRequest(BaseModel):
    """Request body for POST /world-memory/query."""

    kind: str | None = Field(
        None, description="Filter by item kind (observation, state, hypothesis, fact, context_pack)"
    )
    aggregate_id: str | None = Field(None, description="Filter by aggregate UUID")
    require_evidence: bool = Field(False, description="Only return items with evidence")
    max_items: int = Field(10, ge=1, le=200, description="Maximum items to return")
    token_budget: int = Field(4000, ge=10, le=100000, description="Token budget for context pack")
    event_time_lte: AwareDatetime | None = Field(
        None,
        description=(
            "Inclusive UTC event-time cutoff over envelope occurred_at. "
            "Nonblank queries may use the optional internal history coordinator."
        ),
    )
    query: str = Field(
        "",
        description=(
            "Natural-language candidate query (P6-G read composition). "
            "A blank query preserves the existing direct-query semantics."
        ),
    )


class WorldMemoryStateAtRequest(BaseModel):
    aggregate_id: str = Field(..., description="Aggregate UUID to resolve")
    valid_at: AwareDatetime = Field(
        ..., description="World-state validity instant, using the half-open interval [start, end)"
    )
    require_evidence: bool = Field(False, description="Only return evidence-backed state")
    token_budget: int = Field(4000, ge=10, le=100000, description="Token budget for context")


class WorldMemoryTemporalTrace(BaseModel):
    """Source-provided temporal provenance for one retrieved memory item."""

    event_occurred_at: datetime = Field(
        description="World/event time from the validated envelope header"
    )
    observed_at: datetime | None = Field(
        None, description="Observation time when the source item is an observation"
    )
    ingested_at: datetime | None = Field(
        None, description="Receipt time when the source item is an observation"
    )
    validity_start: datetime | None = Field(
        None, description="Start of a source state validity interval"
    )
    validity_end: datetime | None = Field(
        None, description="End of a source state validity interval"
    )
    created_at: datetime | None = Field(
        None, description="Source creation time for a hypothesis or context pack"
    )
    verified_at: datetime | None = Field(
        None, description="Source verification time for a verified fact"
    )


class WorldMemoryContextItemResponse(BaseModel):
    """A single world-memory context item in the response."""

    kind: str
    id: str
    content: str
    evidence_ids: list[str] = Field(default_factory=list)
    temporal: WorldMemoryTemporalTrace


class WorldMemorySourceTrace(BaseModel):
    """Trace metadata for one candidate source participating in a query (P6-G).

    ``source`` serializes as the projection identity (``elasticsearch``
    or ``neo4j``). ``error`` is a stable diagnostic string only and never
    carries candidate payload content.
    """

    source: str
    status: str
    candidate_count: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0.0, ge=0)
    error: str | None = None


class WorldMemoryQueryResponse(BaseModel):
    """Response for POST /world-memory/query."""

    query_id: str
    items: list[WorldMemoryContextItemResponse]
    envelopes: list[dict[str, Any]]
    token_budget: int
    token_used: int
    status: str
    refusal_reason: str | None = None
    candidate_sources: list[WorldMemorySourceTrace] = Field(
        default_factory=list,
        description="Per-source candidate trace metadata (P6-G)",
    )
    latency_ms: float = Field(
        default=0.0,
        ge=0,
        description="Total retrieval latency in milliseconds (P6-G)",
    )


class WorldMemoryScenarioContextRequest(BaseModel):
    """Request body for POST /world-memory/scenario-context (P9-A).

    Accepts only ``scenario``, ``query``, ``aggregate_id``, ``max_items``,
    and ``token_budget``. Any unknown/extra field — including a caller-owned
    ``require_evidence`` override — is rejected at the request boundary.
    Scenario context always retrieves with ``require_evidence=True`` and the
    flag is not caller-configurable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: ScenarioContextLabel = Field(
        ...,
        description="Scenario label identifying the downstream consumer class",
    )
    query: str = Field(
        "",
        description=(
            "Natural-language candidate query (P6-G read composition). "
            "A blank query preserves the existing direct-query semantics."
        ),
    )
    aggregate_id: str | None = Field(None, description="Filter by aggregate UUID")
    max_items: int = Field(10, ge=1, le=200, description="Maximum items to return")
    token_budget: int = Field(4000, ge=10, le=100000, description="Token budget for context pack")
    event_time_lte: AwareDatetime | None = Field(
        None,
        description=(
            "Inclusive UTC event-time cutoff over envelope occurred_at. "
            "Supported only when query is blank."
        ),
    )


class WorldMemoryScenarioContextResponse(BaseModel):
    """Response for POST /world-memory/scenario-context (P9-A).

    Passive, read-only contract: this response carries retrieved context
    data only and never contains an action, command, plan, prediction,
    safety decision, or control signal. The nested query result (``items``,
    ``envelopes``, ``status``, ``refusal_reason``, ``candidate_sources``,
    ``latency_ms``) is preserved exactly from the underlying
    ``world_memory_query`` call.
    """

    model_config = ConfigDict(frozen=True)

    scenario: ScenarioContextLabel = Field(..., description="Selected scenario label")
    passive: bool = Field(
        default=True,
        description="Contract metadata: context data only, never an inference or command",
    )
    read_only: bool = Field(
        default=True,
        description="Contract metadata: retrieval only, never mutates world memory",
    )
    items: list[WorldMemoryContextItemResponse]
    envelopes: list[dict[str, Any]]
    status: str
    refusal_reason: str | None = None
    candidate_sources: list[WorldMemorySourceTrace] = Field(
        default_factory=list,
        description="Per-source candidate trace metadata (P6-G)",
    )
    latency_ms: float = Field(
        default=0.0,
        ge=0,
        description="Total retrieval latency in milliseconds (P6-G)",
    )


# ============================================================================
# Helper: extract WorldContextItem from a validated envelope
# ============================================================================

# Dedicated namespace for deriving a stable evidence UUID from an
# observation's ``raw_evidence_ref``. The raw reference is a URI-like string,
# not a UUID; mapping it deterministically lets the response surface an
# evidence ID for evidence-backed observations while preserving the UUID
# contract of ``WorldContextItem.evidence_ids``. The namespace itself is
# derived from the URL namespace so it is stable across processes.
_EVIDENCE_REF_NAMESPACE = uuid5(NAMESPACE_URL, "esragpro:world-memory:raw-evidence-ref")


def _extract_domain_evidence_ids(domain_obj: Any) -> tuple[UUID, ...]:
    """Return stable evidence IDs for a deserialized P7 domain object.

    Observations carry evidence through ``raw_evidence_ref`` (a URI-like
    string, not a UUID); it is mapped to a deterministic namespace UUID so an
    evidence-backed observation reports a readable evidence ID. States and
    hypotheses keep their explicit ``evidence_ids``/``observation_ids``.
    """
    raw_ref = getattr(domain_obj, "raw_evidence_ref", None)
    if isinstance(raw_ref, str) and raw_ref.strip():
        return (uuid5(_EVIDENCE_REF_NAMESPACE, raw_ref),)
    evidence_ids = getattr(domain_obj, "evidence_ids", ()) or getattr(
        domain_obj, "observation_ids", ()
    )
    return tuple(evidence_ids) if evidence_ids else ()


def _extract_context_items(envelope_dict: dict[str, Any]) -> list[WorldContextItem]:
    """Extract WorldContextItem(s) from a validated envelope.

    Uses the P7-B serializer's ``deserialize_world_memory`` dispatcher to
    materialize the domain object, then wraps it in a WorldContextItem.
    """
    snapshot = envelope_dict.get("world_memory_snapshot", {})
    kind_str = snapshot.get("_kind", "")

    try:
        domain_obj = deserialize_world_memory(snapshot)
    except (ValueError, KeyError):
        # Malformed snapshot — skip
        return []

    # Map domain object to WorldContextItem
    kind_map = {
        "observation": WorldContextItemKind.OBSERVATION,
        "state": WorldContextItemKind.STATE,
        "hypothesis": WorldContextItemKind.HYPOTHESIS,
        "fact": WorldContextItemKind.FACT,
    }

    wm_kind = kind_map.get(kind_str)
    if wm_kind is None:
        # context_pack, transition_record, revocation_record — not queryable items
        return []

    # Extract content and evidence from the domain object
    content = getattr(domain_obj, "content", "")
    evidence_ids = _extract_domain_evidence_ids(domain_obj)
    item_id = getattr(domain_obj, "id", uuid4())

    return [
        WorldContextItem(
            kind=wm_kind,
            id=item_id,
            content=content,
            evidence_ids=evidence_ids,
        )
    ]


def _extract_temporal_trace(
    envelope_dict: dict[str, Any],
) -> WorldMemoryTemporalTrace:
    """Preserve source timestamps from one validated P7-B envelope."""
    snapshot = envelope_dict["world_memory_snapshot"]
    validity = snapshot.get("validity")
    validity_start = validity.get("start") if isinstance(validity, dict) else None
    validity_end = validity.get("end") if isinstance(validity, dict) else None

    return WorldMemoryTemporalTrace(
        event_occurred_at=datetime.fromisoformat(
            str(envelope_dict["occurred_at"]).replace("Z", "+00:00")
        ),
        observed_at=(
            datetime.fromisoformat(str(snapshot["observed_at"]).replace("Z", "+00:00"))
            if snapshot.get("observed_at") is not None
            else None
        ),
        ingested_at=(
            datetime.fromisoformat(str(snapshot["ingested_at"]).replace("Z", "+00:00"))
            if snapshot.get("ingested_at") is not None
            else None
        ),
        validity_start=(
            datetime.fromisoformat(str(validity_start).replace("Z", "+00:00"))
            if validity_start is not None
            else None
        ),
        validity_end=(
            datetime.fromisoformat(str(validity_end).replace("Z", "+00:00"))
            if validity_end is not None
            else None
        ),
        created_at=(
            datetime.fromisoformat(str(snapshot["created_at"]).replace("Z", "+00:00"))
            if snapshot.get("created_at") is not None
            else None
        ),
        verified_at=(
            datetime.fromisoformat(str(snapshot["verified_at"]).replace("Z", "+00:00"))
            if snapshot.get("verified_at") is not None
            else None
        ),
    )


def _coordinator_temporal_to_public_temporal(
    temporal: Any,
) -> WorldMemoryTemporalTrace:
    """Map a coordinator temporal trace onto the public response model.

    The coordinator has already validated the temporal trace; this direct
    field mapping avoids a redundant ``model_dump``/``model_validate``
    round trip per returned entry.
    """
    return WorldMemoryTemporalTrace(
        event_occurred_at=temporal.event_occurred_at,
        observed_at=temporal.observed_at,
        ingested_at=temporal.ingested_at,
        validity_start=temporal.validity_start,
        validity_end=temporal.validity_end,
        created_at=temporal.created_at,
        verified_at=temporal.verified_at,
    )


def _query_matches(query: str, envelope_dict: dict[str, Any]) -> bool:
    """Deterministic case-insensitive all-term match (in-memory test seam).

    Every non-empty whitespace-separated term must appear in the
    lowercased extracted context-item content. A query with no usable
    terms never matches.
    """
    terms = [term for term in query.lower().split() if term]
    if not terms:
        return False
    haystack = " ".join(item.content for item in _extract_context_items(envelope_dict)).lower()
    return all(term in haystack for term in terms)


async def _world_memory_query_via_event_history_coordinator(
    request: WorldMemoryQueryRequest,
    coordinator_request: Any,
) -> WorldMemoryQueryResponse:
    from app.memory_core.world_memory_event_history_coordinator_client import (
        query_world_memory_event_history_coordinator,
    )

    started = perf_counter()
    coordinator_response = await query_world_memory_event_history_coordinator(
        coordinator_request
    )
    trace_status = "refused" if coordinator_response.refusal_reason is not None else "ok"
    trace = WorldMemorySourceTrace(
        source="elasticsearch_event_history",
        status=trace_status,
        candidate_count=coordinator_response.candidate_count,
        latency_ms=coordinator_response.candidate_latency_ms,
        error=coordinator_response.refusal_reason,
    )
    outer_latency_ms = (perf_counter() - started) * 1000.0
    if coordinator_response.refusal_reason is not None:
        return WorldMemoryQueryResponse(
            query_id=str(uuid4()),
            items=[],
            envelopes=[],
            token_budget=request.token_budget,
            token_used=0,
            status="refused",
            refusal_reason=coordinator_response.refusal_reason,
            candidate_sources=[trace],
            latency_ms=outer_latency_ms,
        )

    budgeted_entries = []
    running_tokens = 0
    evidence_required = request.require_evidence
    saw_evidence_backed_entry = False
    for entry in coordinator_response.entries:
        if evidence_required and not entry.evidence_ids:
            # Fail closed: an evidence-required public response never emits an
            # item with an empty evidence-ID list, even when a coordinator
            # entry carries no stable evidence mapping.
            continue
        saw_evidence_backed_entry = True
        item_tokens = len(entry.content) // 4
        if running_tokens + item_tokens > request.token_budget:
            break
        budgeted_entries.append(entry)
        running_tokens += item_tokens

    if (
        evidence_required
        and coordinator_response.entries
        and not saw_evidence_backed_entry
    ):
        # Every coordinator entry was evidence-less — refuse rather than emit
        # an unverifiable evidence-required response.
        status_str = "refused"
        refusal_reason = "insufficient_evidence"
    else:
        status_str = "ok" if budgeted_entries else "no_match"
        refusal_reason = None if budgeted_entries else "no_matching_items"
    return WorldMemoryQueryResponse(
        query_id=str(uuid4()),
        items=[
            WorldMemoryContextItemResponse(
                kind=entry.kind,
                id=str(entry.id),
                content=entry.content,
                evidence_ids=[str(evidence_id) for evidence_id in entry.evidence_ids],
                temporal=_coordinator_temporal_to_public_temporal(entry.temporal),
            )
            for entry in budgeted_entries
        ],
        envelopes=[entry.envelope for entry in budgeted_entries],
        token_budget=request.token_budget,
        token_used=running_tokens,
        status=status_str,
        refusal_reason=refusal_reason,
        candidate_sources=[trace],
        latency_ms=(perf_counter() - started) * 1000.0,
    )


# ============================================================================
# Endpoints
# ============================================================================


@router.post(
    "/remember",
    response_model=WorldMemoryRememberResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Store a P7-B world-memory envelope",
    description="""
Store a typed world-memory envelope for cross-process retrieval.

The envelope is validated by the P7-B envelope validator. It must have:
- `event_type`: non-empty string (e.g. "world.observation.ingested")
- `schema_version`: must equal 1
- `aggregate_type`: must equal "world_memory"
- `aggregate_id`: valid UUID string
- `projection_target`: non-empty string
- `idempotency_key`: non-empty string (deterministic from aggregate_id + event_type)
- `occurred_at`: valid ISO-8601 datetime
- `world_memory_snapshot`: dict with recognized `_kind` discriminator

This endpoint does NOT require authentication or authorization.

Storage: PostgreSQL authority when configured; in-memory reference store
(test seam) otherwise.
""",
)
async def world_memory_remember(request: WorldMemoryRememberRequest) -> WorldMemoryRememberResponse:
    """Store a P7-B world-memory envelope."""
    if _is_pg_authority_configured():
        try:
            await _pg_remember(request.envelope)
        except EnvelopeValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Envelope validation failed: {exc}",
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            )
    else:
        try:
            _get_store().store(request.envelope)
        except EnvelopeValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Envelope validation failed: {exc}",
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            )

    # Extract metadata for the response
    validated_kind = request.envelope.get("world_memory_snapshot", {}).get("_kind", "unknown")

    return WorldMemoryRememberResponse(
        success=True,
        idempotency_key=request.envelope.get("idempotency_key", ""),
        aggregate_id=request.envelope.get("aggregate_id", ""),
        kind=validated_kind,
        event_type=request.envelope.get("event_type", ""),
    )


@router.post(
    "/query",
    response_model=WorldMemoryQueryResponse,
    summary="Query world-memory items",
    description="""
Query stored world-memory envelopes and return a typed context pack.

The response items are extracted from validated P7-B envelopes using
the P7-B serializer's domain dispatcher. Each item carries its kind
discriminator, ID, content, and evidence IDs.

Fail-closed behavior: when `require_evidence` is True and no
evidence-backed items are found, the status is 'refused'.

This endpoint does NOT require authentication or authorization.
""",
)
async def world_memory_query(request: WorldMemoryQueryRequest) -> WorldMemoryQueryResponse:
    """Query world-memory items and return a typed context pack."""
    aggregate_uuid: UUID | None = None
    if request.aggregate_id:
        try:
            aggregate_uuid = UUID(request.aggregate_id)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid aggregate_id UUID: {request.aggregate_id!r}",
            )

    # Fetch stored items from the configured backend.
    # A nonblank query activates P6-G read composition; a blank query
    # preserves the existing deterministic direct-query semantics.
    candidate_traces: list[WorldMemorySourceTrace] = []
    total_latency_ms: float = 0.0
    query_text = request.query.strip()
    if query_text:
        if request.event_time_lte is not None:
            from app.memory_core.world_memory_event_history_coordinator import (
                EventHistoryCoordinatorRequest,
            )
            from app.memory_core.world_memory_event_history_coordinator_client import (
                world_memory_event_history_coordinator_configured,
            )

            if world_memory_event_history_coordinator_configured():
                return await _world_memory_query_via_event_history_coordinator(
                    request,
                    EventHistoryCoordinatorRequest(
                        query=query_text,
                        event_time_lte=request.event_time_lte,
                        kind=request.kind,
                        aggregate_id=aggregate_uuid,
                        require_evidence=request.require_evidence,
                        max_items=request.max_items,
                    ),
                )
            if not _is_pg_authority_configured():
                refusal_reason = "event_history_authority_unavailable"
                return WorldMemoryQueryResponse(
                    query_id=str(uuid4()),
                    items=[],
                    envelopes=[],
                    token_budget=request.token_budget,
                    token_used=0,
                    status="refused",
                    refusal_reason=refusal_reason,
                    candidate_sources=[
                        WorldMemorySourceTrace(
                            source="elasticsearch_event_history",
                            status="refused",
                            error=refusal_reason,
                        )
                    ],
                )
            composed_envelopes, candidate_traces, total_latency_ms, refusal_reason = (
                await _pg_event_history_query(
                    query=query_text,
                    event_time_lte=request.event_time_lte,
                    kind=request.kind,
                    aggregate_id=aggregate_uuid,
                    require_evidence=request.require_evidence,
                    max_items=request.max_items,
                )
            )
            if refusal_reason is not None:
                return WorldMemoryQueryResponse(
                    query_id=str(uuid4()),
                    items=[],
                    envelopes=[],
                    token_budget=request.token_budget,
                    token_used=0,
                    status="refused",
                    refusal_reason=refusal_reason,
                    candidate_sources=candidate_traces,
                    latency_ms=total_latency_ms,
                )
            stored_items = composed_envelopes
            if request.require_evidence:
                evidence_items = [s for s in composed_envelopes if _has_evidence(s)]
            else:
                evidence_items = composed_envelopes
        elif _is_pg_authority_configured():
            composed_envelopes, candidate_traces, total_latency_ms = await _pg_query_composed(
                query=query_text,
                kind=request.kind,
                aggregate_id=aggregate_uuid,
                max_items=request.max_items,
            )
            stored_items = composed_envelopes
            if request.require_evidence:
                evidence_items = [s for s in composed_envelopes if _has_evidence(s)]
            else:
                evidence_items = composed_envelopes
        else:
            # In-memory test seam: deterministic case-insensitive all-term
            # matching over extracted context-item content. No live candidate
            # sources participate, so no source traces are produced. When
            # require_evidence is true we scan all matching items *before*
            # capping by max_items so evidence-backed matches are never
            # silently dropped by an early limit.
            all_matching = _get_store().query(
                kind=request.kind,
                aggregate_id=aggregate_uuid,
                event_time_lte=request.event_time_lte,
                require_evidence=False,
                max_items=None,
            )
            matched_items = [s for s in all_matching if _query_matches(query_text, s.envelope)]
            stored_items = matched_items
            if request.require_evidence:
                evidence_items = [s for s in matched_items if _has_evidence(s)]
            else:
                evidence_items = matched_items
            evidence_items = evidence_items[: request.max_items]
    elif _is_pg_authority_configured():
        if request.require_evidence:
            all_matching = await _pg_query(
                kind=request.kind,
                aggregate_id=aggregate_uuid,
                require_evidence=False,
                max_items=None,
            )
            evidence_items = [s for s in all_matching if _has_evidence(s)]
            stored_items = all_matching
            evidence_items = evidence_items[: request.max_items]
        else:
            stored_items = await _pg_query(
                kind=request.kind,
                aggregate_id=aggregate_uuid,
                event_time_lte=request.event_time_lte,
                require_evidence=False,
                max_items=request.max_items,
            )
            evidence_items = stored_items
    else:
        # When require_evidence is true we must scan all matching items *before*
        # capping by max_items — otherwise evidence-backed items beyond the first
        # max_items non-evidence results would be silently dropped, and the query
        # could report "refused" even though evidence exists in the store.
        if request.require_evidence:
            all_matching = _get_store().query(
                kind=request.kind,
                aggregate_id=aggregate_uuid,
                event_time_lte=request.event_time_lte,
                require_evidence=False,
                max_items=None,
            )
            evidence_items = [s for s in all_matching if _has_evidence(s)]
            # Apply max_items AFTER the evidence filter
            stored_items = all_matching
            evidence_items = evidence_items[: request.max_items]
        else:
            stored_items = _get_store().query(
                kind=request.kind,
                aggregate_id=aggregate_uuid,
                event_time_lte=request.event_time_lte,
                require_evidence=False,
                max_items=request.max_items,
            )
            evidence_items = stored_items

    # Extract WorldContextItems from stored envelopes
    context_items: list[WorldContextItem] = []
    valid_envelopes: list[dict[str, Any]] = []
    for stored in evidence_items:
        for item in _extract_context_items(stored.envelope):
            context_items.append(item)
            valid_envelopes.append(stored.envelope)

    # Apply token budget — truncate items if over budget
    budgeted_items: list[WorldContextItem] = []
    budgeted_envelopes: list[dict[str, Any]] = []
    running_tokens = 0
    for item, env in zip(context_items, valid_envelopes):
        item_tokens = len(item.content) // 4
        if running_tokens + item_tokens > request.token_budget:
            break
        budgeted_items.append(item)
        budgeted_envelopes.append(env)
        running_tokens += item_tokens

    query_id = str(uuid4())

    # Determine status
    if not budgeted_items:
        if not stored_items:
            # No items match kind/aggregate filters at all
            status_str = "no_match"
            refusal_reason = "no_matching_items"
        elif request.require_evidence and not evidence_items:
            # Items exist but all were filtered by evidence requirement
            status_str = "refused"
            refusal_reason = "insufficient_evidence"
        else:
            status_str = "no_match"
            refusal_reason = "no_matching_items"
    else:
        status_str = "ok"
        refusal_reason = None

    return WorldMemoryQueryResponse(
        query_id=query_id,
        items=[
            WorldMemoryContextItemResponse(
                kind=item.kind.value,
                id=str(item.id),
                content=item.content,
                evidence_ids=[str(eid) for eid in item.evidence_ids],
                temporal=_extract_temporal_trace(envelope),
            )
            for item, envelope in zip(budgeted_items, budgeted_envelopes)
        ],
        envelopes=budgeted_envelopes,
        token_budget=request.token_budget,
        token_used=running_tokens,
        status=status_str,
        refusal_reason=refusal_reason,
        candidate_sources=candidate_traces,
        latency_ms=total_latency_ms,
    )


@router.post(
    "/state-at",
    response_model=WorldMemoryQueryResponse,
    summary="Resolve one aggregate WorldState at valid time",
)
async def world_memory_state_at(
    request: WorldMemoryStateAtRequest,
) -> WorldMemoryQueryResponse:
    try:
        aggregate_id = UUID(request.aggregate_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid aggregate_id UUID: {request.aggregate_id!r}",
        )

    started = perf_counter()
    try:
        if _is_pg_authority_configured():
            stored_items = await _pg_state_at(
                aggregate_id=aggregate_id,
                valid_at=request.valid_at,
            )
        else:
            stored_items = _get_store().state_at(
                aggregate_id=aggregate_id,
                valid_at=request.valid_at,
            )
    except (RuntimeError, OSError):
        refusal_reason = "state_at_authority_unavailable"
        return WorldMemoryQueryResponse(
            query_id=str(uuid4()),
            items=[],
            envelopes=[],
            token_budget=request.token_budget,
            token_used=0,
            status="refused",
            refusal_reason=refusal_reason,
            candidate_sources=[
                WorldMemorySourceTrace(
                    source="postgresql_valid_time_state",
                    status="unavailable",
                    error=refusal_reason,
                )
            ],
            latency_ms=max(0.0, (perf_counter() - started) * 1000.0),
        )

    if len(stored_items) > 1:
        refusal_reason = "ambiguous_valid_time_state"
        return WorldMemoryQueryResponse(
            query_id=str(uuid4()),
            items=[],
            envelopes=[],
            token_budget=request.token_budget,
            token_used=0,
            status="refused",
            refusal_reason=refusal_reason,
            candidate_sources=[
                WorldMemorySourceTrace(
                    source="postgresql_valid_time_state",
                    status="ambiguous",
                    candidate_count=len(stored_items),
                    error=refusal_reason,
                )
            ],
            latency_ms=max(0.0, (perf_counter() - started) * 1000.0),
        )

    if not stored_items:
        return WorldMemoryQueryResponse(
            query_id=str(uuid4()),
            items=[],
            envelopes=[],
            token_budget=request.token_budget,
            token_used=0,
            status="no_match",
            refusal_reason="no_matching_items",
            candidate_sources=[
                WorldMemorySourceTrace(
                    source="postgresql_valid_time_state",
                    status="no_match",
                )
            ],
            latency_ms=max(0.0, (perf_counter() - started) * 1000.0),
        )

    stored = stored_items[0]
    if request.require_evidence and not _has_evidence(stored):
        return WorldMemoryQueryResponse(
            query_id=str(uuid4()),
            items=[],
            envelopes=[],
            token_budget=request.token_budget,
            token_used=0,
            status="refused",
            refusal_reason="insufficient_evidence",
            candidate_sources=[
                WorldMemorySourceTrace(
                    source="postgresql_valid_time_state",
                    status="refused",
                    candidate_count=1,
                    error="insufficient_evidence",
                )
            ],
            latency_ms=max(0.0, (perf_counter() - started) * 1000.0),
        )

    items = _extract_context_items(stored.envelope)
    if not items:
        return WorldMemoryQueryResponse(
            query_id=str(uuid4()),
            items=[],
            envelopes=[],
            token_budget=request.token_budget,
            token_used=0,
            status="refused",
            refusal_reason="invalid_state_envelope",
            candidate_sources=[
                WorldMemorySourceTrace(
                    source="postgresql_valid_time_state",
                    status="refused",
                    candidate_count=1,
                    error="invalid_state_envelope",
                )
            ],
            latency_ms=max(0.0, (perf_counter() - started) * 1000.0),
        )

    item = items[0]
    item_tokens = len(item.content) // 4
    if item_tokens > request.token_budget:
        items = []
        envelopes: list[dict[str, Any]] = []
        token_used = 0
        status_str = "no_match"
        refusal_reason = "token_budget_exceeded"
    else:
        envelopes = [stored.envelope]
        token_used = item_tokens
        status_str = "ok"
        refusal_reason = None

    return WorldMemoryQueryResponse(
        query_id=str(uuid4()),
        items=[
            WorldMemoryContextItemResponse(
                kind=resolved.kind.value,
                id=str(resolved.id),
                content=resolved.content,
                evidence_ids=[str(eid) for eid in resolved.evidence_ids],
                temporal=_extract_temporal_trace(stored.envelope),
            )
            for resolved in items
        ],
        envelopes=envelopes,
        token_budget=request.token_budget,
        token_used=token_used,
        status=status_str,
        refusal_reason=refusal_reason,
        candidate_sources=[
            WorldMemorySourceTrace(
                source="postgresql_valid_time_state",
                status="ok",
                candidate_count=1,
                latency_ms=max(0.0, (perf_counter() - started) * 1000.0),
            )
        ],
        latency_ms=max(0.0, (perf_counter() - started) * 1000.0),
    )


@router.post(
    "/scenario-context",
    response_model=WorldMemoryScenarioContextResponse,
    summary="Create a read-only evidence-constrained scenario context",
    description="""
Return passive world-memory context for a named downstream scenario class.

This endpoint always reuses the existing world-memory query path with
`require_evidence=true`. It does not plan, predict, control, or issue actions.
The scenario label identifies a consumer class only; it does not select a
mission, platform, safety policy, or controller.
""",
)
async def world_memory_scenario_context(
    request: WorldMemoryScenarioContextRequest,
) -> WorldMemoryScenarioContextResponse:
    """Return an evidence-constrained, passive scenario context."""
    query_result = await world_memory_query(
        WorldMemoryQueryRequest(
            query=request.query,
            kind=None,
            aggregate_id=request.aggregate_id,
            require_evidence=True,
            max_items=request.max_items,
            token_budget=request.token_budget,
            event_time_lte=request.event_time_lte,
        )
    )
    return WorldMemoryScenarioContextResponse(
        scenario=request.scenario,
        passive=True,
        read_only=True,
        items=query_result.items,
        envelopes=query_result.envelopes,
        status=query_result.status,
        refusal_reason=query_result.refusal_reason,
        candidate_sources=query_result.candidate_sources,
        latency_ms=query_result.latency_ms,
    )
