from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.memory_core.world_memory_event_history_asgi_diagnostics import (
    ASGIDiagnosticSample,
    ASGIDiagnosticsSnapshot,
)
from starlette.types import Receive, Scope, Send


def _load_benchmark():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / ("benchmark_world_memory_external_stages.py")
    )
    spec = importlib.util.spec_from_file_location("p10au_rt4_rt6_benchmark", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_TRACE_NAMES = (
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


def _events(module, timestamps: tuple[int, ...]):
    return tuple(
        module.HttpTraceEvent(name=name, timestamp_ns=timestamp)
        for name, timestamp in zip(_TRACE_NAMES, timestamps, strict=True)
    )


def _clock(module):
    return module.ClientClockMetadata(
        operating_system="Linux",
        boot_id="boot-a",
        perf_counter_implementation="clock_gettime(CLOCK_MONOTONIC)",
        perf_counter_resolution=1e-9,
        perf_counter_monotonic=True,
        perf_counter_adjustable=False,
    )


def _snapshot() -> ASGIDiagnosticsSnapshot:
    return ASGIDiagnosticsSnapshot(
        samples=(
            ASGIDiagnosticSample(
                diagnostic_id="id-a",
                asgi_entry_ns=2_000,
                handler_entry_ns=2_500,
                handler_exit_ns=3_000,
                response_send_complete_ns=4_000,
                complete=True,
            ),
        ),
        snapshot_captured_ns=3_000,
        operating_system="Linux",
        boot_id="boot-a",
        perf_counter_implementation="clock_gettime(CLOCK_MONOTONIC)",
        perf_counter_resolution=1e-9,
        perf_counter_monotonic=True,
        perf_counter_adjustable=False,
        fetch_start_ns=2_900,
        fetch_end_ns=3_100,
    )


def _sample(module, *, complete_ns: int = 5_500, clock=None, events=None):
    timing = module.ClientTiming(
        task_ready_ns=0,
        limiter_acquired_ns=0,
        client_request_start_ns=1_000,
        request_built_ns=1_100,
        client_complete_ns=complete_ns,
        trace_events=events or _events(module, tuple(range(1_200, 1_320, 10))),
        clock_metadata=_clock(module) if clock is None else clock,
    )
    return module.Sample(
        stages=(4.5, 2.0, 2.5),
        diagnostic_id="id-a",
        client_timing=timing,
    )


def test_http_trace_rejects_unsupported_wire_version_fail_closed() -> None:
    module = _load_benchmark()

    parsed, error = module._validate_http_trace(
        _events(module, tuple(range(1_200, 1_320, 10))),
        http_version="HTTP/2",
    )

    assert parsed is None
    assert error == "trace:unsupported_http_version"


@pytest.mark.asyncio
async def test_actual_unsupported_transport_fails_closed_before_requests(monkeypatch) -> None:
    module = _load_benchmark()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("unsupported transport must be rejected before dispatch")

    unsupported = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    supplied = httpx.AsyncClient()
    monkeypatch.setattr(module, "_new_diagnostic_client", lambda: unsupported)
    try:
        assert module._diagnostic_capability_error(unsupported) == (
            "diagnostics:unsupported_transport"
        )
        report = await module.run_benchmark(
            supplied,
            module.BenchmarkConfig(
                surface="coordinator",
                requests=1,
                diagnostics_url="http://bench/diag",
            ),
        )
    finally:
        await supplied.aclose()

    assert report["errors"] == 1
    assert report["samples"] == 0
    assert report["diagnostics_configured"] is True
    assert report["client_stages"] is None


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("operating_system", "Windows", None),
        ("boot_id", "boot-b", "diagnostics:clock_metadata_mismatch"),
        ("perf_counter_implementation", "other-clock", "diagnostics:clock_metadata_mismatch"),
        ("perf_counter_resolution", 2e-9, "diagnostics:clock_metadata_mismatch"),
        ("perf_counter_monotonic", False, "diagnostics:clock_metadata_mismatch"),
        ("perf_counter_adjustable", True, "diagnostics:clock_metadata_mismatch"),
    ],
)
def test_shared_clock_field_mismatch_never_emits_cross_clock(
    field: str, value: str | float | bool, expected: str | None
) -> None:
    module = _load_benchmark()
    clock = replace(_clock(module), **{field: value})

    stages, error = module._validate_asgi_diagnostics(
        _snapshot(), (_sample(module, clock=clock),), ("id-a",)
    )

    assert error == expected
    assert stages is not None
    assert stages["cross_clock"] is False
    assert "pre_asgi_ms" not in stages
    assert "post_send_ms" not in stages


def test_shared_clock_rejects_negative_post_send() -> None:
    module = _load_benchmark()

    stages, error = module._validate_asgi_diagnostics(
        _snapshot(), (_sample(module, complete_ns=3_500),), ("id-a",)
    )

    assert error == "diagnostics:post_send_negative"
    assert stages is not None
    assert stages["cross_clock"] is False
    assert "pre_asgi_ms" not in stages
    assert "post_send_ms" not in stages


def test_shared_clock_rejects_identity_mismatch_without_cross_clock_output(monkeypatch) -> None:
    module = _load_benchmark()
    monkeypatch.setattr(module, "_server_stage_values_ns", lambda _sample: (500, 500, 500, 999))

    stages, error = module._validate_asgi_diagnostics(_snapshot(), (_sample(module),), ("id-a",))

    assert error == "diagnostics:cross_clock_identity_mismatch"
    assert stages is not None
    assert stages["cross_clock"] is False
    assert "pre_asgi_ms" not in stages
    assert "post_send_ms" not in stages


def test_overlapping_request_body_is_client_only_and_identity_is_exact() -> None:
    module = _load_benchmark()
    events = _events(
        module,
        (1_200, 1_210, 1_300, 1_310, 1_900, 2_100, 3_500, 3_510, 3_700, 3_710, 4_000, 4_010),
    )
    parsed, trace_error = module._validate_http_trace(events)
    assert trace_error is None
    assert parsed is not None
    assert parsed.request_body_ns == 200

    measured = (_sample(module, events=events),)
    stages, error = module._validate_asgi_diagnostics(_snapshot(), measured, ("id-a",))
    assert error is None
    assert stages is not None
    report = module._report(
        module.BenchmarkConfig(
            surface="coordinator", requests=1, diagnostics_url="http://bench/diag"
        ),
        measured,
        (),
        False,
        None,
        server_stages=stages,
    )
    server = report["server_stages"]
    assert server is not None
    assert set(server) == {*module.SHARED_ASGI_STAGE_NAMES, "cross_clock"}
    assert "request_body_ms" not in server
    assert report["client_stages"] is not None
    assert "request_body_ms" in report["client_stages"]

    independent_sum = (
        server["pre_asgi_ms"]["p50"]
        + server["server_total_ms"]["p50"]
        + server["post_send_ms"]["p50"]
    )
    external_wall = server["external_wall_ms"]["p50"]
    assert external_wall == pytest.approx(independent_sum)
    assert external_wall != pytest.approx(independent_sum + parsed.request_body_ns / 1e6)
