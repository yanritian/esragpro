from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from pydantic import JsonValue


def _load_diagnostic() -> ModuleType:
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / ("benchmark_world_memory_asgi_boundary.py")
    )
    spec = importlib.util.spec_from_file_location("benchmark_world_memory_asgi_boundary", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_repeated_module_loads_keep_one_backend_path_entry() -> None:
    backend_root = str(Path(__file__).resolve().parents[2])
    original_path = sys.path.copy()
    try:
        sys.path.extend((backend_root, backend_root))
        for _ in range(3):
            _ = _load_diagnostic()
        assert sys.path.count(backend_root) == 1
    finally:
        sys.path[:] = original_path


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("missing_optional", "unexpected"))
async def test_raw_response_keys_must_match_schema(mutation: str) -> None:
    module = _load_diagnostic()
    payload: dict[str, JsonValue] = {
        "query_id": "query-id",
        "items": [],
        "envelopes": [],
        "token_budget": 4000,
        "token_used": 0,
        "status": "no_match",
        "refusal_reason": "no_matching_items",
        "candidate_sources": [],
        "latency_ms": 0.0,
    }
    if mutation == "missing_optional":
        _ = payload.pop("candidate_sources")
    else:
        payload["unexpected"] = True

    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport, base_url="http://asgi") as client:
        sample = await module._request_once(client, module.DiagnosticConfig())

    assert sample.error == "response_fields_incompatible"
    assert sample.schema_errors


@pytest.mark.asyncio
async def test_diagnostic_json_contract_reports_boundary_metrics() -> None:
    module = _load_diagnostic()
    report = await module.run_diagnostic(
        module.DiagnosticConfig(
            requests=2,
            concurrency=1,
            max_items=2,
            query="asgi boundary",
        )
    )

    payload = report.to_json()
    assert payload["route"] == "/api/v1/world-memory/query"
    assert payload["samples"] == 2
    assert payload["concurrency"] == 1
    assert payload["errors"] == 0
    assert payload["status_counts"] == {"no_match": 2}
    assert payload["refusal_reason_counts"] == {"no_matching_items": 2}
    assert payload["schema_compatibility"] == {
        "response_fields": True,
        "errors": [],
    }
    for metric in (
        "total_latency_ms",
        "client_latency_ms",
        "handler_latency_ms",
        "boundary_overhead_ms",
    ):
        values = payload[metric]
        assert set(values) == {"p50", "p95", "p99"}
        assert all(isinstance(values[key], float) for key in values)


def test_cli_exit_code_requires_complete_compatible_report() -> None:
    module = _load_diagnostic()
    config = module.DiagnosticConfig(requests=1)
    sample = module._Sample(1.0, 1.0, 0.0, "no_match", "no_matching_items")
    healthy = module._report(config, [sample])
    failures = (
        module._report(
            config,
            [module._Sample(1.0, 1.0, None, None, None, error="transport")],
        ),
        module._report(module.DiagnosticConfig(requests=2), [sample]),
        module._report(
            config,
            [
                module._Sample(
                    1.0,
                    1.0,
                    0.0,
                    "no_match",
                    "no_matching_items",
                    schema_errors=("missing:status",),
                )
            ],
        ),
    )

    assert module.diagnostic_exit_code(healthy) == 0
    assert all(module.diagnostic_exit_code(report) != 0 for report in failures)


@pytest.mark.asyncio
async def test_warmup_is_excluded_and_warmup_failure_stops_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_diagnostic()
    config = module.DiagnosticConfig(requests=3)
    calls: list[int] = []

    async def fake_request(_client: httpx.AsyncClient, _config):
        calls.append(len(calls))
        return module._Sample(1.0, 1.0, 0.0, "no_match", "no_matching_items")

    monkeypatch.setattr(module, "_request_once", fake_request)
    async with httpx.AsyncClient() as client:
        report = await module._measure(client, config)

    assert len(calls) == config.requests + 1
    assert report.samples == config.requests
    assert report.errors == 0

    calls.clear()

    async def failed_warmup(_client: httpx.AsyncClient, _config):
        calls.append(len(calls))
        if len(calls) == 1:
            return module._Sample(1.0, 1.0, None, None, None, error="warmup")
        return module._Sample(1.0, 1.0, 0.0, "no_match", "no_matching_items")

    monkeypatch.setattr(module, "_request_once", failed_warmup)
    async with httpx.AsyncClient() as client:
        failed_report = await module._measure(client, config)

    assert len(calls) == 1
    assert failed_report.samples == 0
    assert failed_report.errors == 1
    assert failed_report.error_messages == ("warmup",)


@pytest.mark.asyncio
async def test_c16_contract_honors_bound_and_counts_deterministic_responses() -> None:
    module = _load_diagnostic()
    report = await module.run_diagnostic(
        module.DiagnosticConfig(
            requests=4,
            concurrency=16,
            max_items=2,
            query="asgi boundary",
        )
    )

    assert report.samples == 4
    assert report.errors == 0
    assert report.status_counts == {"no_match": 4}
    assert report.refusal_reason_counts == {"no_matching_items": 4}
