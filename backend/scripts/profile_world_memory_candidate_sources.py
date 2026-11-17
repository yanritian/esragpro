"""No-LLM HTTP profiler for world-memory candidate-source diagnostics.

The profiler sends one warm probe followed by a measured request batch to
``POST /world-memory/query``. One persistent ``httpx.AsyncClient`` owns every
request so connection setup is not charged repeatedly. JSON output separates
HTTP latency from the API's returned candidate-source traces.

Run from ``backend/`` against an already-running isolated application::

    python scripts/profile_world_memory_candidate_sources.py \
        --base-url http://127.0.0.1:18086 --requests 100 --concurrency 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

import httpx

_DEFAULT_QUERY = "p10b rehearsal telemetry reading"
_DEFAULT_MAX_ITEMS = 50
_MIN_MAX_ITEMS = 1
_MAX_MAX_ITEMS = 200
_REQUEST_PATH = "/api/v1/world-memory/query"


@dataclass(frozen=True, slots=True)
class ProfileResult:
    """Machine-readable measurements from one warm and measured HTTP run."""

    warm_latency_ms: float | None
    total_requests: int
    samples: int
    errors: Mapping[str, int]
    http_status_counts: Mapping[str, int]
    response_status_counts: Mapping[str, int]
    throughput_rps: float | None
    total_latency_ms: Mapping[str, float | None]
    candidate_sources: Mapping[str, Mapping[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the profile without losing null percentile values."""
        return {
            "warm_latency_ms": self.warm_latency_ms,
            "total_requests": self.total_requests,
            "samples": self.samples,
            "errors": dict(self.errors),
            "http_status_counts": dict(self.http_status_counts),
            "response_status_counts": dict(self.response_status_counts),
            "throughput_rps": self.throughput_rps,
            "total_latency_ms": dict(self.total_latency_ms),
            "candidate_sources": dict(self.candidate_sources),
        }


def percentile(values: list[float], percent: float) -> float | None:
    """Return the nearest-rank percentile for an unsorted sample collection."""
    if not values:
        return None
    ordered = sorted(values)
    if percent <= 0:
        return ordered[0]
    if percent >= 100:
        return ordered[-1]
    rank = math.ceil(percent / 100 * len(ordered))
    return ordered[rank - 1]


def latency_summary(values: list[float]) -> dict[str, float | None]:
    """Summarize total or candidate-source latency measurements in milliseconds."""
    if not values:
        return {"p50": None, "p95": None, "p99": None, "mean": None}
    return {
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "mean": sum(values) / len(values),
    }


def parse_max_items(value: str) -> int:
    """Parse the API-compatible max-items bound for the CLI."""
    try:
        max_items = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--max-items must be an integer") from exc
    if not _MIN_MAX_ITEMS <= max_items <= _MAX_MAX_ITEMS:
        raise argparse.ArgumentTypeError(
            f"--max-items must be between {_MIN_MAX_ITEMS} and {_MAX_MAX_ITEMS}, got {max_items}"
        )
    return max_items


