from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from app.memory_core.world_memory_event_history_asgi_diagnostics import (
    ASGI_DIAGNOSTIC_HEADER_NAME,
    ASGI_DIAGNOSTICS_PATH,
    ASGIDiagnosticSample,
    ASGIDiagnosticsSnapshot,
)


def _load_benchmark() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / ("benchmark_world_memory_external_stages.py")
    )
    spec = importlib.util.spec_from_file_location("p10at_benchmark", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _server_sample(diagnostic_id: str, *, complete: bool = True) -> ASGIDiagnosticSample:
    return ASGIDiagnosticSample(
        diagnostic_id=diagnostic_id,
        asgi_entry_ns=100,
        handler_entry_ns=200,
        handler_exit_ns=500,
        response_send_complete_ns=900,
        complete=complete,
    )


def test_benchmark_joins_exact_ids_out_of_order_and_ignores_unrelated() -> None:
    module = _load_benchmark()
    ids = ("measured-1", "measured-2")
    measured = (
        module.Sample(stages=(10.0,), diagnostic_id=ids[0]),
        module.Sample(stages=(20.0,), diagnostic_id=ids[1]),
    )
    snapshot = ASGIDiagnosticsSnapshot(
        samples=(
            _server_sample("unrelated"),
            ASGIDiagnosticSample(
                diagnostic_id=ids[1],
                asgi_entry_ns=200_000_000,
                handler_entry_ns=202_000_000,
                handler_exit_ns=207_000_000,
                response_send_complete_ns=217_000_000,
                complete=True,
            ),
            ASGIDiagnosticSample(
                diagnostic_id=ids[0],
                asgi_entry_ns=100_000_000,
                handler_entry_ns=101_000_000,
                handler_exit_ns=104_000_000,
                response_send_complete_ns=108_000_000,
                complete=True,
            ),
        )
    )
    stages, error = module._validate_asgi_diagnostics(snapshot, measured, ids)
    assert error is None
    assert stages is not None
    measured_by_id = {sample.diagnostic_id: sample for sample in measured}
    server_by_id = {sample.diagnostic_id: sample for sample in snapshot.samples}
    assert tuple(
        module._asgi_stage_values(server_by_id[identifier], measured_by_id[identifier].stages[0])
        for identifier in ids
    ) == ((1.0, 3.0, 4.0, 8.0, 2.0), (2.0, 5.0, 10.0, 17.0, 3.0))
    assert stages == {
        "admission_ms": {"p50": 1.0, "p95": 2.0, "p99": 2.0},
        "handler_ms": {"p50": 3.0, "p95": 5.0, "p99": 5.0},
        "response_send_ms": {"p50": 4.0, "p95": 10.0, "p99": 10.0},
        "server_total_ms": {"p50": 8.0, "p95": 17.0, "p99": 17.0},
        "outside_asgi_ms": {"p50": 2.0, "p95": 3.0, "p99": 3.0},
    }


@pytest.mark.parametrize(
    "samples",
    [
        (
            _server_sample("measured-1"),
            _server_sample("measured-1"),
            _server_sample("measured-2"),
        ),
        (_server_sample("measured-1"),),
        (
            _server_sample("measured-1", complete=False),
            _server_sample("measured-2"),
        ),
    ],
)
def test_benchmark_fail_closed_on_duplicate_missing_or_incomplete(
    samples: tuple[ASGIDiagnosticSample, ...],
) -> None:
    module = _load_benchmark()
    ids = ("measured-1", "measured-2")
    measured = tuple(module.Sample(stages=(2.0,), diagnostic_id=identifier) for identifier in ids)
    stages, error = module._validate_asgi_diagnostics(
        ASGIDiagnosticsSnapshot(samples=samples), measured, ids
    )
    assert stages is None
    assert error is not None


def test_benchmark_fail_closed_on_negative_outside_asgi() -> None:
    module = _load_benchmark()
    sample = _server_sample("measured-1")
    measured = (module.Sample(stages=(0.0001,), diagnostic_id="measured-1"),)
    stages, error = module._validate_asgi_diagnostics(
        ASGIDiagnosticsSnapshot(samples=(sample,)), measured, ("measured-1",)
    )
    assert stages is None
    assert error == "diagnostics:server_sample_invalid"


@pytest.mark.anyio
async def test_benchmark_uses_separate_warmup_id_and_reports_server_stages() -> None:
    module = _load_benchmark()
    posted_ids: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted_ids.append(request.headers[ASGI_DIAGNOSTIC_HEADER_NAME])
            return httpx.Response(
                200,
                json={
                    "entries": [],
                    "candidate_count": 0,
                    "candidate_latency_ms": 0.0,
                    "refusal_reason": None,
                },
            )
        samples = [
            _server_sample(identifier).model_dump(mode="json")
            for identifier in reversed(posted_ids[1:])
        ]
        samples.append(_server_sample("unrelated").model_dump(mode="json"))
        return httpx.Response(200, json={"samples": samples})

    config = module.BenchmarkConfig(
        surface="coordinator",
        base_url="http://bench",
        requests=2,
        concurrency=1,
        event_time_lte=datetime(2026, 1, 1, tzinfo=timezone.utc),
        diagnostics_url="http://bench" + ASGI_DIAGNOSTICS_PATH,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://bench"
    ) as client:
        report = await module.run_benchmark(client, config)

    assert len(posted_ids) == 3
    assert posted_ids[0] != posted_ids[1]
    assert len(set(posted_ids[1:])) == 2
    assert report["errors"] == 0
    assert report["server_stages"] is not None
    assert set(report["server_stages"]) == set(module.ASGI_STAGE_NAMES)
    assert module.benchmark_exit_code(report) == 0
