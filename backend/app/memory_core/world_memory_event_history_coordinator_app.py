from __future__ import annotations

import asyncio  # noqa: ANYIO_OK
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from time import perf_counter
from typing import Protocol

from fastapi import FastAPI

from app.core.config import settings
from app.memory_core.memory_plane_session import (
    close_session_factory,
    init_session_factory,
)
from app.memory_core.world_memory_event_history_asgi_diagnostics import (
    ASGI_DIAGNOSTICS_PATH,
    ASGIDiagnosticApp,
    ASGIDiagnosticsSnapshot,
    build_asgi_diagnostic_app,
    get_asgi_diagnostic_snapshot,
    mark_handler_entry,
    mark_handler_exit,
)
from app.memory_core.world_memory_event_history_coordinator import (
    COORDINATOR_ENDPOINT_PATH,
    COORDINATOR_STATS_PATH,
    EventHistoryCoordinatorRequest,
    EventHistoryCoordinatorResponse,
    EventHistorySingleflightStats,
    EventHistoryTemporalTrace,
    PreparedEventHistoryEntry,
)
from app.memory_core.world_memory_event_history_diagnostics import (
    EventHistoryPreparedTiming,
    EventHistoryTimingCollector,
)
from app.memory_core.world_memory_event_history_runtime import (
    get_world_memory_event_history_client,
)


class _SourceTrace(Protocol):
    source: str
    candidate_count: int
    latency_ms: float


# Exact-request registry for the service-wide P10-R prepared-response flight.
# A task result is the fully prepared EventHistoryCoordinatorResponse, never a
# bare authority list. Entries are removed on terminal completion so a
# completed result is never reused by a later request.
_prepared_response_tasks: dict[
    EventHistoryCoordinatorRequest,
    asyncio.Task[EventHistoryCoordinatorResponse],
] = {}
# Lifetime read-only counter of prepared-response leaders ever created by
# world_memory_event_history_query. Never reset by shutdown.
_prepared_response_leader_count: int = 0
_prepared_response_timing: dict[EventHistoryCoordinatorRequest, EventHistoryPreparedTiming] = {}
_timing_collector = EventHistoryTimingCollector()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await init_session_factory()
    await get_world_memory_event_history_client()
    try:
        yield
    finally:
        prepared_tasks = tuple(_prepared_response_tasks.values())
        for prepared_task in prepared_tasks:
            if not prepared_task.done():
                _cancel_result = prepared_task.cancel()
        if prepared_tasks:
            _gather_results = await asyncio.gather(*prepared_tasks, return_exceptions=True)
        _prepared_response_tasks.clear()
        _prepared_response_timing.clear()

        from app.memory_core.world_memory_event_history_runtime import (
            close_world_memory_event_history_client,
        )

        await close_world_memory_event_history_client()
        await close_session_factory()


def _history_trace(
    traces: Sequence[_SourceTrace],
) -> tuple[int, float]:
    for trace in traces:
        if trace.source == "elasticsearch_event_history":
            candidate_count = trace.candidate_count
            candidate_latency_ms = trace.latency_ms
            return max(0, int(candidate_count)), max(0.0, float(candidate_latency_ms))
    return 0, 0.0


def _clear_prepared_response_task(
    key: EventHistoryCoordinatorRequest,
    task: asyncio.Future[EventHistoryCoordinatorResponse],
    timing: EventHistoryPreparedTiming,
) -> None:
    timing.mark_complete()
    if _prepared_response_tasks.get(key) is task:
        del _prepared_response_tasks[key]
        if _prepared_response_timing.get(key) is timing:
            _prepared_response_timing.pop(key, None)
    if not task.cancelled():
        _ = task.exception()


