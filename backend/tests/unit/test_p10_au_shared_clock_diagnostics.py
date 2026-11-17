from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.memory_core.world_memory_event_history_asgi_diagnostics import (
    ASGIDiagnosticSample,
    ASGIDiagnosticsSnapshot,
)


def _load_benchmark():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "benchmark_world_memory_external_stages.py"
    )
    spec = importlib.util.spec_from_file_location("p10au_shared_clock_benchmark", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_HTTP11_TRACE = (
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


def _events(module, names: tuple[str, ...], start_ns: int = 100):
    return tuple(
        module.HttpTraceEvent(name=name, timestamp_ns=start_ns + index * 10)
        for index, name in enumerate(names)
    )


def _trace_names() -> tuple[str, ...]:
    return (
        "connection.connect_tcp.started",
        "connection.connect_tcp.complete",
        *_HTTP11_TRACE,
    )


def _server_sample(identifier: str, *, offset: int = 0) -> ASGIDiagnosticSample:
    return ASGIDiagnosticSample(
        diagnostic_id=identifier,
        asgi_entry_ns=2_000 + offset,
        handler_entry_ns=2_500 + offset,
        handler_exit_ns=3_000 + offset,
        response_send_complete_ns=4_000 + offset,
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


def _shared_client_clock(module):
    return module.ClientClockMetadata(
        operating_system="Linux",
        boot_id="boot-a",
        perf_counter_implementation="QueryPerformanceCounter",
        perf_counter_resolution=1e-9,
        perf_counter_monotonic=True,
        perf_counter_adjustable=False,
    )


def _measured_sample(module, identifier: str, wall_ms: float, timing, trace_start_ns: int):
    return module.Sample(
        stages=(wall_ms,),
        diagnostic_id=identifier,
        client_timing=module.ClientTiming(
            0,
            *timing,
            _events(module, _trace_names(), trace_start_ns),
            _shared_client_clock(module),
        ),
    )


def test_exact_id_shared_clock_join_defeats_positional_order() -> None:
    module = _load_benchmark()
    ids = ("id-a", "id-b")
    measured = (
        _measured_sample(module, "id-a", 4.0, (1_000, 1_000, 1_100, 5_000), 1_200),
        _measured_sample(module, "id-b", 6.0, (1_100, 1_100, 1_200, 7_100), 1_300),
    )
    snapshot = _shared_snapshot(
        (
            ASGIDiagnosticSample(
                diagnostic_id="id-b",
                asgi_entry_ns=2_600,
                handler_entry_ns=3_000,
                handler_exit_ns=4_200,
                response_send_complete_ns=5_000,
                complete=True,
            ),
            ASGIDiagnosticSample(
                diagnostic_id="id-a",
                asgi_entry_ns=2_000,
                handler_entry_ns=2_500,
                handler_exit_ns=3_000,
                response_send_complete_ns=4_000,
                complete=True,
            ),
        ),
    )

    stages, error = module._validate_asgi_diagnostics(snapshot, measured, ids)

    assert error is None
    assert stages is not None
    assert stages["pre_asgi_ms"] == {"p50": 0.001, "p95": 0.0015, "p99": 0.0015}
    assert stages["post_send_ms"] == {"p50": 0.001, "p95": 0.0021, "p99": 0.0021}


def test_client_timing_without_snapshot_clock_metadata_keeps_local_stages() -> None:
    module = _load_benchmark()
    measured = (_measured_sample(module, "id-a", 4.0, (1_000, 1_000, 1_100, 5_000), 1_100),)

    stages, error = module._validate_asgi_diagnostics(
        ASGIDiagnosticsSnapshot(samples=(_server_sample("id-a"),)),
        measured,
        ("id-a",),
    )

    assert error is None
    assert stages is not None
    assert stages["cross_clock"] is False
    assert set(stages) == {*module.LOCAL_ASGI_STAGE_NAMES, "cross_clock"}
    assert stages["server_total_ms"] == {"p50": 0.002, "p95": 0.002, "p99": 0.002}
    assert not {"outside_asgi_ms", "pre_asgi_ms", "post_send_ms"}.intersection(stages)


def test_incomparable_windows_client_does_not_validate_linux_fetch_bracket() -> None:
    module = _load_benchmark()
    sample = _server_sample("id-a")
    measured = (
        module.Sample(
            stages=(4.0,),
            diagnostic_id="id-a",
            client_timing=module.ClientTiming(
                1_000,
                1_000,
                1_000,
                1_100,
                5_000,
                (),
                module.ClientClockMetadata(
                    operating_system="Windows",
                    boot_id=None,
                    perf_counter_implementation="QueryPerformanceCounter",
                    perf_counter_resolution=1e-9,
                    perf_counter_monotonic=True,
                    perf_counter_adjustable=False,
                ),
            ),
        ),
    )
    snapshot = _shared_snapshot((sample,)).model_copy(
        update={"fetch_start_ns": 4_500, "fetch_end_ns": 4_600}
    )

    stages, error = module._validate_asgi_diagnostics(snapshot, measured, ("id-a",))

    assert error is None
    assert stages is not None
    assert stages["cross_clock"] is False
    assert not {"outside_asgi_ms", "pre_asgi_ms", "post_send_ms"}.intersection(stages)


@pytest.mark.parametrize("field", ["boot_id", "operating_system", "snapshot_captured_ns", "fetch"])
def test_shared_clock_fails_closed_on_clock_or_fetch_bracket_mismatch(field: str) -> None:
    module = _load_benchmark()
    sample = _server_sample("id-a")
    measured = (
        module.Sample(
            stages=(4.0,),
            diagnostic_id="id-a",
            client_timing=module.ClientTiming(
                1_000,
                1_000,
                1_000,
                1_100,
                5_000,
                (),
                _shared_client_clock(module),
            ),
        ),
    )
    snapshot = _shared_snapshot((sample,))
    if field == "boot_id":
        snapshot = snapshot.model_copy(update={"boot_id": "boot-b"})
    elif field == "operating_system":
        snapshot = snapshot.model_copy(update={"operating_system": "Windows"})
    elif field == "snapshot_captured_ns":
        snapshot = snapshot.model_copy(update={"snapshot_captured_ns": 9_000})
    else:
        snapshot = snapshot.model_copy(update={"fetch_start_ns": 4_500, "fetch_end_ns": 4_600})

    stages, error = module._validate_asgi_diagnostics(snapshot, measured, ("id-a",))

    if field == "operating_system":
        assert error is None
    elif field == "fetch":
        assert error == "diagnostics:fetch_bracket_mismatch"
    else:
        assert error is not None
    assert stages is None or stages.get("cross_clock") is False
    if stages is not None and stages.get("cross_clock") is False:
        assert "outside_asgi_ms" not in stages
        assert "pre_asgi_ms" not in stages
        assert "post_send_ms" not in stages


def test_request_body_overlap_is_not_an_additive_transport_partition() -> None:
    module = _load_benchmark()
    events = _events(module, _trace_names(), 1_200)
    timing = module.ClientTiming(0, 0, 1_000, 1_100, 5_000, events, _shared_client_clock(module))
    parsed, error = module._validate_http_trace(events)

    assert error is None
    assert parsed is not None
    assert parsed.request_body_ns == 10
    measured = (
        module.Sample(
            stages=(4.0,),
            diagnostic_id="id-a",
            client_timing=timing,
        ),
    )
    stages, error = module._validate_asgi_diagnostics(
        _shared_snapshot((_server_sample("id-a"),)), measured, ("id-a",)
    )

    assert error is None
    assert stages is not None
    assert (
        stages["pre_asgi_ms"]["p50"]
        + stages["server_total_ms"]["p50"]
        + stages["post_send_ms"]["p50"]
        == stages["external_wall_ms"]["p50"]
    )
    assert stages["external_wall_ms"]["p50"] == 0.004
