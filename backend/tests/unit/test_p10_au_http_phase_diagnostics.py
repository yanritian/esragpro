from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_benchmark():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / ("benchmark_world_memory_external_stages.py")
    )
    spec = importlib.util.spec_from_file_location("p10au_benchmark", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _events(module, names: tuple[str, ...], start_ns: int = 100):
    return tuple(
        module.HttpTraceEvent(name=name, timestamp_ns=start_ns + index * 10)
        for index, name in enumerate(names)
    )


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


def _trace_names(*, reused: bool = False, tls: bool = False) -> tuple[str, ...]:
    prefix = () if reused else ("connection.connect_tcp.started", "connection.connect_tcp.complete")
    tls_pair = ("connection.start_tls.started", "connection.start_tls.complete") if tls else ()
    return prefix + tls_pair + _HTTP11_TRACE


def test_limiter_wait_is_adjacent_and_excluded_from_external_wall() -> None:
    module = _load_benchmark()
    timing = module.ClientTiming(
        task_ready_ns=100,
        limiter_acquired_ns=250,
        client_request_start_ns=300,
        request_built_ns=350,
        client_complete_ns=1_300,
        trace_events=(),
    )

    values = module._client_stage_values(timing)

    assert values["limiter_wait_ns"] == 150
    assert values["external_wall_ns"] == 1_000
    pre_request_ns = timing.client_request_start_ns - timing.limiter_acquired_ns
    assert (
        values["limiter_wait_ns"] + pre_request_ns + values["external_wall_ns"]
        == timing.client_complete_ns - timing.task_ready_ns
    )
    assert values["external_wall_ns"] < timing.client_complete_ns - timing.task_ready_ns


def test_limiter_timestamp_order_includes_request_start() -> None:
    module = _load_benchmark()
    timing = module.ClientTiming(
        task_ready_ns=100,
        limiter_acquired_ns=350,
        client_request_start_ns=250,
        request_built_ns=400,
        client_complete_ns=600,
        trace_events=_events(module, _trace_names(), 410),
    )

    assert module._timing_error(timing, strict=True) == "diagnostics:limiter_timestamp_invalid"


@pytest.mark.parametrize("reused,tls", [(False, False), (True, False), (False, True)])
def test_http11_trace_fixtures_validate_exact_fresh_reused_and_tls_sequences(
    reused: bool, tls: bool
) -> None:
    module = _load_benchmark()

    parsed, error = module._validate_http_trace(
        _events(module, _trace_names(reused=reused, tls=tls))
    )

    assert error is None
    assert parsed is not None
    assert parsed.connect_ns is None if reused else parsed.connect_ns == 10
    assert parsed.tls_ns is None if not tls else parsed.tls_ns == 10
    assert parsed.response_closed_ns == 10


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "unknown", "failed", "retry", "out_of_order"],
)
def test_http_trace_rejects_missing_duplicate_unknown_failed_retry_and_order(
    mutation: str,
) -> None:
    module = _load_benchmark()
    names = list(_trace_names())
    if mutation == "missing":
        names.pop()
    elif mutation == "duplicate":
        names.insert(2, names[2])
    elif mutation == "unknown":
        names[2] = "http11.not_a_real_phase.started"
    elif mutation == "failed":
        names[2] = "http11.send_request_headers.failed"
    elif mutation == "retry":
        names.extend(("connection.connect_tcp.started", "connection.connect_tcp.complete"))
    else:
        names[2], names[3] = names[3], names[2]

    parsed, error = module._validate_http_trace(_events(module, tuple(names)))

    assert parsed is None
    assert error is not None
