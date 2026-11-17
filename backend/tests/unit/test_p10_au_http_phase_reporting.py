from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.memory_core.world_memory_event_history_asgi_diagnostics import (
    ASGIDiagnosticCollector,
    ASGIDiagnosticSample,
    ASGIDiagnosticsSnapshot,
)


def _load_benchmark():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "benchmark_world_memory_external_stages.py"
    )
    spec = importlib.util.spec_from_file_location("p10au_benchmark_reporting", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _trace_names() -> tuple[str, ...]:
    return (
        "connection.connect_tcp.started",
        "connection.connect_tcp.complete",
        "http11.send_request_headers.started",
        "http11.send_request_headers.complete",
        "http11.send_request_body.started",
        "http11.send_request_body.complete",
        "http11.receive_response_headers.started",
        "http11.receive_response_headers.complete",
        "http11.receive_response_body.started",
        "http11.receive_response_body.complete",
        "http11.response_closed.started",
        "http11.response_closed.complete",
    )


def _server_sample(identifier: str) -> ASGIDiagnosticSample:
    return ASGIDiagnosticSample(
        diagnostic_id=identifier,
        asgi_entry_ns=2_000,
        handler_entry_ns=2_500,
        handler_exit_ns=3_000,
        response_send_complete_ns=4_000,
        complete=True,
    )


def _shared_snapshot(samples: tuple[ASGIDiagnosticSample, ...]) -> ASGIDiagnosticsSnapshot:
    return ASGIDiagnosticsSnapshot(
        samples=samples,
        snapshot_captured_ns=3_000,
        operating_system="Linux",
        boot_id="boot-a",
        perf_counter_implementation="QueryPerformanceCounter",
        perf_counter_resolution=1e-9,
        perf_counter_monotonic=True,
        perf_counter_adjustable=False,
        fetch_start_ns=2_900,
        fetch_end_ns=3_100,
    )


def test_cross_clock_mode_keeps_local_stages_without_subtraction() -> None:
    module = _load_benchmark()
    measured = (
        module.Sample(
            stages=(4.0,),
            diagnostic_id="id-a",
            client_timing=module.ClientTiming(100, 100, 100, 110, 5_000, ()),
        ),
    )

    stages, error = module._validate_asgi_diagnostics(
        ASGIDiagnosticsSnapshot(samples=(_server_sample("id-a"),), operating_system="Windows"),
        measured,
        ("id-a",),
    )

    assert error is None
    assert stages is not None
    assert stages["cross_clock"] is False
    assert set(stages) == {
        "admission_ms",
        "handler_ms",
        "response_send_ms",
        "server_total_ms",
        "cross_clock",
    }
    assert "outside_asgi_ms" not in stages
    assert "pre_asgi_ms" not in stages
    assert "post_send_ms" not in stages


def test_snapshot_captures_shared_clock_metadata_without_changing_samples() -> None:
    module = _load_benchmark()
    snapshot = ASGIDiagnosticCollector().snapshot()

    assert snapshot.samples == ()
    assert snapshot.snapshot_captured_ns is not None
    assert snapshot.operating_system
    assert snapshot.perf_counter_implementation
    assert snapshot.perf_counter_resolution is not None
    assert snapshot.perf_counter_monotonic is True
    assert snapshot.perf_counter_adjustable is False
    assert module.ASGIDiagnosticsSnapshot.model_fields["snapshot_captured_ns"]


def test_client_trace_timestamps_must_stay_inside_request_bounds() -> None:
    module = _load_benchmark()
    events = tuple(
        module.HttpTraceEvent(name=name, timestamp_ns=100 + index * 10)
        for index, name in enumerate(_trace_names())
    )

    assert (
        module._timing_error(module.ClientTiming(0, 0, 100, 150, 300, events), strict=True)
        == "diagnostics:client_trace_out_of_bounds"
    )
    assert (
        module._timing_error(module.ClientTiming(0, 0, 0, 50, 180, events), strict=True)
        == "diagnostics:client_trace_out_of_bounds"
    )


def test_missing_client_clock_metadata_disables_cross_clock() -> None:
    module = _load_benchmark()
    measured = (
        module.Sample(
            stages=(4.0,),
            diagnostic_id="id-a",
            client_timing=module.ClientTiming(0, 0, 1_000, 1_100, 5_000, ()),
        ),
    )

    stages, error = module._validate_asgi_diagnostics(
        _shared_snapshot((_server_sample("id-a"),)), measured, ("id-a",)
    )

    assert error is None
    assert stages is not None
    assert stages["cross_clock"] is False
    assert "outside_asgi_ms" not in stages
    assert "pre_asgi_ms" not in stages
    assert "post_send_ms" not in stages


def test_negative_local_outside_asgi_is_rejected() -> None:
    module = _load_benchmark()

    assert module._local_asgi_stage_values((500, 500, 500, 2_000), 1_000) is None


def test_windows_client_to_linux_server_falls_back_without_case_error() -> None:
    module = _load_benchmark()
    windows_clock = module.ClientClockMetadata(
        operating_system="Windows",
        boot_id=None,
        perf_counter_implementation="QueryPerformanceCounter",
        perf_counter_resolution=1e-9,
        perf_counter_monotonic=True,
        perf_counter_adjustable=False,
    )
    measured = (
        module.Sample(
            stages=(4.0,),
            diagnostic_id="id-a",
            client_timing=module.ClientTiming(0, 0, 1_000, 1_100, 5_000, (), windows_clock),
        ),
    )

    stages, error = module._validate_asgi_diagnostics(
        _shared_snapshot((_server_sample("id-a"),)), measured, ("id-a",)
    )

    assert error is None
    assert stages is not None
    assert stages["cross_clock"] is False
    assert "outside_asgi_ms" not in stages
    assert "pre_asgi_ms" not in stages
    assert "post_send_ms" not in stages


def test_report_emits_client_stages() -> None:
    module = _load_benchmark()
    events = tuple(
        module.HttpTraceEvent(name=name, timestamp_ns=400 + index * 10)
        for index, name in enumerate(_trace_names())
    )
    measured = (
        module.Sample(
            status="ok",
            stages=(4.0, 2.0, 1.0),
            diagnostic_id="id-a",
            client_timing=module.ClientTiming(100, 250, 300, 350, 1_300, events),
        ),
    )
    report = module._report(
        module.BenchmarkConfig(
            surface="coordinator", requests=1, diagnostics_url="http://bench/diag"
        ),
        measured,
        (),
        False,
        None,
    )

    assert report["client_stages"] is not None
    assert set(report["client_stages"]) <= set(module.CLIENT_STAGE_NAMES)
    assert all(name.endswith("_ms") for name in report["client_stages"])
    assert report["client_stages"]["limiter_wait_ms"] == {
        "p50": 0.00015,
        "p95": 0.00015,
        "p99": 0.00015,
    }
    assert report["client_stages"]["external_wall_ms"]["p50"] == 0.001


def test_shared_summary_uses_report_summary_percentiles() -> None:
    module = _load_benchmark()
    values = (1.0, 2.0, 3.0, 4.0)

    assert module._shared_summary(values) == module._summary(values)


@pytest.mark.anyio
async def test_diagnostic_capability_is_checked_before_warmup(monkeypatch) -> None:
    module = _load_benchmark()
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(500)

    config = module.BenchmarkConfig(
        surface="coordinator",
        requests=1,
        diagnostics_url="http://bench/diag",
        event_time_lte=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(module, "_new_diagnostic_client", lambda: client)
        monkeypatch.setattr(
            module,
            "_diagnostic_capability_error",
            lambda _client: "diagnostics:unsupported_httpx_version",
        )
        report = await module.run_benchmark(client, config)

    assert report["errors"] == 1
    assert report["samples"] == 0
    assert requests == []
