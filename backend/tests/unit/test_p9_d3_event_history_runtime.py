from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import cast
from uuid import UUID

import anyio
import pytest
from elastic_transport import HttpxAsyncHttpNode
from elasticsearch import AsyncElasticsearch
from neo4j import AsyncDriver
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core import elasticsearch as core_elasticsearch
from app.core.config import Settings
from app.memory_core import world_memory_event_history_runtime as runtime
from app.memory_core.world_memory_event_history_readiness import (
    EventHistoryReadiness,
    EventHistoryReadinessStatus,
)
from app.memory_core.world_memory_projection import _runtime as projection_runtime


class _FakeElasticsearch(AsyncElasticsearch):
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _TrackedAssessmentSession:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def _event_history_query_key(query: str = "same-query") -> runtime.EventHistoryQueryKey:
    return runtime.EventHistoryQueryKey(
        query=query,
        event_time_lte=datetime(2026, 8, 6, 12, tzinfo=timezone.utc),
        kind="fact",
        aggregate_id=UUID("00000000-0000-0000-0000-000000000001"),
        require_evidence=True,
        max_items=10,
    )


def _event_history_query_result() -> runtime.EventHistoryQueryExecution:
    return runtime.EventHistoryQueryExecution(
        authorities=(),
        candidate_count=0,
        candidate_latency_ms=0.0,
        refusal_reason=None,
    )


@pytest.mark.asyncio
async def test_event_history_query_singleflight_joins_identical_in_flight_supplier() -> None:
    await runtime.close_world_memory_event_history_client()
    supplier_started = anyio.Event()
    second_waiter_started = anyio.Event()
    release_supplier = anyio.Event()
    supplier_calls = 0
    results: list[runtime.EventHistoryQueryExecution] = []

    async def supply() -> runtime.EventHistoryQueryExecution:
        nonlocal supplier_calls
        supplier_calls += 1
        supplier_started.set()
        await release_supplier.wait()
        return _event_history_query_result()

    async def wait_for_query(second: bool = False) -> None:
        if second:
            second_waiter_started.set()
        results.append(
            await runtime.coalesce_world_memory_event_history_query(
                _event_history_query_key(),
                supply,
            )
        )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(wait_for_query)
        await supplier_started.wait()
        task_group.start_soon(wait_for_query, True)
        await second_waiter_started.wait()
        assert supplier_calls == 1
        release_supplier.set()

    assert supplier_calls == 1
    assert len(results) == 2


@pytest.mark.asyncio
async def test_event_history_query_singleflight_runs_distinct_keys_independently() -> None:
    await runtime.close_world_memory_event_history_client()
    release_suppliers = anyio.Event()
    both_suppliers_started = anyio.Event()
    supplier_calls: dict[str, int] = {}

    async def supply(query: str) -> runtime.EventHistoryQueryExecution:
        supplier_calls[query] = supplier_calls.get(query, 0) + 1
        if len(supplier_calls) == 2:
            both_suppliers_started.set()
        await release_suppliers.wait()
        return _event_history_query_result()

    async def wait_for_query(query: str) -> None:
        await runtime.coalesce_world_memory_event_history_query(
            _event_history_query_key(query),
            lambda: supply(query),
        )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(wait_for_query, "first-query")
        task_group.start_soon(wait_for_query, "second-query")
        await both_suppliers_started.wait()
        assert supplier_calls == {"first-query": 1, "second-query": 1}
        release_suppliers.set()


@pytest.mark.asyncio
async def test_event_history_query_singleflight_waiter_cancellation_does_not_cancel_leader() -> (
    None
):
    await runtime.close_world_memory_event_history_client()
    supplier_started = anyio.Event()
    release_supplier = anyio.Event()
    cancelled_waiter_started = anyio.Event()
    cancelled_waiter_done = anyio.Event()
    second_waiter_done = anyio.Event()
    leader_done = anyio.Event()
    cancelled_waiter_scope: anyio.CancelScope | None = None
    cancelled_waiter = False
    supplier_calls = 0

    async def supply() -> runtime.EventHistoryQueryExecution:
        nonlocal supplier_calls
        supplier_calls += 1
        supplier_started.set()
        await release_supplier.wait()
        return _event_history_query_result()

    async def wait_for_query(name: str) -> None:
        nonlocal cancelled_waiter_scope, cancelled_waiter
        with anyio.CancelScope() as cancel_scope:
            if name == "cancelled":
                cancelled_waiter_scope = cancel_scope
                cancelled_waiter_started.set()
            try:
                await runtime.coalesce_world_memory_event_history_query(
                    _event_history_query_key(),
                    supply,
                )
            except anyio.get_cancelled_exc_class():
                cancelled_waiter = True
            finally:
                if name == "cancelled":
                    cancelled_waiter_done.set()
                elif name == "second":
                    second_waiter_done.set()
                else:
                    leader_done.set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(wait_for_query, "leader")
        await supplier_started.wait()
        task_group.start_soon(wait_for_query, "second")
        task_group.start_soon(wait_for_query, "cancelled")
        await cancelled_waiter_started.wait()
        assert cancelled_waiter_scope is not None
        cancelled_waiter_scope.cancel()
        await cancelled_waiter_done.wait()
        assert supplier_calls == 1
        release_supplier.set()
        await leader_done.wait()
        await second_waiter_done.wait()

    assert cancelled_waiter
    assert supplier_calls == 1


