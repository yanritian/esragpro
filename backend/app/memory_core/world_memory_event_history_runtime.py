from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from elastic_transport import HttpxAsyncHttpNode
from elasticsearch import AsyncElasticsearch

from app.core.config import settings
from app.memory_core.repositories.world_memory_authority import StoredEnvelope
from app.memory_core.world_memory_event_history_readiness import EventHistoryReadiness


class _ElasticsearchFactory(Protocol):
    def __call__(self, url: str) -> AsyncElasticsearch: ...


@dataclass(frozen=True, slots=True)
class EventHistoryQueryKey:
    """Exact retrieval inputs used to identify one in-flight event-history read."""

    query: str
    event_time_lte: datetime
    kind: str | None
    aggregate_id: UUID | None
    require_evidence: bool
    max_items: int


@dataclass(frozen=True, slots=True)
class EventHistoryQueryExecution:
    """Temporary core result shared only while an event-history read is active."""

    authorities: tuple[StoredEnvelope, ...]
    candidate_count: int = 0
    candidate_latency_ms: float = 0.0
    retrieval_latency_ms: float = 0.0
    readiness_latency_ms: float = 0.0
    authority_latency_ms: float = 0.0
    query_unattributed_ms: float = 0.0
    refusal_reason: str | None = None


_event_history_client: AsyncElasticsearch | None = None
_event_history_readiness_task: asyncio.Task[EventHistoryReadiness] | None = None
_event_history_query_tasks: dict[
    EventHistoryQueryKey, asyncio.Task[EventHistoryQueryExecution]
] = {}


def _clear_event_history_readiness_task(
    task: asyncio.Task[EventHistoryReadiness],
) -> None:
    global _event_history_readiness_task
    if _event_history_readiness_task is task:
        _event_history_readiness_task = None
    if not task.cancelled():
        _ = task.exception()


async def assess_world_memory_event_history_readiness(
    assessment: Callable[[], Coroutine[object, object, EventHistoryReadiness]],
) -> EventHistoryReadiness:
    """Share one live readiness assessment without caching its result."""
    global _event_history_readiness_task
    task = _event_history_readiness_task
    if task is None or task.done():
        task = asyncio.create_task(assessment())
        _event_history_readiness_task = task
        task.add_done_callback(_clear_event_history_readiness_task)
    return await asyncio.shield(task)


def _clear_event_history_query_task(
    key: EventHistoryQueryKey,
    task: asyncio.Future[EventHistoryQueryExecution],
) -> None:
    if _event_history_query_tasks.get(key) is task:
        del _event_history_query_tasks[key]
    if not task.cancelled():
        _ = task.exception()


async def coalesce_world_memory_event_history_query(
    key: EventHistoryQueryKey,
    query: Callable[[], Coroutine[object, object, EventHistoryQueryExecution]],
) -> EventHistoryQueryExecution:
    """Share one live exact event-history retrieval without retaining its result."""
    task = _event_history_query_tasks.get(key)
    if task is None or task.done():
        task = asyncio.create_task(query())
        _event_history_query_tasks[key] = task
        task.add_done_callback(lambda completed: _clear_event_history_query_task(key, completed))
    return await asyncio.shield(task)


def _default_elasticsearch_factory(url: str) -> AsyncElasticsearch:
    return AsyncElasticsearch(
        url,
        node_class=HttpxAsyncHttpNode,
        connections_per_node=settings.ES_CONNECTIONS_PER_NODE,
    )


async def get_world_memory_event_history_client(
    *,
    elasticsearch_factory: _ElasticsearchFactory | None = None,
) -> AsyncElasticsearch | None:
    global _event_history_client
    if not settings.WORLD_MEMORY_EVENT_HISTORY_ENABLED:
        return None
    if _event_history_client is not None:
        return _event_history_client
    factory = elasticsearch_factory or _default_elasticsearch_factory
    _event_history_client = factory(settings.ES_HOST)
    return _event_history_client


async def close_world_memory_event_history_client() -> None:
    global _event_history_client, _event_history_readiness_task
    query_tasks = tuple(_event_history_query_tasks.values())
    for query_task in query_tasks:
        if not query_task.done():
            _ = query_task.cancel()
    if query_tasks:
        _ = await asyncio.gather(*query_tasks, return_exceptions=True)
    _event_history_query_tasks.clear()

    readiness_task = _event_history_readiness_task
    try:
        if readiness_task is not None and not readiness_task.done():
            _ = readiness_task.cancel()
            try:
                await readiness_task
            except asyncio.CancelledError:
                pass
    finally:
        if _event_history_readiness_task is readiness_task:
            _event_history_readiness_task = None
        client = _event_history_client
        _event_history_client = None
        if client is not None:
            await client.close()


__all__ = [
    "EventHistoryQueryExecution",
    "EventHistoryQueryKey",
    "assess_world_memory_event_history_readiness",
    "close_world_memory_event_history_client",
    "coalesce_world_memory_event_history_query",
    "get_world_memory_event_history_client",
]