def aggregate_candidate_sources(
    responses: list[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Aggregate documented candidate-source timing and status fields only."""
    grouped: dict[str, dict[str, Any]] = {}
    for response in responses:
        raw_sources = response.get("candidate_sources", [])
        if not isinstance(raw_sources, list):
            continue
        for raw_source in raw_sources:
            if not isinstance(raw_source, dict):
                continue
            source = raw_source.get("source")
            status = raw_source.get("status")
            latency_ms = raw_source.get("latency_ms")
            candidate_count = raw_source.get("candidate_count")
            if not isinstance(source, str) or not isinstance(status, str):
                continue
            bucket = grouped.setdefault(
                source,
                {"samples": 0, "status_counts": {}, "latencies": [], "candidate_counts": []},
            )
            bucket["samples"] += 1
            status_counts = bucket["status_counts"]
            status_counts[status] = status_counts.get(status, 0) + 1
            if isinstance(latency_ms, int | float):
                bucket["latencies"].append(float(latency_ms))
            if isinstance(candidate_count, int):
                bucket["candidate_counts"].append(candidate_count)

    return {
        source: {
            "samples": bucket["samples"],
            "status_counts": bucket["status_counts"],
            "latency_ms": latency_summary(bucket["latencies"]),
            "candidate_count": {
                "sum": sum(bucket["candidate_counts"]),
                "p50": percentile(bucket["candidate_counts"], 50),
                "p95": percentile(bucket["candidate_counts"], 95),
                "p99": percentile(bucket["candidate_counts"], 99),
            },
        }
        for source, bucket in sorted(grouped.items())
    }


async def _request(
    client: httpx.AsyncClient,
    url: str,
    payload: Mapping[str, Any],
) -> tuple[float, int, Mapping[str, Any]]:
    """Measure one request and return its HTTP status plus JSON body."""
    started = perf_counter()
    response = await client.post(url, json=payload)
    latency_ms = (perf_counter() - started) * 1000
    return latency_ms, response.status_code, response.json()


async def run_profile(
    client: httpx.AsyncClient,
    url: str,
    payload: Mapping[str, Any],
    request_count: int,
    concurrency: int,
) -> ProfileResult:
    """Warm once, then profile a bounded batch using the supplied client."""
    if request_count <= 0:
        raise ValueError("--requests must be positive")
    if concurrency <= 0:
        raise ValueError("--concurrency must be positive")

    warm_latency_ms: float | None = None
    try:
        warm_latency_ms, _warm_http_status, _warm_response = await _request(client, url, payload)
    except httpx.HTTPError:
        pass

    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    responses: list[Mapping[str, Any]] = []
    errors: dict[str, int] = {}
    http_status_counts: dict[str, int] = {}
    response_status_counts: dict[str, int] = {}

    async def measure_one() -> None:
        async with semaphore:
            try:
                latency_ms, http_status, response = await _request(client, url, payload)
            except httpx.HTTPError as exc:
                name = type(exc).__name__
                errors[name] = errors.get(name, 0) + 1
                return
            http_status_counts[str(http_status)] = http_status_counts.get(str(http_status), 0) + 1
            if http_status != 200:
                return
            response_status = response.get("status")
            if isinstance(response_status, str):
                response_status_counts[response_status] = (
                    response_status_counts.get(response_status, 0) + 1
                )
            latencies.append(latency_ms)
            responses.append(response)

    started = perf_counter()
    await asyncio.gather(*(measure_one() for _ in range(request_count)))
    elapsed = perf_counter() - started
    return ProfileResult(
        warm_latency_ms=warm_latency_ms,
        total_requests=request_count,
        samples=len(latencies),
        errors=errors,
        http_status_counts=http_status_counts,
        response_status_counts=response_status_counts,
        throughput_rps=request_count / elapsed if elapsed > 0 else None,
        total_latency_ms=latency_summary(latencies),
        candidate_sources=aggregate_candidate_sources(responses),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="No-LLM HTTP candidate-source profiler")
    parser.add_argument("--base-url", required=True, help="Running isolated application URL")
    parser.add_argument("--requests", type=int, default=100, help="Measured requests (default: 100)")
    parser.add_argument("--concurrency", type=int, default=1, help="Max in-flight requests (default: 1)")
    parser.add_argument("--query", default=_DEFAULT_QUERY, help="Query text sent to world-memory")
    parser.add_argument(
        "--max-items",
        type=parse_max_items,
        default=_DEFAULT_MAX_ITEMS,
        help="Maximum items to return (default: 50; range: 1..200)",
    )
    parser.add_argument(
        "--event-time-lte",
        type=datetime.fromisoformat,
        default=None,
        help="Optional inclusive event-time cutoff",
    )
    parser.add_argument(
        "--require-evidence",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Require evidence-backed results (default: true)",
    )
    return parser


def _build_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": args.query,
        "require_evidence": args.require_evidence,
        "max_items": args.max_items,
        "token_budget": 4000,
    }
    if args.event_time_lte is not None:
        payload["event_time_lte"] = args.event_time_lte.astimezone(timezone.utc).isoformat()
    return payload


async def _run_cli(args: argparse.Namespace) -> int:
    payload = _build_payload(args)
    url = f"{args.base_url.rstrip('/')}{_REQUEST_PATH}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        result = await run_profile(client, url, payload, args.requests, args.concurrency)
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    return asyncio.run(_run_cli(_build_parser().parse_args()))


if __name__ == "__main__":
    sys.exit(main())