@pytest.mark.asyncio
async def test_event_history_query_singleflight_starts_fresh_supplier_after_completion() -> None:
    await runtime.close_world_memory_event_history_client()
    supplier_calls = 0

    async def supply() -> runtime.EventHistoryQueryExecution:
        nonlocal supplier_calls
        supplier_calls += 1
        return _event_history_query_result()

    key = _event_history_query_key()
    await runtime.coalesce_world_memory_event_history_query(key, supply)
    await runtime.coalesce_world_memory_event_history_query(key, supply)

    assert supplier_calls == 2


@pytest.mark.asyncio
async def test_event_history_runtime_close_cancels_active_query_tasks() -> None:
    await runtime.close_world_memory_event_history_client()
    supplier_started = anyio.Event()
    supplier_cancelled = anyio.Event()
    waiter_cancelled = False

    async def supply() -> runtime.EventHistoryQueryExecution:
        supplier_started.set()
        try:
            await anyio.sleep_forever()
        finally:
            supplier_cancelled.set()
        return _event_history_query_result()

    async def wait_for_query() -> None:
        nonlocal waiter_cancelled
        try:
            await runtime.coalesce_world_memory_event_history_query(
                _event_history_query_key(),
                supply,
            )
        except asyncio.CancelledError:
            waiter_cancelled = True

    waiter_task = asyncio.create_task(wait_for_query())
    await supplier_started.wait()
    await runtime.close_world_memory_event_history_client()
    await waiter_task

    assert supplier_cancelled.is_set()
    assert waiter_cancelled
    assert runtime._event_history_query_tasks == {}


@pytest.mark.asyncio
async def test_event_history_readiness_singleflight_is_in_flight_only_and_shutdown_safe() -> None:
    await runtime.close_world_memory_event_history_client()
    assessment_started = anyio.Event()
    release_assessment = anyio.Event()
    sessions: list[_TrackedAssessmentSession] = []
    waiter_started = {name: anyio.Event() for name in ("first", "second", "cancelled")}
    waiter_done = {name: anyio.Event() for name in ("first", "second", "cancelled")}
    waiter_scopes: dict[str, anyio.CancelScope] = {}
    cancelled_waiters: set[str] = set()
    results: dict[str, EventHistoryReadiness] = {}
    assessment_calls = 0
    block_next_assessment = False

    async def assess() -> EventHistoryReadiness:
        nonlocal assessment_calls, assessment_started, release_assessment, block_next_assessment
        assessment_calls += 1
        session = _TrackedAssessmentSession()
        sessions.append(session)
        try:
            if assessment_calls == 1 or block_next_assessment:
                block_next_assessment = False
                assessment_started.set()
                await release_assessment.wait()
            return EventHistoryReadiness(EventHistoryReadinessStatus.READY, 3, 3)
        finally:
            await session.close()

    async def wait_for_assessment(name: str) -> None:
        with anyio.CancelScope() as cancel_scope:
            waiter_scopes[name] = cancel_scope
            waiter_started[name].set()
            try:
                results[name] = await runtime.assess_world_memory_event_history_readiness(assess)
            except anyio.get_cancelled_exc_class():
                cancelled_waiters.add(name)
            finally:
                waiter_done[name].set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(wait_for_assessment, "first")
        await assessment_started.wait()
        task_group.start_soon(wait_for_assessment, "second")
        await waiter_started["second"].wait()
        task_group.start_soon(wait_for_assessment, "cancelled")
        await waiter_started["cancelled"].wait()
        waiter_scopes["cancelled"].cancel()
        await waiter_done["cancelled"].wait()
        release_assessment.set()

    assert assessment_calls == 1
    assert cancelled_waiters == {"cancelled"}
    assert results["first"].status is EventHistoryReadinessStatus.READY
    assert results["second"].status is EventHistoryReadinessStatus.READY
    assert sessions[0].close_calls == 1

    later = await runtime.assess_world_memory_event_history_readiness(assess)

    assert assessment_calls == 2
    assert later.status is EventHistoryReadinessStatus.READY
    assert sessions[1].close_calls == 1

    block_next_assessment = True
    assessment_started = anyio.Event()
    release_assessment = anyio.Event()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(runtime.assess_world_memory_event_history_readiness, assess)
        await assessment_started.wait()
        await runtime.close_world_memory_event_history_client()

    assert sessions[2].close_calls == 1


