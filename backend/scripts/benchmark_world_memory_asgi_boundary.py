"""ASGI timing diagnostic; run from backend/ with project Python."""

# Run from backend/: python scripts/benchmark_world_memory_asgi_boundary.py --requests 2 --concurrency 1

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Final

import anyio
import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError

backend_root = str(Path(__file__).resolve().parents[1])
sys.path[:] = [backend_root, *(path for path in sys.path if path != backend_root)]

ROUTE_PATH: Final = "/api/v1/world-memory/query"


@dataclass(frozen=True, slots=True)
class DiagnosticConfig:
    requests: int = 8
    concurrency: int = 1
    max_items: int = 10
    query: str = "asgi boundary"

    def validate(self) -> None:
        checks = (
            (1 <= self.requests <= 1000, "requests must be between 1 and 1000"),
            (1 <= self.concurrency <= 16, "concurrency must be between 1 and 16"),
            (1 <= self.max_items <= 200, "max_items must be between 1 and 200"),
            (
                bool(self.query.strip()) and len(self.query) <= 200,
                "query must be 1..200 nonblank characters",
            ),
        )
        for valid, message in checks:
            if not valid:
                raise argparse.ArgumentTypeError(message)


@dataclass(frozen=True, slots=True)
class LatencySummary:
    p50: float | None
    p95: float | None
    p99: float | None

    def to_json(self) -> dict[str, JsonValue]:
        return {"p50": self.p50, "p95": self.p95, "p99": self.p99}


@dataclass(frozen=True, slots=True)
class _Sample:
    total_ms: float
    client_ms: float
    handler_ms: float | None
    status: str | None
    refusal_reason: str | None
    schema_errors: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    requests: int
    samples: int
    concurrency: int
    total_latency: LatencySummary
    client_latency: LatencySummary
    handler_latency: LatencySummary
    boundary_overhead: LatencySummary
    status_counts: dict[str, int]
    refusal_reason_counts: dict[str, int]
    errors: int
    error_messages: tuple[str, ...]
    schema_compatible: bool
    schema_errors: tuple[str, ...]

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "route": ROUTE_PATH,
            "requests": self.requests,
            "samples": self.samples,
            "concurrency": self.concurrency,
            "total_latency_ms": self.total_latency.to_json(),
            "client_latency_ms": self.client_latency.to_json(),
            "handler_latency_ms": self.handler_latency.to_json(),
            "boundary_overhead_ms": self.boundary_overhead.to_json(),
            "status_counts": dict(self.status_counts),
            "refusal_reason_counts": dict(self.refusal_reason_counts),
            "errors": self.errors,
            "error_messages": list(self.error_messages),
            "schema_compatibility": {
                "response_fields": self.schema_compatible,
                "errors": list(self.schema_errors),
            },
        }


class _CliArgs(argparse.Namespace):
    requests: int = 8
    concurrency: int = 1
    max_items: int = 10
    query: str = "asgi boundary"


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    rank = max(1, math.ceil(percentile / 100.0 * len(values)))
    return sorted(values)[rank - 1]


def _summary(values: list[float]) -> LatencySummary:
    return LatencySummary(*(_percentile(values, p) for p in (50.0, 95.0, 99.0)))


def _schema_errors(actual: set[str], expected: set[str]) -> tuple[str, ...]:
    return (
        *(f"missing:{field}" for field in sorted(expected - actual)),
        *(f"unexpected:{field}" for field in sorted(actual - expected)),
    )


def _decode_response(
    response: httpx.Response,
    *,
    started: float,
    client_ms: float,
) -> _Sample:
    total_ms = max(0.0, (perf_counter() - started) * 1000.0)
    if response.status_code != 200:
        return _Sample(total_ms, client_ms, None, None, None, error=f"http_{response.status_code}")
    try:
        from app.api.world_memory_plane import WorldMemoryQueryResponse

        raw = TypeAdapter(dict[str, JsonValue]).validate_json(response.content)
        parsed = WorldMemoryQueryResponse.model_validate(raw)
    except ValidationError as exc:
        return _Sample(total_ms, client_ms, None, None, None, error=f"schema:{type(exc).__name__}")
    schema_errors = _schema_errors(set(raw), set(WorldMemoryQueryResponse.model_fields))
    return _Sample(
        total_ms=total_ms,
        client_ms=client_ms,
        handler_ms=max(0.0, parsed.latency_ms),
        status=parsed.status,
        refusal_reason=parsed.refusal_reason,
        schema_errors=schema_errors,
        error=None if not schema_errors else "response_fields_incompatible",
    )


