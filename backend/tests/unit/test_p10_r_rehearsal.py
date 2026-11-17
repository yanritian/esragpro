"""Focused rehearsal coverage for the P10-R isolated slice.

Covers the coordinator-app prepared-response singleflight and the isolated
two-service compose artifact without starting any infrastructure:

- the coordinator ``leader_count`` increases exactly once for concurrent
  identical prepared-response requests, ``in_flight`` is nonzero while the
  leader is blocked, and returns to zero after completion;
- distinct exact requests create distinct prepared-response leaders;
- authority/context/temporal preparation runs once per returned
  authority/item, never once per waiter;
- the coordinator app exposes a typed internal GET stats endpoint returning
  the live prepared-response snapshot;
- the isolated ``docker-compose.p10r.yml`` declares exactly the approved
  two-service topology with no volumes and no production compose mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
import pytest
import yaml
from fastapi.routing import APIRoute

from app.api import world_memory_plane as wmp
from app.memory_core.repositories.world_memory_authority import StoredEnvelope
from app.memory_core.world_memory import WorldObservation
from app.memory_core.world_memory_event_history_coordinator import (
    COORDINATOR_STATS_PATH,
    EventHistoryCoordinatorRequest,
    EventHistoryCoordinatorResponse,
    EventHistorySingleflightStats,
)
from app.memory_core.world_memory_event_history_coordinator_app import (
    create_world_memory_event_history_coordinator_app,
    world_memory_event_history_query,
    world_memory_event_history_stats,
)
from app.memory_core.world_memory_serializer import (
    EVENT_OBSERVATION_INGESTED,
    build_world_memory_envelope,
)


def _make_stored_envelope(
    *,
    content: str,
    occurred_at: datetime,
) -> StoredEnvelope:
    envelope = build_world_memory_envelope(
        WorldObservation(
            id=UUID("00000000-0000-0000-0000-000000000101"),
            content=content,
            observed_at=occurred_at,
            ingested_at=occurred_at,
            source="rescue-sensor",
            raw_evidence_ref="sensor://road-closure/1000",
        ),
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="elasticsearch",
    )
    return StoredEnvelope(
        id=UUID("00000000-0000-0000-0000-000000000201"),
        event_type=envelope["event_type"],
        schema_version=envelope["schema_version"],
        aggregate_id=UUID("00000000-0000-0000-0000-000000000301"),
        projection_target=envelope["projection_target"],
        idempotency_key=envelope["idempotency_key"],
        occurred_at=occurred_at,
        kind="observation",
        envelope=envelope,
        created_at=occurred_at,
    )


@pytest.mark.asyncio
async def test_identical_coordinator_requests_prepare_authority_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent identical coordinator calls share one prepared response.

    The leader prepares the canonical typed response exactly once; every
    waiter joins the same in-flight prepared-response task and receives the
    same typed response. Authority/context/temporal preparation runs once per
    returned authority/item, never once per waiter.
    """
    occurred_at = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    stored1 = _make_stored_envelope(content="Historic road closure", occurred_at=occurred_at)
    stored2 = _make_stored_envelope(content="Bridge inspection", occurred_at=occurred_at)

    initial_stats = await world_memory_event_history_stats()
    initial_leader_count = initial_stats.leader_count
    initial_in_flight = initial_stats.in_flight
    assert initial_in_flight == 0

    pg_calls = 0
    temporal_calls = 0
    context_calls = 0
    started_count = 0
    waiters_started = anyio.Event()
    release_supplier = anyio.Event()
    results: list[EventHistoryCoordinatorResponse] = []

    async def fake_pg_event_history_query(**kwargs: Any) -> Any:
        nonlocal pg_calls
        pg_calls += 1
        await release_supplier.wait()
        return (
            [stored1, stored2],
            [
                wmp.WorldMemorySourceTrace(
                    source="elasticsearch_event_history",
                    status="ok",
                    candidate_count=2,
                    latency_ms=7.0,
                )
            ],
            8.0,
            None,
        )

    original_extract_temporal_trace = wmp._extract_temporal_trace
    original_extract_context_items = wmp._extract_context_items

    def fake_extract_temporal_trace(
        envelope: dict[str, Any],
    ) -> wmp.WorldMemoryTemporalTrace:
        nonlocal temporal_calls
        temporal_calls += 1
        return original_extract_temporal_trace(envelope)

    def fake_extract_context_items(
        envelope: dict[str, Any],
    ) -> list[wmp.WorldContextItem]:
        nonlocal context_calls
        context_calls += 1
        return original_extract_context_items(envelope)

    monkeypatch.setattr(wmp, "_pg_event_history_query", fake_pg_event_history_query)
    monkeypatch.setattr(wmp, "_extract_temporal_trace", fake_extract_temporal_trace)
    monkeypatch.setattr(wmp, "_extract_context_items", fake_extract_context_items)

    request = EventHistoryCoordinatorRequest(
        query="  road closure  ",
        event_time_lte=occurred_at,
        kind="observation",
        aggregate_id=stored1.aggregate_id,
        require_evidence=True,
        max_items=25,
    )

    async def wait_for_query() -> None:
        nonlocal started_count
        started_count += 1
        if started_count >= 8:
            waiters_started.set()
        results.append(await world_memory_event_history_query(request))

    async with anyio.create_task_group() as task_group:
        for _ in range(8):
            task_group.start_soon(wait_for_query)
        await waiters_started.wait()
        # Yield once so the final waiter can reach the shared in-flight task
        # before we observe the blocked leader and then release it.
        await anyio.lowlevel.checkpoint()

        blocked_stats = await world_memory_event_history_stats()
        assert blocked_stats.leader_count == initial_leader_count + 1
        assert blocked_stats.in_flight == 1

        release_supplier.set()

    assert len(results) == 8
    first = results[0]
    assert isinstance(first, EventHistoryCoordinatorResponse)
    assert first.refusal_reason is None
    assert first.candidate_count == 2
    assert len(first.entries) == 2
    for result in results[1:]:
        assert isinstance(result, EventHistoryCoordinatorResponse)
        assert result == first

    assert pg_calls == 1
    assert temporal_calls == 2
    assert context_calls == 2

    final_stats = await world_memory_event_history_stats()
    assert final_stats.leader_count == initial_leader_count + 1
    assert final_stats.in_flight == 0