@pytest.mark.asyncio
async def test_event_history_runtime_is_opt_in_caches_its_client_and_closes_once() -> None:
    await runtime.close_world_memory_event_history_client()
    original = runtime.settings
    runtime.settings = Settings(
        WORLD_MEMORY_EVENT_HISTORY_ENABLED=True,
        WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME="world-memory-events-v2",
    )
    created: list[_FakeElasticsearch] = []

    def make_client(url: str) -> _FakeElasticsearch:
        assert url == "http://localhost:9200"
        client = _FakeElasticsearch()
        created.append(client)
        return client

    try:
        first = await runtime.get_world_memory_event_history_client(
            elasticsearch_factory=make_client,
        )
        second = await runtime.get_world_memory_event_history_client(
            elasticsearch_factory=make_client,
        )
        await runtime.close_world_memory_event_history_client()
        await runtime.close_world_memory_event_history_client()
    finally:
        runtime.settings = original

    assert first is second
    assert len(created) == 1
    assert created[0].close_calls == 1


@pytest.mark.asyncio
async def test_projection_worker_threads_the_configured_history_index_to_its_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_indices: list[str | None] = []

    class _FakeElasticsearchWriter:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def ensure_index(self) -> None:
            return None

        async def __call__(self, command: object) -> None:
            del command

    class _FakeEventHistoryWriter(_FakeElasticsearchWriter):
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args
            raw_index = kwargs.get("index_name")
            history_indices.append(raw_index if isinstance(raw_index, str) else None)

    class _FakeNeo4jWriter:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def ensure_schema(self) -> None:
            return None

        async def __call__(self, command: object) -> None:
            del command

    monkeypatch.setattr(
        projection_runtime,
        "WorldMemoryElasticsearchWriter",
        _FakeElasticsearchWriter,
    )
    monkeypatch.setattr(
        projection_runtime,
        "WorldMemoryEventHistoryElasticsearchWriter",
        _FakeEventHistoryWriter,
    )
    monkeypatch.setattr(projection_runtime, "WorldMemoryNeo4jWriter", _FakeNeo4jWriter)

    _ = await projection_runtime.create_world_memory_projection_worker(
        session_factory=async_sessionmaker(),
        elasticsearch_client=cast(AsyncElasticsearch, object()),
        neo4j_driver=cast(AsyncDriver, object()),
        worker_identity="p9-d3-runtime-test",
        event_history_index_name="custom-event-history-index",
    )

    assert history_indices == ["custom-event-history-index"]


@pytest.mark.asyncio
async def test_application_es_client_uses_the_available_httpx_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[dict[str, object]] = []

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args
            constructed.append(kwargs)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(core_elasticsearch, "AsyncElasticsearch", _FakeClient)
    client = core_elasticsearch.ESClient()

    _ = await client.connect()

    assert constructed[0]["node_class"] is core_elasticsearch.HttpxAsyncHttpNode


@pytest.mark.asyncio
async def test_history_client_uses_configured_transport_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await runtime.close_world_memory_event_history_client()
    original = runtime.settings
    constructed: list[dict[str, object]] = []

    class _ConfiguredFakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args
            constructed.append(kwargs)

        async def close(self) -> None:
            return None

    runtime.settings = Settings(
        WORLD_MEMORY_EVENT_HISTORY_ENABLED=True,
        ES_CONNECTIONS_PER_NODE=37,
    )
    monkeypatch.setattr(runtime, "AsyncElasticsearch", _ConfiguredFakeClient)

    try:
        client = await runtime.get_world_memory_event_history_client()
    finally:
        await runtime.close_world_memory_event_history_client()
        runtime.settings = original

    assert client is not None
    assert constructed == [
        {
            "node_class": HttpxAsyncHttpNode,
            "connections_per_node": 37,
        }
    ]
