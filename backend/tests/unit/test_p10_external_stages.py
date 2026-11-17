from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from pydantic import JsonValue

from tests.unit.test_p10_as_external_timing_stats import _stats_payload, _timing_payload

ConfigValue = str | int | datetime | None


def _load_benchmark() -> ModuleType:
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "benchmark_world_memory_external_stages.py"
    )
    spec = importlib.util.spec_from_file_location("benchmark_world_memory_external_stages", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _config(module: ModuleType, **overrides: ConfigValue):
    values: dict[str, ConfigValue] = {
        "surface": "api",
        "base_url": "http://bench",
        "requests": 2,
        "concurrency": 1,
        "max_items": 10,
        "query": "historical",
        "query_mode": "identical",
        "event_time_lte": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "stats_url": None,
    }
    values.update(overrides)
    return module.BenchmarkConfig(**values)


def _api_payload(
    *,
    status: str = "ok",
    refusal_reason: str | None = None,
    internal_ms: float = 12.0,
    candidate_ms: float = 5.0,
) -> dict[str, JsonValue]:
    return {
        "query_id": "query-id",
        "items": [],
        "envelopes": [],
        "token_budget": 4000,
        "token_used": 0,
        "status": status,
        "refusal_reason": refusal_reason,
        "candidate_sources": [
            {
                "source": "elasticsearch_event_history",
                "status": "ok" if refusal_reason is None else "refused",
                "candidate_count": 1,
                "latency_ms": candidate_ms,
                "error": refusal_reason,
            }
        ],
        "latency_ms": internal_ms,
    }


def _coordinator_payload(
    *, candidate_ms: float = 5.0, refusal_reason: str | None = None
) -> dict[str, JsonValue]:
    return {
        "entries": [],
        "candidate_count": 1,
        "candidate_latency_ms": candidate_ms,
        "refusal_reason": refusal_reason,
    }


@pytest.mark.parametrize(
    ("wall", "internal", "candidate", "non_candidate", "boundary"),
    [(30.0, 12.0, 5.0, 7.0, 18.0), (4.0, 12.0, 5.0, 7.0, 0.0)],
)
def test_api_stage_arithmetic_is_clamped(
    wall: float,
    internal: float,
    candidate: float,
    non_candidate: float,
    boundary: float,
) -> None:
    module = _load_benchmark()

    stages = module.derive_api_stages(wall, internal, candidate)

    assert stages == (wall, internal, candidate, non_candidate, boundary)


def test_coordinator_stage_arithmetic_is_clamped() -> None:
    module = _load_benchmark()

    assert module.derive_coordinator_stages(30.0, 5.0)[2] == 25.0
    assert module.derive_coordinator_stages(4.0, 5.0)[2] == 0.0


def test_request_payload_preserves_identical_and_appends_unique_index() -> None:
    module = _load_benchmark()
    identical = _config(module, query_mode="identical")
    unique = _config(module, query_mode="unique")

    assert module.build_request_payload(identical, 3)["query"] == "historical"
    assert module.build_request_payload(unique, 3)["query"] == "historical #3"


def test_coordinator_request_payload_excludes_api_only_token_budget() -> None:
    module = _load_benchmark()

    payload = module.build_request_payload(_config(module, surface="coordinator"), 1)

    assert payload == {
        "query": "historical",
        "event_time_lte": "2026-01-01T00:00:00+00:00",
        "require_evidence": True,
        "max_items": 10,
    }


@pytest.mark.anyio
async def test_api_http_report_excludes_one_warmup_and_reports_stages() -> None:
    module = _load_benchmark()
    seen_queries: list[str] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        seen_queries.append("called")
        return httpx.Response(200, json=_api_payload())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://bench") as client:
        report = await module.run_benchmark(client, _config(module))

    assert report["samples"] == 2
    assert report["errors"] == 0
    assert len(seen_queries) == 3
    assert report["status_counts"] == {"ok": 2}
    assert report["refusal_reason_counts"] == {}
    assert set(report["stages"]) == {
        "external_wall_ms",
        "api_internal_ms",
        "candidate_ms",
        "coordinator_non_candidate_ms",
        "external_boundary_ms",
    }


@pytest.mark.anyio
async def test_warmup_refusal_fails_closed_and_is_not_measured() -> None:
    module = _load_benchmark()
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json=_api_payload(status="refused", refusal_reason="no_matching_items"),
            )
        return httpx.Response(200, json=_api_payload())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://bench"
    ) as client:
        report = await module.run_benchmark(client, _config(module))

    assert calls == 1
    assert report["samples"] == 0
    assert report["errors"] == 1
    assert module.benchmark_exit_code(report) != 0


@pytest.mark.anyio
async def test_raw_schema_failure_fails_closed() -> None:
    module = _load_benchmark()
    malformed = _api_payload()
    _ = malformed.pop("candidate_sources")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=malformed)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://bench"
    ) as client:
        report = await module.run_benchmark(client, _config(module))

    assert report["samples"] == 0
    assert report["errors"] == 1
    assert report["schema_compatible"] is False
    assert module.benchmark_exit_code(report) != 0


@pytest.mark.anyio
async def test_coordinator_stats_report_exact_leader_delta_and_in_flight_after() -> None:
    module = _load_benchmark()
    stats_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stats_calls
        if request.method == "GET":
            stats_calls += 1
            return httpx.Response(
                200,
                json=_stats_payload(
                    leader_count=10 if stats_calls == 1 else 13,
                    in_flight=0,
                    last_sequence=0 if stats_calls == 1 else 3,
                    timing_samples=(
                        []
                        if stats_calls == 1
                        else [_timing_payload(index, "leader") for index in range(1, 4)]
                    ),
                ),
            )
        return httpx.Response(200, json=_coordinator_payload())

    config = _config(
        module,
        surface="coordinator",
        requests=3,
        stats_url="http://bench/stats",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://bench"
    ) as client:
        report = await module.run_benchmark(client, config)

    assert stats_calls == 2
    assert report["stats"] is not None
    assert report["stats"]["leader_count_delta"] == 3
    assert report["stats"]["join_count_delta"] == 0
    assert report["stats"]["in_flight_after"] == 0
    assert module.benchmark_exit_code(report) == 0


@pytest.mark.anyio
async def test_concurrency_completes_all_requests() -> None:
    module = _load_benchmark()
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_coordinator_payload())

    config = _config(module, surface="coordinator", requests=5, concurrency=16)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://bench"
    ) as client:
        report = await module.run_benchmark(client, config)

    assert calls == 6
    assert report["samples"] == 5
    assert report["errors"] == 0
    assert report["concurrency"] == 16


@pytest.mark.anyio
async def test_invalid_stats_fails_closed() -> None:
    module = _load_benchmark()
    stats_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stats_calls
        if request.method == "GET":
            stats_calls += 1
            return httpx.Response(
                200,
                json=_stats_payload(
                    leader_count=10 if stats_calls == 1 else 9,
                    in_flight=0,
                ),
            )
        return httpx.Response(200, json=_coordinator_payload())

    config = _config(module, surface="coordinator", stats_url="http://bench/stats")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://bench"
    ) as client:
        report = await module.run_benchmark(client, config)

    assert report["errors"] >= 1
    assert module.benchmark_exit_code(report) != 0