async def _request_once(client: httpx.AsyncClient, config: DiagnosticConfig) -> _Sample:
    started = perf_counter()
    try:
        response = await client.post(
            ROUTE_PATH,
            json={
                "query": config.query,
                "max_items": config.max_items,
                "require_evidence": True,
                "token_budget": 4000,
            },
        )
    except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
        elapsed = max(0.0, (perf_counter() - started) * 1000.0)
        return _Sample(elapsed, elapsed, None, None, None, error=type(exc).__name__)
    client_ms = max(0.0, (perf_counter() - started) * 1000.0)
    return _decode_response(response, started=started, client_ms=client_ms)


async def _collect(client: httpx.AsyncClient, config: DiagnosticConfig) -> list[_Sample]:
    samples: list[_Sample] = []
    if config.concurrency == 1:
        for _ in range(config.requests):
            samples.append(await _request_once(client, config))
        return samples

    limiter = anyio.CapacityLimiter(config.concurrency)

    async def worker() -> None:
        async with limiter:
            samples.append(await _request_once(client, config))

    async with anyio.create_task_group() as task_group:
        for _ in range(config.requests):
            _ = task_group.start_soon(worker)
    return samples


async def _measure(client: httpx.AsyncClient, config: DiagnosticConfig) -> DiagnosticReport:
    warmup = await _request_once(client, config)
    if warmup.error is not None:
        return _report(config, [warmup])
    return _report(config, await _collect(client, config))


@contextmanager
def _deterministic_reference_store() -> Generator[None, None, None]:
    from app.api import world_memory_plane as wmp

    original_configured = wmp._is_pg_authority_configured
    store = wmp._get_store()
    store.clear()
    wmp._is_pg_authority_configured = lambda: False
    try:
        yield
    finally:
        store.clear()
        wmp._is_pg_authority_configured = original_configured


def _report(config: DiagnosticConfig, samples: list[_Sample]) -> DiagnosticReport:
    valid = [sample for sample in samples if sample.error is None]
    errors = [sample.error for sample in samples if sample.error is not None]
    handler = [sample.handler_ms for sample in valid if sample.handler_ms is not None]
    overhead = [
        max(0.0, sample.total_ms - sample.handler_ms)
        for sample in valid
        if sample.handler_ms is not None
    ]
    statuses: dict[str, int] = {}
    refusals: dict[str, int] = {}
    for sample in valid:
        if sample.status is not None:
            statuses[sample.status] = statuses.get(sample.status, 0) + 1
        if sample.refusal_reason is not None:
            refusals[sample.refusal_reason] = refusals.get(sample.refusal_reason, 0) + 1
    schema_errors = tuple(sorted({error for sample in samples for error in sample.schema_errors}))
    return DiagnosticReport(
        requests=config.requests,
        samples=len(valid),
        concurrency=config.concurrency,
        total_latency=_summary([sample.total_ms for sample in valid]),
        client_latency=_summary([sample.client_ms for sample in valid]),
        handler_latency=_summary(handler),
        boundary_overhead=_summary(overhead),
        status_counts=statuses,
        refusal_reason_counts=refusals,
        errors=len(errors),
        error_messages=tuple(errors),
        schema_compatible=not schema_errors and bool(valid),
        schema_errors=schema_errors,
    )


def diagnostic_exit_code(report: DiagnosticReport) -> int:
    return int(
        not (report.errors == 0 and report.samples == report.requests and report.schema_compatible)
    )


async def run_diagnostic(config: DiagnosticConfig) -> DiagnosticReport:
    """Run the ASGI boundary diagnostic against the existing application."""
    config.validate()
    from app.main import app

    with _deterministic_reference_store():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://asgi") as client:
            return await _measure(client, config)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--requests", type=int, default=8)
    _ = parser.add_argument("--concurrency", type=int, choices=(1, 16), default=1)
    _ = parser.add_argument("--max-items", type=int, default=10)
    _ = parser.add_argument("--query", default="asgi boundary")
    return parser


def main() -> int:
    parser = _build_parser()
    args = _CliArgs()
    _ = parser.parse_args(namespace=args)
    try:
        report = anyio.run(
            run_diagnostic,
            DiagnosticConfig(
                requests=args.requests,
                concurrency=args.concurrency,
                max_items=args.max_items,
                query=args.query,
            ),
        )
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    print(json.dumps(report.to_json(), ensure_ascii=False, sort_keys=True))
    return diagnostic_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
