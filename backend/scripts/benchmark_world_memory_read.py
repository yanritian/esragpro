"""
P10 deterministic no-LLM benchmark for the world-memory read path.

The read path is the P9-D3 historical event-history query (ES candidate
source + PostgreSQL authority rehydration). No LLM participates.

Two seams are supported:

- in-process (default): calls ``world_memory_plane.world_memory_query``
  directly, the same seam the P9-D2 live integration test uses. Requires
  the PostgreSQL authority and Elasticsearch to be reachable.
- HTTP: ``--base-url http://127.0.0.1:8000`` measures the real application
  endpoint ``POST /api/v1/world-memory/query``.

Concurrency: ``--concurrency N`` bounds the number of in-flight requests
for the measured batch (default 1 = strictly sequential). All requests
share the same persistent HTTP client (or session factory), so connection
setup is not charged to each request. Each ``request()`` measures
transport/request latency after semaphore admission, so the local semaphore
queueing time is not part of the percentile values; throughput (requests/sec
over the wall-clock batch duration) is the capacity signal.

Output (JSON on stdout) reports total requests, configured concurrency,
errors, throughput, cold latency, warm P50/P95/P99, mean, ``target_ms``,
``target_met``, status counts, and an explicit ``blocked`` flag when the
endpoint or corpus is unavailable.

Exit codes: ``0`` = benchmark ran with samples (``target_met`` is a reported
field, not a gate), ``2`` = blocked (endpoint or corpus unavailable),
``1`` = unexpected error.

The historical query path is refused when the corpus is incomplete
(``event_history_*`` reasons). The benchmark detects that on its probe
request and reports a blocked result instead of inventing latency numbers.

Run (from ``backend/`` with the project venv active)::

    WORLD_MEMORY_EVENT_HISTORY_ENABLED=true \\
        python scripts/benchmark_world_memory_read.py --requests 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

import httpx
from elastic_transport import HttpxAsyncHttpNode
from elasticsearch import AsyncElasticsearch

from app.core.config import settings
from app.memory_core import memory_plane_session
from app.memory_core.world_memory_event_history_runtime import (
    close_world_memory_event_history_client,
    get_world_memory_event_history_client,
)

_DEFAULT_MAX_ITEMS = 10
_MIN_MAX_ITEMS = 1
_MAX_MAX_ITEMS = 200


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

# ============================================================================
# Report types
# ============================================================================


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    samples: int
    errors: int
    cold_latency_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    mean_ms: float | None
    target_ms: float
    target_met: bool
    blocked: bool
    blocked_reason: str | None
    status_counts: Mapping[str, int]
    refusal_reason_counts: Mapping[str, int]
    total_requests: int | None = None
    concurrency: int = 1
    throughput_rps: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "errors": self.errors,
            "total_requests": self.total_requests,
            "concurrency": self.concurrency,
            "throughput_rps": self.throughput_rps,
            "cold_latency_ms": self.cold_latency_ms,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "mean_ms": self.mean_ms,
            "target_ms": self.target_ms,
            "target_met": self.target_met,
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "status_counts": dict(self.status_counts),
            "refusal_reason_counts": dict(self.refusal_reason_counts),
        }


# ============================================================================
# Pure functions (testable)
# ============================================================================


def percentile(sorted_values: list[float], p: float) -> float | None:
    """Nearest-rank percentile over an ascending list."""
    if not sorted_values:
        return None
    if p <= 0:
        return sorted_values[0]
    if p >= 100:
        return sorted_values[-1]
    rank = math.ceil(p / 100.0 * len(sorted_values))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def compute_summary(
    samples: list[float],
    target_ms: float,
) -> tuple[float | None, float | None, float | None, float | None, bool]:
    """Return (p50, p95, p99, mean, target_met)."""
    if not samples:
        return None, None, None, None, False
    ordered = sorted(samples)
    p50 = percentile(ordered, 50.0)
    p95 = percentile(ordered, 95.0)
    p99 = percentile(ordered, 99.0)
    mean = sum(samples) / len(samples)
    target_met = p99 is not None and p99 <= target_ms
    return p50, p95, p99, mean, target_met


def is_corpus_blocked(status: str, refusal_reason: str | None) -> bool:
    """True when a read-path response is refused because the corpus gate holds.

    The API refuses historical queries with ``event_history_*`` reasons when
    the feature is disabled, the corpus is lagging/unavailable, or the
    authority is unreachable. A ``no_match``/``insufficient_evidence`` refusal
    is a legitimate read-path outcome, not a blocked corpus.
    """
    return (
        status == "refused"
        and refusal_reason is not None
        and refusal_reason.startswith("event_history_")
    )


# ============================================================================
# Benchmark loop (testable)
# ============================================================================


RequestFn = Callable[[], Awaitable[tuple[float, str, str | None]]]


async def _collect_samples(
    *,
    request: RequestFn,
    count: int,
    concurrency: int,
) -> tuple[list[float], int, dict[str, int], dict[str, int]]:
    """Run ``count`` requests, bounding in-flight concurrency.

    Returns ``(samples, errors, status_counts, refusal_reason_counts)``. With ``concurrency == 1``
    the loop is strictly sequential. With ``concurrency > 1`` requests are
    fanned out under an ``asyncio.Semaphore`` so never more than
    ``concurrency`` are in flight at once. Each ``request()`` measures its
    own transport/request latency after semaphore admission, so the local
    semaphore queueing time is not part of the percentile values; the
    wall-clock batch throughput is the capacity signal.
    Read-modify-write blocks below contain no ``await``, so they are atomic
    under the event loop and need no lock.
    """
    if concurrency == 1:
        samples: list[float] = []
        errors = 0
        status_counts: dict[str, int] = {}
        refusal_reason_counts: dict[str, int] = {}
        for _ in range(count):
            try:
                latency, status, refusal_reason = await request()
            except Exception:  # noqa: BLE001 - counted as an error sample
                errors += 1
                continue
            status_counts[status] = status_counts.get(status, 0) + 1
            if refusal_reason is not None:
                refusal_reason_counts[refusal_reason] = (
                    refusal_reason_counts.get(refusal_reason, 0) + 1
                )
            samples.append(latency)
        return samples, errors, status_counts, refusal_reason_counts

    sem = asyncio.Semaphore(concurrency)
    samples = []
    errors = 0
    status_counts = {}
    refusal_reason_counts: dict[str, int] = {}

    async def worker() -> None:
        nonlocal errors
        async with sem:
            try:
                latency, status, refusal_reason = await request()
            except Exception:  # noqa: BLE001 - counted as an error sample
                errors += 1
                return
            status_counts[status] = status_counts.get(status, 0) + 1
            if refusal_reason is not None:
                refusal_reason_counts[refusal_reason] = (
                    refusal_reason_counts.get(refusal_reason, 0) + 1
                )
            samples.append(latency)

    _ = await asyncio.gather(*(worker() for _ in range(count)))
    return samples, errors, status_counts, refusal_reason_counts


async def run_benchmark(
    *,
    request: RequestFn,
    count: int,
    target_ms: float,
    concurrency: int = 1,
) -> BenchmarkResult:
    """Run the benchmark loop.

    ``request`` is an async callable returning ``(latency_ms, status,
    refusal_reason)`` and raising on transport/HTTP errors. The first call is
    the probe + cold sample; refusal with an ``event_history_*`` reason (or a
    raised error) blocks the benchmark honestly. ``concurrency`` bounds the
    number of in-flight requests for the measured batch (1 = sequential);
    all requests share the same caller-supplied transport.
    """
    if count <= 0:
        raise ValueError("--requests must be positive")
    if concurrency <= 0:
        raise ValueError("--concurrency must be positive")

    try:
        cold_latency, probe_status, probe_refusal = await request()
    except Exception as exc:  # noqa: BLE001 - endpoint is unreachable
        _blocked = BenchmarkResult(
            samples=0,
            errors=0,
            total_requests=0,
            concurrency=concurrency,
            throughput_rps=None,
            cold_latency_ms=None,
            p50_ms=None,
            p95_ms=None,
            p99_ms=None,
            mean_ms=None,
            target_ms=target_ms,
            target_met=False,
            blocked=True,
            blocked_reason=f"endpoint_unavailable: {type(exc).__name__}",
            status_counts={},
            refusal_reason_counts={},
        )
        return _blocked

    if is_corpus_blocked(probe_status, probe_refusal):
        return BenchmarkResult(
            samples=0,
            errors=0,
            total_requests=0,
            concurrency=concurrency,
            throughput_rps=None,
            cold_latency_ms=None,
            p50_ms=None,
            p95_ms=None,
            p99_ms=None,
            mean_ms=None,
            target_ms=target_ms,
            target_met=False,
            blocked=True,
            blocked_reason=probe_refusal or "event_history_unavailable",
            status_counts={},
            refusal_reason_counts={},
        )

    started = perf_counter()
    samples, errors, status_counts, refusal_reason_counts = await _collect_samples(
        request=request,
        count=count,
        concurrency=concurrency,
    )
    wall_seconds = perf_counter() - started

    p50, p95, p99, mean, target_met = compute_summary(samples, target_ms)
    target_met = errors == 0 and target_met
    throughput_rps = count / wall_seconds if wall_seconds > 0 else None
    return BenchmarkResult(
        samples=len(samples),
        errors=errors,
        total_requests=count,
        concurrency=concurrency,
        throughput_rps=throughput_rps,
        cold_latency_ms=cold_latency,
        p50_ms=p50,
        p95_ms=p95,
        p99_ms=p99,
        mean_ms=mean,
        target_ms=target_ms,
        target_met=target_met,
        blocked=False,
        blocked_reason=None,
        status_counts=status_counts,
        refusal_reason_counts=refusal_reason_counts,
    )


# ============================================================================
# Request seams
# ============================================================================


def _make_in_process_request(
    *,
    query: str,
    event_time_lte: datetime,
    require_evidence: bool,
    max_items: int = _DEFAULT_MAX_ITEMS,
) -> RequestFn:
    from app.api import world_memory_plane as wmp

    async def request() -> tuple[float, str, str | None]:
        started = perf_counter()
        response = await wmp.world_memory_query(
            wmp.WorldMemoryQueryRequest(
                query=query,
                event_time_lte=event_time_lte,
                require_evidence=require_evidence,
                max_items=max_items,
                token_budget=4000,
                kind=None,
                aggregate_id=None,
            )
        )
        latency_ms = (perf_counter() - started) * 1000.0
        return latency_ms, response.status, response.refusal_reason

    return request


def _make_http_request(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    api_prefix: str,
    query: str,
    event_time_lte: datetime,
    require_evidence: bool,
    max_items: int = _DEFAULT_MAX_ITEMS,
) -> RequestFn:
    """Build a request callable over the supplied persistent HTTP client.

    ``client`` is owned by the caller and reused for the probe and every
    measured sample so connection setup is not charged to each request. The
    caller is responsible for closing it once after the benchmark.
    """
    url = f"{base_url.rstrip('/')}{api_prefix}/world-memory/query"

    async def request() -> tuple[float, str, str | None]:
        started = perf_counter()
        response = await client.post(
            url,
            json={
                "query": query,
                "event_time_lte": event_time_lte.isoformat(),
                "require_evidence": require_evidence,
                "max_items": max_items,
                "token_budget": 4000,
            },
        )
        latency_ms = (perf_counter() - started) * 1000.0
        if response.status_code != 200:
            raise RuntimeError(
                f"world-memory query returned HTTP {response.status_code}"
            )
        body = response.json()
        return latency_ms, body["status"], body.get("refusal_reason")

    return request


# ============================================================================
# CLI
# ============================================================================


async def _run_and_report(
    request: RequestFn,
    count: int,
    target_ms: float,
    concurrency: int,
) -> int:
    result = await run_benchmark(
        request=request,
        count=count,
        target_ms=target_ms,
        concurrency=concurrency,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    if result.blocked:
        return 2
    return 0


async def _run_benchmark(args: argparse.Namespace) -> int:
    event_time_lte = args.event_time_lte
    if event_time_lte is None:
        event_time_lte = datetime.now(timezone.utc)

    if args.base_url:
        # One persistent client for the probe and all measured samples; it is
        # closed once when the context exits, after the benchmark completes.
        async with httpx.AsyncClient(timeout=30.0) as http:
            request = _make_http_request(
                client=http,
                base_url=args.base_url,
                api_prefix=args.api_prefix,
                query=args.query,
                event_time_lte=event_time_lte,
                require_evidence=args.require_evidence,
                max_items=args.max_items,
            )
            return await _run_and_report(
                request, args.requests, args.target_ms, args.concurrency
            )

    await memory_plane_session.init_session_factory(database_url=args.database_url)
    session_factory = memory_plane_session._state()["session_factory"]
    if session_factory is None:
        raise RuntimeError("world-memory authority session factory is unavailable")
    _ = await get_world_memory_event_history_client(
        elasticsearch_factory=lambda url: AsyncElasticsearch(
            url,
            node_class=HttpxAsyncHttpNode,
        )
    )
    request = _make_in_process_request(
        query=args.query,
        event_time_lte=event_time_lte,
        require_evidence=args.require_evidence,
        max_items=args.max_items,
    )
    return await _run_and_report(
        request, args.requests, args.target_ms, args.concurrency
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic no-LLM latency benchmark for the world-memory "
            "historical read path. Fails honestly (blocked) when the endpoint "
            "or corpus is unavailable."
        )
    )
    parser.add_argument("--requests", type=int, default=100, help="Measured samples (default: 100)")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Max in-flight requests for the measured batch (default: 1 = sequential)",
    )
    parser.add_argument(
        "--target-ms",
        type=float,
        default=100.0,
        help="P99 latency target in ms (default: 100.0)",
    )
    parser.add_argument(
        "--query",
        default="historical",
        help='Candidate query sent to the read path (default: "historical")',
    )
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
        help="Inclusive UTC event-time cutoff (default: now at benchmark start)",
    )
    parser.add_argument(
        "--require-evidence",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Require evidence on retrieved items (default: true)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="HTTP mode: base URL of the running application (e.g. http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--api-prefix",
        default=settings.API_PREFIX,
        help=f"API prefix for HTTP mode (default: {settings.API_PREFIX})",
    )
    parser.add_argument(
        "--database-url",
        default=settings.DATABASE_URL,
        help="PostgreSQL authority URL for in-process mode (default: settings.DATABASE_URL)",
    )
    return parser


async def _run_cli(args: argparse.Namespace) -> int:
    try:
        return await _run_benchmark(args)
    finally:
        if not args.base_url:
            await close_world_memory_event_history_client()
            await memory_plane_session.close_session_factory()


def main() -> int:
    return asyncio.run(_run_cli(_build_parser().parse_args()))


if __name__ == "__main__":
    sys.exit(main())