async def _prepare_event_history_response(
    request: EventHistoryCoordinatorRequest,
) -> EventHistoryCoordinatorResponse:
    from app.api.world_memory_plane import (
        _extract_context_items,
        _extract_temporal_trace,
        _pg_event_history_query,
    )

    timing = _prepared_response_timing.get(request)
    authorities, traces, _, refusal_reason = await _pg_event_history_query(
        query=request.query,
        event_time_lte=request.event_time_lte,
        kind=request.kind,
        aggregate_id=request.aggregate_id,
        require_evidence=request.require_evidence,
        max_items=request.max_items,
        diagnostic_sink=(None if timing is None else timing.recorder),
    )
    candidate_count, candidate_latency_ms = _history_trace(traces)
    if refusal_reason is not None:
        if timing is not None:
            retrieval_ms = (
                0.0
                if timing.recorder.retrieval_timing is None
                else timing.recorder.retrieval_timing.retrieval_ms
            )
            timing.preparation_ms = max(
                0.0,
                (perf_counter() - timing.started_at) * 1000.0 - retrieval_ms,
            )
        return EventHistoryCoordinatorResponse(
            candidate_count=candidate_count,
            candidate_latency_ms=candidate_latency_ms,
            refusal_reason=refusal_reason,
        )

    entries: list[PreparedEventHistoryEntry] = []
    for authority in authorities:
        # Temporal/context extraction are read-only over the authority
        # envelope. Each prepared entry still stores its own deep copy so
        # entries never share mutable state with the authority row or with
        # one another.
        envelope = authority.envelope
        trace = _extract_temporal_trace(envelope)
        temporal = EventHistoryTemporalTrace(
            event_occurred_at=trace.event_occurred_at,
            observed_at=trace.observed_at,
            ingested_at=trace.ingested_at,
            validity_start=trace.validity_start,
            validity_end=trace.validity_end,
            created_at=trace.created_at,
            verified_at=trace.verified_at,
        )
        for item in _extract_context_items(envelope):
            entries.append(
                PreparedEventHistoryEntry(
                    envelope=deepcopy(envelope),
                    kind=item.kind.value,
                    id=item.id,
                    content=item.content,
                    evidence_ids=tuple(item.evidence_ids),
                    temporal=temporal,
                )
            )
    if timing is not None:
        retrieval_ms = (
            0.0
            if timing.recorder.retrieval_timing is None
            else timing.recorder.retrieval_timing.retrieval_ms
        )
        timing.preparation_ms = max(
            0.0,
            (perf_counter() - timing.started_at) * 1000.0 - retrieval_ms,
        )
    return EventHistoryCoordinatorResponse(
        entries=tuple(entries),
        candidate_count=candidate_count,
        candidate_latency_ms=candidate_latency_ms,
    )


async def _world_memory_event_history_query(
    request: EventHistoryCoordinatorRequest,
) -> EventHistoryCoordinatorResponse:
    """Create or join one cancellation-shielded prepared-response task.

    Identical normalized requests share a single leader task whose result is
    the fully prepared ``EventHistoryCoordinatorResponse``. A completed result
    is removed from the registry and is never reused.
    """
    global _prepared_response_leader_count
    caller_started = perf_counter()
    task = _prepared_response_tasks.get(request)
    if task is None or task.done():
        role = "leader"
        timing = EventHistoryPreparedTiming()
        _prepared_response_timing[request] = timing
        task = asyncio.create_task(_prepare_event_history_response(request))
        _prepared_response_tasks[request] = task
        _prepared_response_leader_count += 1
        task.add_done_callback(
            lambda completed: _clear_prepared_response_task(request, completed, timing)
        )
    else:
        role = "joiner"
        timing = _prepared_response_timing[request]
    response = await asyncio.shield(task)
    timing.record_completed(role, caller_started, _timing_collector)
    return response


async def world_memory_event_history_query(
    request: EventHistoryCoordinatorRequest,
) -> EventHistoryCoordinatorResponse:
    mark_handler_entry()
    try:
        return await _world_memory_event_history_query(request)
    finally:
        mark_handler_exit()


async def world_memory_event_history_stats() -> EventHistorySingleflightStats:
    """Live snapshot of the coordinator prepared-response singleflight."""
    in_flight = sum(1 for task in _prepared_response_tasks.values() if not task.done())
    snapshot = _timing_collector.snapshot()
    return EventHistorySingleflightStats(
        leader_count=_prepared_response_leader_count,
        in_flight=in_flight,
        join_count=snapshot.join_count,
        last_sequence=snapshot.last_sequence,
        timing_samples=snapshot.timing_samples,
    )


def create_world_memory_event_history_coordinator_app() -> FastAPI | ASGIDiagnosticApp:
    coordinator_app = FastAPI(
        title=f"{settings.APP_NAME} historical event coordinator",
        version=settings.APP_VERSION,
        lifespan=lifespan,
    )
    coordinator_app.add_api_route(
        COORDINATOR_ENDPOINT_PATH,
        world_memory_event_history_query,
        methods=["POST"],
        response_model=EventHistoryCoordinatorResponse,
    )
    coordinator_app.add_api_route(
        COORDINATOR_STATS_PATH,
        world_memory_event_history_stats,
        methods=["GET"],
        response_model=EventHistorySingleflightStats,
    )
    if settings.WORLD_MEMORY_EVENT_HISTORY_ASGI_DIAGNOSTICS_ENABLED:
        coordinator_app.add_api_route(
            ASGI_DIAGNOSTICS_PATH,
            get_asgi_diagnostic_snapshot,
            methods=["GET"],
            response_model=ASGIDiagnosticsSnapshot,
            include_in_schema=False,
        )
        return build_asgi_diagnostic_app(coordinator_app)
    return coordinator_app


app = create_world_memory_event_history_coordinator_app()


__all__ = [
    "app",
    "create_world_memory_event_history_coordinator_app",
    "lifespan",
    "world_memory_event_history_query",
    "world_memory_event_history_stats",
]