@pytest.mark.asyncio
async def test_singleflight_stats_lifetime_in_flight_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_stats = await world_memory_event_history_stats()
    initial_leader_count = initial_stats.leader_count
    assert initial_stats.in_flight == 0

    supplier_started = anyio.Event()
    second_waiter_started = anyio.Event()
    release_supplier = anyio.Event()
    supplier_calls = 0
    results: list[EventHistoryCoordinatorResponse] = []

    async def fake_pg_event_history_query(**kwargs: Any) -> Any:
        nonlocal supplier_calls
        supplier_calls += 1
        supplier_started.set()
        await release_supplier.wait()
        return [], [], 0.0, None

    monkeypatch.setattr(wmp, "_pg_event_history_query", fake_pg_event_history_query)

    request = EventHistoryCoordinatorRequest(
        query="road closure",
        event_time_lte=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
        max_items=50,
    )

    async def wait_for_query(second: bool = False) -> None:
        if second:
            second_waiter_started.set()
        results.append(await world_memory_event_history_query(request))

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(wait_for_query)
        await supplier_started.wait()
        blocked_stats = await world_memory_event_history_stats()
        assert blocked_stats.leader_count == initial_leader_count + 1
        assert blocked_stats.in_flight == 1

        task_group.start_soon(wait_for_query, True)
        await second_waiter_started.wait()
        blocked_stats = await world_memory_event_history_stats()
        assert blocked_stats.leader_count == initial_leader_count + 1
        assert blocked_stats.in_flight == 1
        release_supplier.set()

    assert supplier_calls == 1
    assert len(results) == 2
    final_stats = await world_memory_event_history_stats()
    assert final_stats.leader_count == initial_leader_count + 1
    assert final_stats.in_flight == 0


@pytest.mark.asyncio
async def test_singleflight_stats_distinct_keys_create_distinct_leaders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_stats = await world_memory_event_history_stats()
    initial_leader_count = initial_stats.leader_count
    release_suppliers = anyio.Event()
    both_suppliers_started = anyio.Event()
    supplier_calls: dict[str, int] = {}

    async def fake_pg_event_history_query(**kwargs: Any) -> Any:
        query = str(kwargs["query"])
        supplier_calls[query] = supplier_calls.get(query, 0) + 1
        if len(supplier_calls) == 2:
            both_suppliers_started.set()
        await release_suppliers.wait()
        return [], [], 0.0, None

    monkeypatch.setattr(wmp, "_pg_event_history_query", fake_pg_event_history_query)

    async def wait_for_query(query: str) -> None:
        await world_memory_event_history_query(
            EventHistoryCoordinatorRequest(
                query=query,
                event_time_lte=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
                max_items=50,
            )
        )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(wait_for_query, "first-query")
        task_group.start_soon(wait_for_query, "second-query")
        await both_suppliers_started.wait()
        blocked_stats = await world_memory_event_history_stats()
        assert blocked_stats.leader_count == initial_leader_count + 2
        assert blocked_stats.in_flight == 2
        release_suppliers.set()

    assert supplier_calls == {"first-query": 1, "second-query": 1}
    final_stats = await world_memory_event_history_stats()
    assert final_stats.in_flight == 0


