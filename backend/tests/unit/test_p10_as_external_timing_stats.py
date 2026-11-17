from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from pydantic import JsonValue

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


def _coordinator_payload(
    *, candidate_ms: float = 5.0, refusal_reason: str | None = None
) -> dict[str, JsonValue]:
    return {
        "entries": [],
        "candidate_count": 1,
        "candidate_latency_ms": candidate_ms,
        "refusal_reason": refusal_reason,
    }


def _timing_payload(sequence: int, role: str) -> dict[str, JsonValue]:
    return {
        "sequence": sequence,
        "role": role,
        "caller_wait_ms": 1.0,
        "leader_execution_ms": 2.0,
        "retrieval_ms": 3.0,
        "readiness_ms": 0.5,
        "candidate_ms": 1.0,
        "authority_ms": 0.75,
        "query_unattributed_ms": 0.75,
        "preparation_ms": 0.25,
        "leader_unattributed_ms": 0.5,
    }


def _stats_payload(
    *,
    leader_count: int,
    in_flight: int,
    join_count: int = 0,
    last_sequence: int = 0,
    timing_samples: list[JsonValue] | None = None,
) -> dict[str, JsonValue]:
    return {
        "leader_count": leader_count,
        "in_flight": in_flight,
        "join_count": join_count,
        "last_sequence": last_sequence,
        "timing_samples": timing_samples or [],
    }


@pytest.mark.anyio
async def test_coordinator_stats_filters_samples_after_before_sequence_and_summarizes_roles() -> (
    None
):
    module = _load_benchmark()
    stats_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stats_calls
        if request.method == "GET":
            stats_calls += 1
            if stats_calls == 1:
                return httpx.Response(
                    200,
                    json=_stats_payload(
                        leader_count=20,
                        join_count=30,
                        last_sequence=100,
                        timing_samples=[_timing_payload(100, "leader")],
                        in_flight=0,
                    ),
                )
            return httpx.Response(
                200,
                json=_stats_payload(
                    leader_count=22,
                    join_count=31,
                    last_sequence=103,
                    timing_samples=[
                        _timing_payload(100, "leader"),
                        _timing_payload(101, "leader"),
                        _timing_payload(102, "joiner"),
                        _timing_payload(103, "leader"),
                    ],
                    in_flight=0,
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

    assert report["errors"] == 0
    assert report["stats"] is not None
    assert report["stats"]["sample_count"] == 3
    assert report["stats"]["role_counts"] == {"leader": 2, "joiner": 1}
    assert report["stats"]["leader_count_delta"] == 2
    assert report["stats"]["join_count_delta"] == 1
    assert set(report["stats"]["internal_stages"]) == {
        "caller_wait_ms",
        "leader_execution_ms",
        "retrieval_ms",
        "readiness_ms",
        "candidate_ms",
        "authority_ms",
        "query_unattributed_ms",
        "preparation_ms",
        "leader_unattributed_ms",
    }
    assert set(report["stats"]["internal_stages"]["retrieval_ms"]) == {
        "p50",
        "p95",
        "p99",
    }
    assert module.benchmark_exit_code(report) == 0


@pytest.mark.anyio
async def test_coordinator_stats_sample_count_mismatch_fails_closed() -> None:
    module = _load_benchmark()
    stats_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stats_calls
        if request.method == "GET":
            stats_calls += 1
            return httpx.Response(
                200,
                json=_stats_payload(
                    leader_count=10 if stats_calls == 1 else 12,
                    join_count=20 if stats_calls == 1 else 21,
                    last_sequence=40 if stats_calls == 1 else 42,
                    timing_samples=(
                        [_timing_payload(40, "leader")]
                        if stats_calls == 1
                        else [_timing_payload(41, "leader")]
                    ),
                    in_flight=0,
                ),
            )
        return httpx.Response(200, json=_coordinator_payload())

    config = _config(
        module,
        surface="coordinator",
        requests=2,
        stats_url="http://bench/stats",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://bench"
    ) as client:
        report = await module.run_benchmark(client, config)

    assert report["errors"] >= 1
    assert module.benchmark_exit_code(report) != 0


def test_coordinator_stats_requires_an_exact_contiguous_sequence_delta() -> None:
    module = _load_benchmark()
    before = module.EventHistorySingleflightStats(
        leader_count=10,
        join_count=20,
        in_flight=0,
        last_sequence=100,
        timing_samples=(),
    )
    after = module.EventHistorySingleflightStats(
        leader_count=13,
        join_count=20,
        in_flight=0,
        last_sequence=104,
        timing_samples=tuple(
            module.EventHistoryTimingSample.model_validate(_timing_payload(sequence, "leader"))
            for sequence in (101, 103, 104)
        ),
    )

    stats, error = module._validate_internal_stats(before, after, expected_samples=3)

    assert stats is None
    assert error == "stats:sequence_delta_mismatch"
