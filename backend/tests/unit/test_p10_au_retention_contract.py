from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from app.memory_core.world_memory_event_history_asgi_diagnostics import (
    ASGIDiagnosticSample,
    ASGIDiagnosticsSnapshot,
)


def _load_benchmark():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / ("benchmark_world_memory_external_stages.py")
    )
    spec = importlib.util.spec_from_file_location("p10au_retention_benchmark", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _snapshot(*identifiers: str) -> ASGIDiagnosticsSnapshot:
    return ASGIDiagnosticsSnapshot(
        samples=tuple(
            ASGIDiagnosticSample(
                diagnostic_id=identifier,
                asgi_entry_ns=1,
                handler_entry_ns=2,
                handler_exit_ns=3,
                response_send_complete_ns=4,
                complete=True,
            )
            for identifier in identifiers
        )
    )


def _config(module, *, diagnostics: bool = True):
    return module.BenchmarkConfig(
        surface="coordinator",
        requests=100 if diagnostics else 1,
        diagnostics_url="http://bench/diag" if diagnostics else None,
    )


@pytest.mark.anyio
async def test_retention_brackets_warmup_and_serializes_exact_measured_ids(monkeypatch) -> None:
    module = _load_benchmark()
    config = _config(module)
    events: list[str] = []
    measured_ids: list[str] = []
    fetch_count = 0

    async def fetch(_client, _url):
        nonlocal fetch_count
        fetch_count += 1
        events.append(f"fetch-{fetch_count}")
        if fetch_count == 1:
            return _snapshot(), None, False
        return _snapshot("warmup", *measured_ids), None, False

    async def request(_client, _config, _index, diagnostic_id=None, **_kwargs):
        events.append("warmup")
        assert diagnostic_id is not None
        return module.Sample(status="ok", stages=(1.0, 0.5, 0.5), diagnostic_id=diagnostic_id)

    async def collect(_client, _config, diagnostic_ids=None):
        events.append("collect-start")
        assert diagnostic_ids is not None
        measured_ids.extend(diagnostic_ids)
        samples = tuple(
            module.Sample(status="ok", stages=(1.0, 0.5, 0.5), diagnostic_id=identifier)
            for identifier in diagnostic_ids
        )
        events.append("collect-end")
        return samples

    monkeypatch.setattr(module, "_fetch_asgi_diagnostics", fetch)
    monkeypatch.setattr(module, "_request_once", request)
    monkeypatch.setattr(module, "_collect", collect)

    report = await module._run_benchmark_with_client(object(), config)

    retention = report["diagnostic_retention"]
    assert report["errors"] == 0
    assert module.benchmark_exit_code(report) == 0
    assert events == ["fetch-1", "warmup", "collect-start", "collect-end", "fetch-2"]
    assert retention is not None
    assert len(retention["measured_ids"]) == 100
    assert len(set(retention["measured_ids"])) == 100
    assert "warmup" not in retention["measured_ids"]
    assert retention["measured_ids"] == measured_ids
    for snapshot in (retention["before"], retention["after"]):
        assert "cursor" not in snapshot
        assert snapshot["count"] == len(snapshot["samples"])
    assert retention["before"]["count"] == 0
    assert retention["after"]["count"] == 101


@pytest.mark.anyio
@pytest.mark.parametrize("failed_call", ["before", "after"])
async def test_retention_snapshot_failure_fails_closed(monkeypatch, failed_call: str) -> None:
    module = _load_benchmark()
    config = _config(module)
    calls = 0
    events: list[str] = []

    async def fetch(_client, _url):
        nonlocal calls
        calls += 1
        if calls == (1 if failed_call == "before" else 2):
            return None, "http_503", False
        return _snapshot(), None, False

    async def request(_client, _config, _index, diagnostic_id=None, **_kwargs):
        events.append("warmup")
        return module.Sample(status="ok", stages=(1.0, 0.5, 0.5), diagnostic_id=diagnostic_id)

    async def collect(_client, _config, diagnostic_ids=None):
        events.append("collect")
        return tuple(
            module.Sample(status="ok", stages=(1.0, 0.5, 0.5), diagnostic_id=identifier)
            for identifier in (diagnostic_ids or ())
        )

    monkeypatch.setattr(module, "_fetch_asgi_diagnostics", fetch)
    monkeypatch.setattr(module, "_request_once", request)
    monkeypatch.setattr(module, "_collect", collect)

    report = await module._run_benchmark_with_client(object(), config)

    assert report["errors"] == 1
    assert report["diagnostic_retention"] is None
    assert module.benchmark_exit_code(report) == 1
    assert events == [] if failed_call == "before" else events == ["warmup", "collect"]


@pytest.mark.anyio
async def test_diagnostics_disabled_keeps_retention_out_of_report(monkeypatch) -> None:
    module = _load_benchmark()
    config = _config(module, diagnostics=False)

    async def fetch(*_args, **_kwargs):
        raise AssertionError("diagnostics fetch must stay disabled")

    async def request(_client, _config, _index, diagnostic_id=None, **_kwargs):
        return module.Sample(status="ok", stages=(1.0, 0.5, 0.5), diagnostic_id=diagnostic_id)

    async def collect(*_args, **_kwargs):
        return (module.Sample(status="ok", stages=(1.0, 0.5, 0.5)),)

    monkeypatch.setattr(module, "_fetch_asgi_diagnostics", fetch)
    monkeypatch.setattr(module, "_request_once", request)
    monkeypatch.setattr(module, "_collect", collect)

    report = await module._run_benchmark_with_client(object(), config)

    assert report["diagnostic_retention"] is None
    assert module.benchmark_exit_code(report) == 0


def test_main_emits_retention_through_json_boundary(monkeypatch, capsys) -> None:
    module = _load_benchmark()
    config = module.BenchmarkConfig(
        surface="coordinator", requests=1, diagnostics_url="http://bench/diag"
    )
    retention = {
        "before": module._serialize_diagnostic_snapshot(_snapshot()),
        "after": module._serialize_diagnostic_snapshot(_snapshot("warmup")),
        "measured_ids": ["measured-1"],
    }
    measured = (module.Sample(status="ok", stages=(1.0, 0.5, 0.5)),)
    server_stages = {"cross_clock": False}

    async def fake_run(_client: httpx.AsyncClient, _config):
        return module._report(
            config,
            measured,
            (),
            False,
            None,
            server_stages,
            retention,
        )

    monkeypatch.setattr(module, "run_benchmark", fake_run)
    monkeypatch.setattr(sys, "argv", ["benchmark", "--diagnostics-url", "http://bench/diag"])

    assert module.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostic_retention"] == retention
    assert "cursor" not in output["diagnostic_retention"]["before"]