@pytest.mark.asyncio
async def test_coordinator_stats_endpoint_returns_typed_snapshot() -> None:
    snapshot = await world_memory_event_history_stats()
    assert isinstance(snapshot, EventHistorySingleflightStats)
    assert snapshot.leader_count >= 0
    assert snapshot.in_flight == 0


def test_coordinator_app_registers_internal_stats_get_route() -> None:
    coordinator_app = create_world_memory_event_history_coordinator_app()
    stats_routes = [
        route
        for route in coordinator_app.routes
        if isinstance(route, APIRoute) and route.path == COORDINATOR_STATS_PATH
    ]
    assert len(stats_routes) == 1
    methods = stats_routes[0].methods
    assert methods is not None
    assert "GET" in methods
    assert COORDINATOR_STATS_PATH.startswith("/internal/")


def _command_workers(command: object) -> int:
    assert isinstance(command, list)
    assert "--workers" in command
    raw = command[command.index("--workers") + 1]
    assert isinstance(raw, str)
    if raw.startswith("${") and ":-" in raw and raw.endswith("}"):
        # Compose env-substitution with a default: resolve to the default so
        # the parameterized coordinator worker count still asserts as 1.
        return int(raw[raw.index(":-") + 2 : -1])
    return int(raw)


def _p10r_compose() -> dict[str, Any]:
    compose_path = Path(__file__).parents[2] / "docker-compose.p10r.yml"
    payload = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("compose payload must be a mapping")
    return payload


def test_p10r_compose_declares_exact_isolated_topology() -> None:
    payload = _p10r_compose()
    services = payload["services"]
    assert isinstance(services, dict)
    assert set(services) == {"coordinator", "api"}
    # No named volumes or bind mounts anywhere: the isolated rehearsal must not
    # mutate host paths or the production compose topology.
    assert "volumes" not in payload

    coordinator = services["coordinator"]
    assert coordinator["image"] == "esragpro-p10r-backend:probe"
    assert coordinator["build"] == {"context": ".", "dockerfile": "Dockerfile"}
    assert "ports" not in coordinator
    assert "volumes" not in coordinator
    assert _command_workers(coordinator["command"]) == 1
    assert "--no-access-log" in coordinator["command"]

    api = services["api"]
    assert api["image"] == "esragpro-p10r-backend:probe"
    assert api["build"] == {"context": ".", "dockerfile": "Dockerfile"}
    assert api["ports"] == ["127.0.0.1:18091:8000"]
    assert "volumes" not in api
    assert _command_workers(api["command"]) == 4
    assert "--no-access-log" in api["command"]

    assert api["depends_on"]["coordinator"]["condition"] == "service_healthy"
    assert api["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "-f",
        "http://localhost:8000/health",
    ]
    assert coordinator["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "-f",
        "http://localhost:8001/openapi.json",
    ]

    networks = payload["networks"]
    assert isinstance(networks, dict)
    assert set(networks) == {
        "esragpro_esragpro-internal",
        "esragpro-memory_mem-internal",
    }
    assert all(networks[name]["external"] is True for name in networks)

    for service_name in ("coordinator", "api"):
        service = services[service_name]
        assert set(service["networks"]) == {
            "esragpro_esragpro-internal",
            "esragpro-memory_mem-internal",
        }
        env = service["environment"]
        assert env["DATABASE_URL"] == (
            "postgresql+asyncpg://postgres:esragpro2026secure@esragpro-postgres:"
            "5432/esragpro_test"
        )
        assert env["ES_HOST"] == "http://esragpro-mem-es:9200"
        assert env["WORLD_MEMORY_EVENT_HISTORY_ENABLED"] == "true"
        # P10-AH parameterized this env var with a Compose default. Accept the
        # exact preserved expression (resolves to world-memory-events-p10o-20260806)
        # and reject any other value.
        assert env["WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME"] == (
            "${WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME:-world-memory-events-p10o-20260806}"
        )
        assert env["NEO4J_ENABLED"] == "false"
        assert env["WORLD_MEMORY_CANDIDATE_SOURCES_ENABLED"] == "false"
        assert env["MEMORY_CANDIDATE_SOURCES_ENABLED"] == "false"

    coordinator_env = coordinator["environment"]
    assert "WORLD_MEMORY_EVENT_HISTORY_COORDINATOR_URL" not in coordinator_env
    assert api["environment"]["WORLD_MEMORY_EVENT_HISTORY_COORDINATOR_URL"] == (
        "http://coordinator:8001/internal/world-memory/event-history/query"
    )
