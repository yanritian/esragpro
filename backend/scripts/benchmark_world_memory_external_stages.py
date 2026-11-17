# noqa: SIZE_OK - standalone two-surface diagnostic keeps one typed CLI lifecycle.
from __future__ import annotations

import argparse
import json
import math
import platform
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from time import get_clock_info, perf_counter, perf_counter_ns
from typing import Final, Literal, NotRequired, TypedDict, assert_never
from uuid import uuid4

import anyio
import httpcore
import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.api.world_memory_plane import WorldMemoryQueryResponse
from app.memory_core.world_memory_event_history_asgi_diagnostics import (
    ASGI_DIAGNOSTIC_HEADER_NAME,
    ASGIDiagnosticSample,
    ASGIDiagnosticsSnapshot,
)
from app.memory_core.world_memory_event_history_coordinator import (
    COORDINATOR_ENDPOINT_PATH,
    EventHistoryCoordinatorResponse,
    EventHistorySingleflightStats,
)
from app.memory_core.world_memory_event_history_diagnostics import EventHistoryTimingSample

API_PATH: Final[str] = "/api/v1/world-memory/query"
Surface = Literal["api", "coordinator"]
QueryMode = Literal["identical", "unique"]
Payload = dict[str, JsonValue]
StageValues = tuple[float, ...]
PATHS: dict[Surface, str] = {"api": API_PATH, "coordinator": COORDINATOR_ENDPOINT_PATH}
STAGE_NAMES: dict[Surface, tuple[str, ...]] = {
    "api": (
        "external_wall_ms",
        "api_internal_ms",
        "candidate_ms",
        "coordinator_non_candidate_ms",
        "external_boundary_ms",
    ),
    "coordinator": ("external_wall_ms", "candidate_ms", "coordinator_non_candidate_ms"),
}
INTERNAL_STAGE_NAMES: Final[tuple[str, ...]] = (
    "caller_wait_ms",
    "leader_execution_ms",
    "retrieval_ms",
    "readiness_ms",
    "candidate_ms",
    "authority_ms",
    "query_unattributed_ms",
    "preparation_ms",
    "leader_unattributed_ms",
)
ASGI_STAGE_NAMES: Final[tuple[str, ...]] = (
    "admission_ms",
    "handler_ms",
    "response_send_ms",
    "server_total_ms",
    "outside_asgi_ms",
)
LOCAL_ASGI_STAGE_NAMES: Final[tuple[str, ...]] = ASGI_STAGE_NAMES[:-1]
SHARED_ASGI_STAGE_NAMES: Final[tuple[str, ...]] = (
    "admission_ms",
    "handler_ms",
    "response_send_ms",
    "server_total_ms",
    "pre_asgi_ms",
    "post_send_ms",
    "external_wall_ms",
)
ASGI_CLOCK_METADATA_FIELDS: Final[tuple[str, ...]] = (
    "snapshot_captured_ns",
    "operating_system",
    "boot_id",
    "perf_counter_implementation",
    "perf_counter_resolution",
    "perf_counter_monotonic",
    "perf_counter_adjustable",
    "fetch_start_ns",
    "fetch_end_ns",
)
CLIENT_STAGE_NAMES: Final[tuple[str, ...]] = (
    "limiter_wait_ms",
    "pool_dispatch_ms",
    "connect_ms",
    "tls_ms",
    "request_headers_ms",
    "request_body_ms",
    "response_headers_ms",
    "response_body_ms",
    "response_closed_ms",
    "external_wall_ms",
)
HTTPX_DIAGNOSTIC_VERSION: Final[str] = "0.28.1"
HTTPCORE_DIAGNOSTIC_VERSION: Final[str] = "1.0.9"
DIAGNOSTIC_LIMITS: Final[httpx.Limits] = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
)


class StatsValues(TypedDict):
    leader_count_delta: int
    join_count_delta: int
    in_flight_after: int
    sample_count: int
    role_counts: dict[str, int]
    internal_stages: dict[str, dict[str, float]]


class ClientStageValues(TypedDict):
    limiter_wait_ns: int
    external_wall_ns: int
    pool_dispatch_ns: NotRequired[int]
    connect_ns: NotRequired[int]
    tls_ns: NotRequired[int]
    request_headers_ns: NotRequired[int]
    request_body_ns: NotRequired[int]
    response_headers_ns: NotRequired[int]
    response_body_ns: NotRequired[int]
    response_closed_ns: NotRequired[int]


class DiagnosticRetention(TypedDict):
    before: dict[str, JsonValue]
    after: dict[str, JsonValue]
    measured_ids: list[str]


class DiagnosticSnapshotShapeError(TypeError):
    pass


@dataclass(frozen=True, slots=True)
class ClientClockMetadata:
    operating_system: str
    boot_id: str | None
    perf_counter_implementation: str
    perf_counter_resolution: float
    perf_counter_monotonic: bool
    perf_counter_adjustable: bool


@dataclass(frozen=True, slots=True)
class HttpTraceEvent:
    name: str
    timestamp_ns: int


@dataclass(frozen=True, slots=True)
class HttpTraceTiming:
    first_event_ns: int
    connect_ns: int | None
    tls_ns: int | None
    request_headers_ns: int
    request_body_ns: int
    response_headers_ns: int
    response_body_ns: int
    response_closed_ns: int


@dataclass(frozen=True, slots=True)
class ClientTiming:
    task_ready_ns: int
    limiter_acquired_ns: int
    client_request_start_ns: int
    request_built_ns: int
    client_complete_ns: int
    trace_events: tuple[HttpTraceEvent, ...]
    clock_metadata: ClientClockMetadata | None = None


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    surface: Surface = "api"
    base_url: str = "http://127.0.0.1:8000"
    requests: int = 8
    concurrency: int = 1
    max_items: int = 10
    query: str = "historical"
    query_mode: QueryMode = "identical"
    event_time_lte: datetime | None = None
    stats_url: str | None = None
    diagnostics_url: str | None = None


def derive_api_stages(wall_ms: float, internal_ms: float, candidate_ms: float) -> StageValues:
    return (
        wall_ms,
        internal_ms,
        candidate_ms,
        max(0.0, internal_ms - candidate_ms),
        max(0.0, wall_ms - internal_ms),
    )


def derive_coordinator_stages(wall_ms: float, candidate_ms: float) -> StageValues:
    return (wall_ms, candidate_ms, max(0.0, wall_ms - candidate_ms))


def build_request_payload(config: BenchmarkConfig, request_index: int) -> Payload:
    match config.query_mode:
        case "identical":
            query = config.query
        case "unique":
            query = f"{config.query} #{request_index}"
        case unreachable:
            assert_never(unreachable)
    payload: Payload = {
        "query": query,
        "event_time_lte": (config.event_time_lte or datetime.now(timezone.utc)).isoformat(),
        "require_evidence": True,
        "max_items": config.max_items,
    }
    match config.surface:
        case "api":
            payload["token_budget"] = 4000
        case "coordinator":
            pass
        case unreachable:
            assert_never(unreachable)
    return payload


@dataclass(frozen=True, slots=True)
class Sample:
    status: str | None = None
    refusal_reason: str | None = None
    stages: StageValues | None = None
    error: str | None = None
    schema_error: bool = False
    diagnostic_id: str | None = None
    client_timing: ClientTiming | None = None


class BenchmarkReport(TypedDict):
    surface: Surface
    query_mode: QueryMode
    requests: int
    concurrency: int
    samples: int
    errors: int
    status_counts: dict[str, int]
    refusal_reason_counts: dict[str, int]
    stages: dict[str, dict[str, float]]
    schema_compatible: bool
    stats: StatsValues | None
    stats_configured: bool
    server_stages: dict[str, dict[str, float] | bool] | None
    client_stages: dict[str, dict[str, float]] | None
    diagnostics_configured: bool
    diagnostic_retention: DiagnosticRetention | None


def _boot_id() -> str | None:
    if platform.system() != "Linux":
        return None
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value or None


def _client_clock_metadata() -> ClientClockMetadata:
    clock = get_clock_info("perf_counter")
    return ClientClockMetadata(
        operating_system=platform.system(),
        boot_id=_boot_id(),
        perf_counter_implementation=clock.implementation,
        perf_counter_resolution=clock.resolution,
        perf_counter_monotonic=clock.monotonic,
        perf_counter_adjustable=clock.adjustable,
    )


def _new_diagnostic_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=False,
        trust_env=False,
        follow_redirects=False,
        limits=DIAGNOSTIC_LIMITS,
        timeout=30.0,
    )


def _diagnostic_capability_error(client: httpx.AsyncClient) -> str | None:
    if httpx.__version__ != HTTPX_DIAGNOSTIC_VERSION:
        return "diagnostics:unsupported_httpx_version"
    if httpcore.__version__ != HTTPCORE_DIAGNOSTIC_VERSION:
        return "diagnostics:unsupported_httpcore_version"
    transport = client._transport
    if not isinstance(transport, httpx.AsyncHTTPTransport):
        return "diagnostics:unsupported_transport"
    if client._trust_env is not False:
        return "diagnostics:trust_env_enabled"
    if client.follow_redirects:
        return "diagnostics:redirects_unsupported"
    if client._mounts:
        return "diagnostics:custom_mounts_unsupported"
    pool = transport._pool
    if not isinstance(pool, httpcore.AsyncConnectionPool):
        return "diagnostics:unsupported_pool"
    if pool._http1 is not True or pool._http2 is not False:
        return "diagnostics:unsupported_http_protocol"
    if pool._proxy is not None:
        return "diagnostics:proxy_unsupported"
    if (
        pool._max_connections != DIAGNOSTIC_LIMITS.max_connections
        or pool._max_keepalive_connections != DIAGNOSTIC_LIMITS.max_keepalive_connections
    ):
        return "diagnostics:limits_not_explicit"
    return None


def _validate_http_trace(
    events: Sequence[HttpTraceEvent],
    *,
    http_version: str = "HTTP/1.1",
) -> tuple[HttpTraceTiming | None, str | None]:
    if http_version != "HTTP/1.1":
        return None, "trace:unsupported_http_version"
    names = tuple(event.name for event in events)
    common = (
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
    accepted = {
        common,
        ("connection.connect_tcp.started", "connection.connect_tcp.complete", *common),
        (
            "connection.connect_tcp.started",
            "connection.connect_tcp.complete",
            "connection.start_tls.started",
            "connection.start_tls.complete",
            *common,
        ),
    }
    if names not in accepted:
        return None, "trace:sequence_invalid"
    if any(type(event.timestamp_ns) is not int for event in events):
        return None, "trace:timestamp_not_integer"
    if any(event.timestamp_ns < 0 for event in events):
        return None, "trace:timestamp_negative"
    if any(left.timestamp_ns > right.timestamp_ns for left, right in pairwise(events)):
        return None, "trace:timestamp_out_of_order"
    timestamps = {event.name: event.timestamp_ns for event in events}

    def duration(started: str, complete: str) -> int:
        return timestamps[complete] - timestamps[started]

    connect_ns = (
        None
        if "connection.connect_tcp.started" not in timestamps
        else duration("connection.connect_tcp.started", "connection.connect_tcp.complete")
    )
    tls_ns = (
        None
        if "connection.start_tls.started" not in timestamps
        else duration("connection.start_tls.started", "connection.start_tls.complete")
    )
    return (
        HttpTraceTiming(
            first_event_ns=events[0].timestamp_ns,
            connect_ns=connect_ns,
            tls_ns=tls_ns,
            request_headers_ns=duration(
                "http11.send_request_headers.started", "http11.send_request_headers.complete"
            ),
            request_body_ns=duration(
                "http11.send_request_body.started", "http11.send_request_body.complete"
            ),
            response_headers_ns=duration(
                "http11.receive_response_headers.started",
                "http11.receive_response_headers.complete",
            ),
            response_body_ns=duration(
                "http11.receive_response_body.started", "http11.receive_response_body.complete"
            ),
            response_closed_ns=duration(
                "http11.response_closed.started", "http11.response_closed.complete"
            ),
        ),
        None,
    )


def _client_stage_values(timing: ClientTiming) -> ClientStageValues:
    values = ClientStageValues(
        limiter_wait_ns=timing.limiter_acquired_ns - timing.task_ready_ns,
        external_wall_ns=timing.client_complete_ns - timing.client_request_start_ns,
    )
    parsed, _error = _validate_http_trace(timing.trace_events)
    if parsed is None:
        return values
    values["pool_dispatch_ns"] = parsed.first_event_ns - timing.request_built_ns
    if parsed.connect_ns is not None:
        values["connect_ns"] = parsed.connect_ns
    if parsed.tls_ns is not None:
        values["tls_ns"] = parsed.tls_ns
    values["request_headers_ns"] = parsed.request_headers_ns
    values["request_body_ns"] = parsed.request_body_ns
    values["response_headers_ns"] = parsed.response_headers_ns
    values["response_body_ns"] = parsed.response_body_ns
    values["response_closed_ns"] = parsed.response_closed_ns
    return values


def _schema_bad(raw: dict[str, JsonValue], expected: set[str]) -> bool:
    return set(raw) != expected


def _raw(response: httpx.Response) -> tuple[dict[str, JsonValue] | None, str | None, bool]:
    if response.status_code != 200:
        return None, f"http_{response.status_code}", False
    try:
        return TypeAdapter(dict[str, JsonValue]).validate_json(response.content), None, False
    except (ValidationError, ValueError) as exc:
        return None, f"schema:{type(exc).__name__}", True


def _decode(
    response: httpx.Response,
    surface: Surface,
    wall_ms: float,
    diagnostic_id: str | None = None,
) -> Sample:
    raw, error, schema_error = _raw(response)
    if error is not None or raw is None:
        return Sample(
            error=error or "empty_response",
            schema_error=schema_error,
            diagnostic_id=diagnostic_id,
        )
    schema_bad = False
    try:
        match surface:
            case "api":
                schema_bad = _schema_bad(raw, set(WorldMemoryQueryResponse.model_fields))
                parsed = WorldMemoryQueryResponse.model_validate(raw)
                source = next(
                    (
                        item
                        for item in parsed.candidate_sources
                        if item.source == "elasticsearch_event_history"
                    ),
                    None,
                )
                if source is None:
                    return Sample(
                        error="missing:elasticsearch_event_history",
                        diagnostic_id=diagnostic_id,
                    )
                values = (parsed.latency_ms, source.latency_ms)
                if not all(math.isfinite(value) and value >= 0 for value in values):
                    return Sample(error="malformed:latency_ms", diagnostic_id=diagnostic_id)
                sample = Sample(
                    parsed.status,
                    parsed.refusal_reason,
                    derive_api_stages(wall_ms, *values),
                )
            case "coordinator":
                schema_bad = _schema_bad(raw, set(EventHistoryCoordinatorResponse.model_fields))
                parsed = EventHistoryCoordinatorResponse.model_validate(raw)
                if (
                    not math.isfinite(parsed.candidate_latency_ms)
                    or parsed.candidate_latency_ms < 0
                ):
                    return Sample(
                        error="malformed:candidate_latency_ms",
                        diagnostic_id=diagnostic_id,
                    )
                sample = Sample(
                    "refused" if parsed.refusal_reason is not None else "ok",
                    parsed.refusal_reason,
                    derive_coordinator_stages(wall_ms, parsed.candidate_latency_ms),
                )
            case unreachable:
                assert_never(unreachable)
    except (ValidationError, ValueError) as exc:
        return Sample(
            error=f"schema:{type(exc).__name__}",
            schema_error=True,
            diagnostic_id=diagnostic_id,
        )
    if schema_bad:
        return Sample(
            error="response_fields_incompatible",
            schema_error=True,
            diagnostic_id=diagnostic_id,
        )
    return replace(sample, diagnostic_id=diagnostic_id)


async def _request_once(
    client: httpx.AsyncClient,
    config: BenchmarkConfig,
    index: int,
    diagnostic_id: str | None = None,
    *,
    task_ready_ns: int | None = None,
    limiter_acquired_ns: int | None = None,
) -> Sample:
    if diagnostic_id is not None:
        ready_ns = perf_counter_ns() if task_ready_ns is None else task_ready_ns
        acquired_ns = ready_ns if limiter_acquired_ns is None else limiter_acquired_ns
        headers = {ASGI_DIAGNOSTIC_HEADER_NAME: diagnostic_id}
        payload = build_request_payload(config, index)
        url = f"{config.base_url.rstrip('/')}{PATHS[config.surface]}"
        trace_events: list[HttpTraceEvent] = []

        async def trace(name: str, _info: dict[str, JsonValue]) -> None:
            trace_events.append(HttpTraceEvent(name=name, timestamp_ns=perf_counter_ns()))

        request_start_ns = perf_counter_ns()
        request = client.build_request(
            "POST",
            url,
            json=payload,
            headers=headers,
            extensions={"trace": trace},
        )
        request_built_ns = perf_counter_ns()
        response: httpx.Response | None = None
        sample = Sample(error="request_failed", diagnostic_id=diagnostic_id)
        try:
            response = await client.send(request, stream=False)
            sample = _decode(response, config.surface, 0.0, diagnostic_id)
            await response.aclose()
        except (httpx.HTTPError, TimeoutError) as exc:
            sample = Sample(error=type(exc).__name__, diagnostic_id=diagnostic_id)
        finally:
            client_complete_ns = perf_counter_ns()
        timing = ClientTiming(
            task_ready_ns=ready_ns,
            limiter_acquired_ns=acquired_ns,
            client_request_start_ns=request_start_ns,
            request_built_ns=request_built_ns,
            client_complete_ns=client_complete_ns,
            trace_events=tuple(trace_events),
            clock_metadata=_client_clock_metadata(),
        )
        wall_ms = (client_complete_ns - request_start_ns) / 1_000_000.0
        sample = _replace_external_wall(sample, config.surface, wall_ms)
        trace_version = "HTTP/1.1" if response is None else response.http_version
        capability_error = _diagnostic_capability_error(client)
        if capability_error is None:
            _parsed, trace_error = _validate_http_trace(
                tuple(trace_events), http_version=trace_version
            )
            if trace_error is not None:
                sample = replace(sample, error=f"diagnostics:{trace_error}")
        elif not isinstance(client._transport, httpx.MockTransport):
            sample = replace(sample, error=capability_error)
        if isinstance(client._transport, httpx.MockTransport):
            return replace(sample, diagnostic_id=diagnostic_id)
        return replace(sample, client_timing=timing, diagnostic_id=diagnostic_id)

    started = perf_counter()
    headers = {} if diagnostic_id is None else {ASGI_DIAGNOSTIC_HEADER_NAME: diagnostic_id}
    try:
        response = await client.post(
            f"{config.base_url.rstrip('/')}{PATHS[config.surface]}",
            json=build_request_payload(config, index),
            headers=headers,
        )
    except (httpx.HTTPError, TimeoutError) as exc:
        return Sample(error=type(exc).__name__, diagnostic_id=diagnostic_id)
    return _decode(
        response,
        config.surface,
        max(0.0, (perf_counter() - started) * 1000.0),
        diagnostic_id,
    )


def _replace_external_wall(sample: Sample, surface: Surface, wall_ms: float) -> Sample:
    if sample.stages is None or sample.error is not None:
        return sample
    match surface:
        case "api":
            stages = derive_api_stages(wall_ms, sample.stages[1], sample.stages[2])
        case "coordinator":
            stages = derive_coordinator_stages(wall_ms, sample.stages[1])
        case unreachable:
            assert_never(unreachable)
    return replace(sample, stages=stages)


async def _collect(
    client: httpx.AsyncClient,
    config: BenchmarkConfig,
    diagnostic_ids: Sequence[str] | None = None,
) -> tuple[Sample, ...]:
    samples: list[Sample] = []
    limiter = anyio.CapacityLimiter(config.concurrency)

    async def worker(index: int) -> None:
        task_ready_ns = perf_counter_ns()
        async with limiter:
            limiter_acquired_ns = perf_counter_ns()
            diagnostic_id = None if diagnostic_ids is None else diagnostic_ids[index - 1]
            samples.append(
                await _request_once(
                    client,
                    config,
                    index,
                    diagnostic_id,
                    task_ready_ns=task_ready_ns,
                    limiter_acquired_ns=limiter_acquired_ns,
                )
            )

    async with anyio.create_task_group() as task_group:
        for index in range(1, config.requests + 1):
            task_group.start_soon(worker, index)
    return tuple(samples)


async def _fetch_stats(
    client: httpx.AsyncClient, url: str
) -> tuple[EventHistorySingleflightStats | None, str | None, bool]:
    try:
        response = await client.get(url)
    except (httpx.HTTPError, TimeoutError) as exc:
        return None, type(exc).__name__, False
    raw, error, schema_bad = _raw(response)
    if error is not None or raw is None:
        return None, error or "empty_response", schema_bad
    try:
        parsed = EventHistorySingleflightStats.model_validate(raw)
    except (ValidationError, ValueError) as exc:
        return None, f"schema:{type(exc).__name__}", True
    if _schema_bad(raw, set(EventHistorySingleflightStats.model_fields)):
        return None, "stats_fields_incompatible", True
    return parsed, None, False


async def _fetch_asgi_diagnostics(
    client: httpx.AsyncClient, url: str
) -> tuple[ASGIDiagnosticsSnapshot | None, str | None, bool]:
    fetch_start_ns = perf_counter_ns()
    try:
        response = await client.get(url)
    except (httpx.HTTPError, TimeoutError) as exc:
        return None, type(exc).__name__, False
    raw, error, schema_bad = _raw(response)
    if error is not None or raw is None:
        return None, error or "empty_response", schema_bad
    try:
        parsed = ASGIDiagnosticsSnapshot.model_validate(raw)
    except (ValidationError, ValueError) as exc:
        return None, f"schema:{type(exc).__name__}", True
    fetch_end_ns = perf_counter_ns()
    expected = set(ASGIDiagnosticsSnapshot.model_fields)
    if set(raw) not in ({"samples"}, expected):
        return None, "diagnostics_fields_incompatible", True
    return (
        parsed.model_copy(update={"fetch_start_ns": fetch_start_ns, "fetch_end_ns": fetch_end_ns}),
        None,
        False,
    )


def _serialize_diagnostic_snapshot(snapshot: ASGIDiagnosticsSnapshot) -> dict[str, JsonValue]:
    serialized = TypeAdapter(dict[str, JsonValue]).validate_python(snapshot.model_dump(mode="json"))
    samples = serialized.get("samples")
    if not isinstance(samples, list):
        raise DiagnosticSnapshotShapeError("diagnostics:samples_invalid")
    serialized["count"] = len(samples)
    return serialized


def _summary(values: Sequence[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {f"p{p}": ordered[max(0, math.ceil(p * len(ordered) / 100) - 1)] for p in (50, 95, 99)}


def _shared_summary(values: Sequence[float]) -> dict[str, float]:
    return _summary(values)


def _timing_stage_values(sample: EventHistoryTimingSample) -> StageValues:
    return (
        sample.caller_wait_ms,
        sample.leader_execution_ms,
        sample.retrieval_ms,
        sample.readiness_ms,
        sample.candidate_ms,
        sample.authority_ms,
        sample.query_unattributed_ms,
        sample.preparation_ms,
        sample.leader_unattributed_ms,
    )


def _internal_stage_summary(
    samples: Sequence[EventHistoryTimingSample],
) -> dict[str, dict[str, float]]:
    values = tuple(_timing_stage_values(sample) for sample in samples)
    return {
        name: _summary(tuple(row[index] for row in values))
        for index, name in enumerate(INTERNAL_STAGE_NAMES)
    }


def _server_stage_values_ns(sample: ASGIDiagnosticSample) -> tuple[int, int, int, int] | None:
    timestamps = (
        sample.asgi_entry_ns,
        sample.handler_entry_ns,
        sample.handler_exit_ns,
        sample.response_send_complete_ns,
    )
    asgi_entry_ns, handler_entry_ns, handler_exit_ns, response_send_complete_ns = timestamps
    if (
        not sample.complete
        or handler_entry_ns is None
        or handler_exit_ns is None
        or response_send_complete_ns is None
    ):
        return None
    if not (asgi_entry_ns <= handler_entry_ns <= handler_exit_ns <= response_send_complete_ns):
        return None
    admission_ns = handler_entry_ns - asgi_entry_ns
    handler_ns = handler_exit_ns - handler_entry_ns
    response_send_ns = response_send_complete_ns - handler_exit_ns
    server_total_ns = response_send_complete_ns - asgi_entry_ns
    if admission_ns + handler_ns + response_send_ns != server_total_ns:
        return None
    return admission_ns, handler_ns, response_send_ns, server_total_ns


def _asgi_stage_values(
    sample: ASGIDiagnosticSample,
    client_wall_ms: float,
) -> tuple[float, ...] | None:
    if not math.isfinite(client_wall_ms) or client_wall_ms < 0.0:
        return None
    server_values_ns = _server_stage_values_ns(sample)
    if server_values_ns is None:
        return None
    server_values = tuple(value / 1_000_000.0 for value in server_values_ns)
    outside_asgi_ms = client_wall_ms - server_values[-1]
    if not math.isfinite(outside_asgi_ms) or outside_asgi_ms < 0.0:
        return None
    return (*server_values, outside_asgi_ms)


def _local_asgi_stage_values(
    server_values_ns: tuple[int, int, int, int],
    client_wall_ns: int,
) -> tuple[float, ...] | None:
    if client_wall_ns < 0:
        return None
    server_values = tuple(value / 1_000_000.0 for value in server_values_ns)
    outside_asgi_ms = (client_wall_ns - server_values_ns[-1]) / 1_000_000.0
    if not math.isfinite(outside_asgi_ms) or outside_asgi_ms < 0.0:
        return None
    return (*server_values, outside_asgi_ms)


def _local_server_stage_values(server_values_ns: tuple[int, int, int, int]) -> tuple[float, ...]:
    return tuple(value / 1_000_000.0 for value in server_values_ns)


def _snapshot_metadata_present(snapshot: ASGIDiagnosticsSnapshot) -> bool:
    return any(
        getattr(snapshot, field) is not None
        for field in ASGI_CLOCK_METADATA_FIELDS
        if field not in ("fetch_start_ns", "fetch_end_ns")
    )


def _snapshot_metadata_complete(snapshot: ASGIDiagnosticsSnapshot) -> bool:
    snapshot_captured_ns = snapshot.snapshot_captured_ns
    operating_system = snapshot.operating_system
    implementation = snapshot.perf_counter_implementation
    resolution = snapshot.perf_counter_resolution
    monotonic = snapshot.perf_counter_monotonic
    adjustable = snapshot.perf_counter_adjustable
    fetch_start_ns = snapshot.fetch_start_ns
    fetch_end_ns = snapshot.fetch_end_ns
    if any(
        value is None
        for value in (
            snapshot_captured_ns,
            operating_system,
            implementation,
            resolution,
            monotonic,
            adjustable,
            fetch_start_ns,
            fetch_end_ns,
        )
    ):
        return False
    return operating_system != "Linux" or bool(snapshot.boot_id)


def _client_metadata_state(
    timings: Sequence[ClientTiming],
) -> tuple[ClientClockMetadata | None, bool]:
    metadata = tuple(timing.clock_metadata for timing in timings)
    if not any(value is not None for value in metadata):
        return None, True
    if any(value is None for value in metadata):
        return None, False
    first = metadata[0]
    if first is None:
        return None, False
    return first, all(value == first for value in metadata)


def _clock_compatibility_error(
    snapshot: ASGIDiagnosticsSnapshot,
    client_metadata: ClientClockMetadata | None,
) -> str | None:
    if snapshot.operating_system != "Linux":
        if snapshot.boot_id is not None:
            return "diagnostics:clock_metadata_mismatch"
        return None
    if client_metadata is None:
        return None
    if client_metadata.operating_system != snapshot.operating_system:
        return "diagnostics:clock_metadata_mismatch"
    if not _snapshot_metadata_complete(snapshot):
        return "diagnostics:clock_metadata_incomplete"
    if not snapshot.boot_id:
        return "diagnostics:clock_boot_id_missing"
    snapshot_resolution = snapshot.perf_counter_resolution
    snapshot_monotonic = snapshot.perf_counter_monotonic
    snapshot_adjustable = snapshot.perf_counter_adjustable
    snapshot_fetch_start_ns = snapshot.fetch_start_ns
    snapshot_fetch_end_ns = snapshot.fetch_end_ns
    snapshot_captured_ns = snapshot.snapshot_captured_ns
    if (
        snapshot_resolution is None
        or snapshot_monotonic is None
        or snapshot_adjustable is None
        or snapshot_fetch_start_ns is None
        or snapshot_fetch_end_ns is None
        or snapshot_captured_ns is None
    ):
        return "diagnostics:clock_metadata_incomplete"
    if (
        not math.isfinite(snapshot_resolution)
        or snapshot_resolution <= 0.0
        or snapshot_monotonic is not True
        or snapshot_adjustable is not False
    ):
        return "diagnostics:clock_metadata_incompatible"
    if (
        client_metadata.boot_id != snapshot.boot_id
        or client_metadata.perf_counter_implementation != snapshot.perf_counter_implementation
        or client_metadata.perf_counter_resolution != snapshot_resolution
        or client_metadata.perf_counter_monotonic is not snapshot_monotonic
        or client_metadata.perf_counter_adjustable is not snapshot_adjustable
    ):
        return "diagnostics:clock_metadata_mismatch"
    if snapshot_fetch_start_ns > snapshot_fetch_end_ns:
        return "diagnostics:fetch_bracket_invalid"
    if not snapshot_fetch_start_ns <= snapshot_captured_ns <= snapshot_fetch_end_ns:
        return "diagnostics:fetch_bracket_mismatch"
    return None


def _timing_error(timing: ClientTiming, *, strict: bool) -> str | None:
    timestamps = (
        timing.task_ready_ns,
        timing.limiter_acquired_ns,
        timing.client_request_start_ns,
        timing.request_built_ns,
        timing.client_complete_ns,
    )
    if any(type(value) is not int for value in timestamps):
        return "diagnostics:client_timestamp_not_integer"
    if any(value < 0 for value in timestamps):
        return "diagnostics:client_timestamp_negative"
    if not (timing.task_ready_ns <= timing.limiter_acquired_ns <= timing.client_request_start_ns):
        return "diagnostics:limiter_timestamp_invalid"
    if not (timing.client_request_start_ns <= timing.request_built_ns <= timing.client_complete_ns):
        return "diagnostics:client_timestamp_order_invalid"
    if timing.trace_events:
        _parsed, error = _validate_http_trace(timing.trace_events)
        if error is not None:
            return f"diagnostics:client_trace_invalid:{error}"
        if (
            timing.trace_events[0].timestamp_ns < timing.request_built_ns
            or timing.trace_events[-1].timestamp_ns > timing.client_complete_ns
        ):
            return "diagnostics:client_trace_out_of_bounds"
    elif strict:
        return "diagnostics:client_trace_invalid:trace:sequence_invalid"
    return None


def _local_stage_summary(
    values: Sequence[tuple[float, ...]],
    *,
    cross_clock: bool | None,
    include_outside: bool = True,
) -> dict[str, dict[str, float] | bool]:
    stage_names = ASGI_STAGE_NAMES if include_outside else LOCAL_ASGI_STAGE_NAMES
    stages: dict[str, dict[str, float] | bool] = {
        name: _summary(tuple(row[index] for row in values))
        for index, name in enumerate(stage_names)
    }
    if cross_clock is not None:
        stages["cross_clock"] = cross_clock
    return stages


def _shared_stage_summary(
    values: Sequence[tuple[float, ...]],
) -> dict[str, dict[str, float] | bool]:
    stages: dict[str, dict[str, float] | bool] = {
        name: _shared_summary(tuple(row[index] for row in values))
        for index, name in enumerate(SHARED_ASGI_STAGE_NAMES)
    }
    stages["cross_clock"] = True
    return stages


def _local_stage_values(
    joined: Sequence[tuple[Sample, ASGIDiagnosticSample]],
    server_values_ns: Sequence[tuple[int, int, int, int]],
    *,
    use_client_timing: bool,
    include_outside: bool = True,
) -> tuple[list[tuple[float, ...]], str | None]:
    values: list[tuple[float, ...]] = []
    for (measured_sample, server_sample), server_values in zip(
        joined, server_values_ns, strict=True
    ):
        if (
            measured_sample.error is not None
            or measured_sample.stages is None
            or len(measured_sample.stages) == 0
        ):
            return values, "diagnostics:measured_sample_incomplete"
        timing = measured_sample.client_timing
        if not include_outside:
            row = _local_server_stage_values(server_values)
        elif use_client_timing:
            if timing is None:
                return values, "diagnostics:measured_sample_incomplete"
            timing_error = _timing_error(timing, strict=False)
            if timing_error is not None:
                return values, timing_error
            row = _local_asgi_stage_values(
                server_values,
                timing.client_complete_ns - timing.client_request_start_ns,
            )
        elif timing is None:
            row = _asgi_stage_values(server_sample, measured_sample.stages[0])
        else:
            row = _local_asgi_stage_values(
                server_values,
                timing.client_complete_ns - timing.client_request_start_ns,
            )
        if row is None:
            error = (
                "diagnostics:outside_asgi_negative"
                if use_client_timing or timing is not None
                else "diagnostics:server_sample_invalid"
            )
            return values, error
        values.append(row)
    return values, None


def _validate_asgi_diagnostics(
    snapshot: ASGIDiagnosticsSnapshot,
    measured: Sequence[Sample],
    expected_ids: Sequence[str],
) -> tuple[dict[str, dict[str, float] | bool] | None, str | None]:
    if len(set(expected_ids)) != len(expected_ids):
        return None, "diagnostics:request_ids_not_unique"
    measured_by_id = {sample.diagnostic_id: sample for sample in measured}
    if len(measured_by_id) != len(measured):
        return None, "diagnostics:measured_id_duplicate"
    if set(measured_by_id) != set(expected_ids):
        return None, "diagnostics:measured_id_mismatch"
    retained = tuple(sample for sample in snapshot.samples if sample.diagnostic_id in expected_ids)
    counts = Counter(sample.diagnostic_id for sample in retained)
    if any(counts.get(diagnostic_id, 0) != 1 for diagnostic_id in expected_ids):
        return None, "diagnostics:sample_join_incomplete"
    joined = tuple(
        (
            measured_by_id[diagnostic_id],
            next(sample for sample in retained if sample.diagnostic_id == diagnostic_id),
        )
        for diagnostic_id in expected_ids
    )
    server_values_ns: list[tuple[int, int, int, int]] = []
    for _measured_sample, server_sample in joined:
        server_values = _server_stage_values_ns(server_sample)
        if server_values is None:
            return None, "diagnostics:server_sample_invalid"
        server_values_ns.append(server_values)

    metadata_present = _snapshot_metadata_present(snapshot)
    if not metadata_present:
        if any(measured_sample.client_timing is not None for measured_sample, _ in joined):
            values, local_error = _local_stage_values(
                joined, server_values_ns, use_client_timing=False, include_outside=False
            )
            return (
                _local_stage_summary(values, cross_clock=False, include_outside=False)
                if values
                else None,
                local_error,
            )
        values, local_error = _local_stage_values(joined, server_values_ns, use_client_timing=False)
        if local_error is not None:
            return _local_stage_summary(values, cross_clock=None) if values else None, local_error
        return _local_stage_summary(values, cross_clock=None), None

    timings = tuple(measured_sample.client_timing for measured_sample, _ in joined)
    if any(value is None for value in timings):
        values, local_error = _local_stage_values(
            joined, server_values_ns, use_client_timing=False, include_outside=False
        )
        return (
            _local_stage_summary(values, cross_clock=False, include_outside=False)
            if values
            else None,
            local_error or "diagnostics:measured_sample_incomplete",
        )
    typed_timings = tuple(value for value in timings if value is not None)
    client_metadata, metadata_consistent = _client_metadata_state(typed_timings)
    if not metadata_consistent and all(value is not None for value in timings):
        values, local_error = _local_stage_values(
            joined, server_values_ns, use_client_timing=False, include_outside=False
        )
        return (
            _local_stage_summary(values, cross_clock=False, include_outside=False)
            if values
            else None,
            local_error or "diagnostics:clock_metadata_mismatch",
        )
    if client_metadata is None or snapshot.operating_system != "Linux":
        values, local_error = _local_stage_values(
            joined, server_values_ns, use_client_timing=True, include_outside=False
        )
        if local_error is not None:
            return (
                _local_stage_summary(values, cross_clock=False, include_outside=False)
                if values
                else None,
                local_error,
            )
        clock_error = _clock_compatibility_error(snapshot, client_metadata)
        if (
            clock_error == "diagnostics:clock_metadata_mismatch"
            and client_metadata is not None
            and client_metadata.operating_system != snapshot.operating_system
        ):
            clock_error = None
        return (
            _local_stage_summary(values, cross_clock=False, include_outside=False)
            if values
            else None,
            clock_error,
        )
    clock_error = _clock_compatibility_error(snapshot, client_metadata)
    if clock_error is not None:
        values, local_error = _local_stage_values(
            joined, server_values_ns, use_client_timing=True, include_outside=False
        )
        if local_error is not None:
            return (
                _local_stage_summary(values, cross_clock=False, include_outside=False)
                if values
                else None,
                local_error,
            )
        if (
            clock_error == "diagnostics:clock_metadata_mismatch"
            and client_metadata.operating_system != snapshot.operating_system
        ):
            clock_error = None
        return (
            _local_stage_summary(values, cross_clock=False, include_outside=False)
            if values
            else None,
            clock_error,
        )

    strict_client = any(timing.clock_metadata is not None for timing in typed_timings)
    local_values: list[tuple[float, ...]] = []
    shared_values: list[tuple[float, ...]] = []
    for diagnostic_id in expected_ids:
        measured_sample = measured_by_id[diagnostic_id]
        server_sample = next(sample for sample in retained if sample.diagnostic_id == diagnostic_id)
        timing = measured_sample.client_timing
        if timing is None or measured_sample.error is not None:
            return _local_stage_summary(
                local_values, cross_clock=False, include_outside=False
            ) if local_values else None, "diagnostics:measured_sample_incomplete"
        timing_error = _timing_error(timing, strict=strict_client)
        local_values.append(
            _local_server_stage_values(_server_stage_values_ns(server_sample) or (0, 0, 0, 0))
        )
        if timing_error is not None:
            return _local_stage_summary(
                local_values, cross_clock=False, include_outside=False
            ) if local_values else None, timing_error
        server_ns = _server_stage_values_ns(server_sample)
        if server_ns is None:
            return _local_stage_summary(
                local_values, cross_clock=False, include_outside=False
            ) if local_values else None, "diagnostics:server_sample_invalid"
        response_send_complete_ns = server_sample.response_send_complete_ns
        if response_send_complete_ns is None:
            return _local_stage_summary(
                local_values, cross_clock=False, include_outside=False
            ) if local_values else None, "diagnostics:server_sample_invalid"
        external_ns = timing.client_complete_ns - timing.client_request_start_ns
        pre_asgi_ns = server_sample.asgi_entry_ns - timing.client_request_start_ns
        post_send_ns = timing.client_complete_ns - response_send_complete_ns
        if pre_asgi_ns < 0:
            return _local_stage_summary(
                local_values, cross_clock=False, include_outside=False
            ) if local_values else None, "diagnostics:pre_asgi_negative"
        if post_send_ns < 0:
            return _local_stage_summary(
                local_values, cross_clock=False, include_outside=False
            ) if local_values else None, "diagnostics:post_send_negative"
        if external_ns < 0:
            return _local_stage_summary(
                local_values, cross_clock=False, include_outside=False
            ) if local_values else None, "diagnostics:external_wall_negative"
        if pre_asgi_ns + server_ns[-1] + post_send_ns != external_ns:
            return _local_stage_summary(
                local_values, cross_clock=False, include_outside=False
            ) if local_values else None, "diagnostics:cross_clock_identity_mismatch"
        shared_values.append(
            (
                server_ns[0] / 1_000_000.0,
                server_ns[1] / 1_000_000.0,
                server_ns[2] / 1_000_000.0,
                server_ns[3] / 1_000_000.0,
                pre_asgi_ns / 1_000_000.0,
                post_send_ns / 1_000_000.0,
                external_ns / 1_000_000.0,
            )
        )
    return _shared_stage_summary(shared_values), None


def _validate_internal_stats(
    before: EventHistorySingleflightStats,
    after: EventHistorySingleflightStats,
    expected_samples: int,
) -> tuple[StatsValues | None, str | None]:
    if after.leader_count < before.leader_count:
        return None, "stats:leader_count_decreased"
    if after.join_count < before.join_count:
        return None, "stats:join_count_decreased"
    if after.last_sequence < before.last_sequence:
        return None, "stats:last_sequence_decreased"
    if after.last_sequence - before.last_sequence != expected_samples:
        return None, "stats:sequence_delta_mismatch"
    recent = tuple(
        sample for sample in after.timing_samples if sample.sequence > before.last_sequence
    )
    sequences = tuple(sample.sequence for sample in recent)
    if len(recent) != expected_samples:
        return None, "stats:sample_count_mismatch"
    expected_sequences = tuple(range(before.last_sequence + 1, after.last_sequence + 1))
    if len(set(sequences)) != len(sequences) or sequences != expected_sequences:
        return None, "stats:timing_sequence_invalid"
    leader_delta = after.leader_count - before.leader_count
    join_delta = after.join_count - before.join_count
    if leader_delta + join_delta != expected_samples:
        return None, "stats:role_count_mismatch"
    counted_roles = Counter(sample.role for sample in recent)
    role_counts = {
        "leader": counted_roles.get("leader", 0),
        "joiner": counted_roles.get("joiner", 0),
    }
    if role_counts["leader"] != leader_delta or role_counts["joiner"] != join_delta:
        return None, "stats:role_sample_count_mismatch"
    return {
        "leader_count_delta": leader_delta,
        "join_count_delta": join_delta,
        "in_flight_after": after.in_flight,
        "sample_count": len(recent),
        "role_counts": role_counts,
        "internal_stages": _internal_stage_summary(recent),
    }, None


def _client_stage_summary(
    measured: Sequence[Sample],
) -> dict[str, dict[str, float]] | None:
    stage_values: dict[str, list[float]] = {name: [] for name in CLIENT_STAGE_NAMES}
    for sample in measured:
        if sample.error is not None or sample.client_timing is None:
            continue
        for raw_name, value_ns in _client_stage_values(sample.client_timing).items():
            if isinstance(value_ns, int):
                name = f"{raw_name.removesuffix('_ns')}_ms"
                if name not in stage_values:
                    raise AssertionError(f"unrecognized client stage: {raw_name}")
                stage_values[name].append(value_ns / 1_000_000.0)
    summary = {name: _summary(values) for name, values in stage_values.items() if values}
    return summary or None


def _report(
    config: BenchmarkConfig,
    measured: tuple[Sample, ...],
    errors: tuple[str, ...],
    schema_bad: bool,
    stats: StatsValues | None,
    server_stages: dict[str, dict[str, float] | bool] | None = None,
    diagnostic_retention: DiagnosticRetention | None = None,
) -> BenchmarkReport:
    valid = tuple(
        sample for sample in measured if sample.error is None and sample.stages is not None
    )
    stage_values = tuple(sample.stages for sample in valid)
    stages = {
        name: _summary(tuple(values[position] for values in stage_values if values is not None))
        for position, name in enumerate(STAGE_NAMES[config.surface])
        if stage_values
    }
    status_counts = dict(Counter(sample.status for sample in valid if sample.status is not None))
    refusal_counts = dict(
        Counter(sample.refusal_reason for sample in valid if sample.refusal_reason is not None)
    )
    schema_bad = schema_bad or any(sample.schema_error for sample in measured)
    return {
        "surface": config.surface,
        "query_mode": config.query_mode,
        "requests": config.requests,
        "concurrency": config.concurrency,
        "samples": len(valid),
        "errors": len(errors) + sum(sample.error is not None for sample in measured),
        "status_counts": status_counts,
        "refusal_reason_counts": refusal_counts,
        "stages": stages,
        "schema_compatible": bool(valid) and not schema_bad,
        "stats": stats,
        "stats_configured": config.stats_url is not None,
        "server_stages": server_stages,
        "client_stages": _client_stage_summary(measured),
        "diagnostics_configured": config.diagnostics_url is not None,
        "diagnostic_retention": diagnostic_retention,
    }


def _validate_config(config: BenchmarkConfig) -> None:
    if (
        not 1 <= config.requests <= 1000
        or config.concurrency not in (1, 16)
        or not 1 <= config.max_items <= 200
    ):
        raise argparse.ArgumentTypeError(
            "requests 1..1000, concurrency 1|16, max-items 1..200 required"
        )


async def run_benchmark(
    client: httpx.AsyncClient,
    config: BenchmarkConfig,
) -> BenchmarkReport:
    if config.diagnostics_url is None:
        return await _run_benchmark_with_client(client, config)

    diagnostic_client = _new_diagnostic_client()
    use_supplied_mock = isinstance(client._transport, httpx.MockTransport)
    benchmark_client = client if use_supplied_mock else diagnostic_client
    capability_error = _diagnostic_capability_error(benchmark_client)
    if capability_error is not None and not (
        use_supplied_mock and capability_error == "diagnostics:unsupported_transport"
    ):
        if benchmark_client is diagnostic_client:
            await diagnostic_client.aclose()
        return _report(config, (), (capability_error,), False, None)
    if benchmark_client is not client:
        try:
            return await _run_benchmark_with_client(benchmark_client, config)
        finally:
            await diagnostic_client.aclose()
    return await _run_benchmark_with_client(benchmark_client, config)


async def _run_benchmark_with_client(
    client: httpx.AsyncClient,
    config: BenchmarkConfig,
) -> BenchmarkReport:
    _validate_config(config)
    run_id = uuid4().hex[:16]
    warmup_id = None if config.diagnostics_url is None else f"p10at-warmup-{run_id}"
    diagnostic_ids = (
        tuple(f"p10at-measured-{run_id}-{index}" for index in range(1, config.requests + 1))
        if config.diagnostics_url is not None
        else None
    )
    errors: list[str] = []
    schema_bad = False
    diagnostic_before: dict[str, JsonValue] | None = None
    if config.diagnostics_url is not None:
        before_snapshot, error, diagnostics_schema_bad = await _fetch_asgi_diagnostics(
            client, config.diagnostics_url
        )
        schema_bad = diagnostics_schema_bad
        if error is not None or before_snapshot is None:
            return _report(
                config,
                (),
                (error or "diagnostics_unavailable",),
                schema_bad,
                None,
            )
        try:
            diagnostic_before = _serialize_diagnostic_snapshot(before_snapshot)
        except (AttributeError, TypeError, ValidationError, ValueError):
            return _report(
                config,
                (),
                ("diagnostics:retention_schema_invalid",),
                True,
                None,
            )
    warmup = await _request_once(client, config, 0, warmup_id)
    if warmup.error is not None or warmup.refusal_reason is not None or warmup.status == "refused":
        return _report(config, (), (warmup.error or "warmup_refusal",), warmup.schema_error, None)
    before: EventHistorySingleflightStats | None = None
    if config.stats_url is not None:
        before, error, schema_bad = await _fetch_stats(client, config.stats_url)
        if error is not None or before is None:
            return _report(config, (), (error or "stats_unavailable",), schema_bad, None)
    measured = await _collect(client, config, diagnostic_ids)
    stats: StatsValues | None = None
    if config.stats_url is not None and before is not None:
        after, error, after_schema_bad = await _fetch_stats(client, config.stats_url)
        schema_bad = schema_bad or after_schema_bad
        if error is not None or after is None:
            errors.append(error or "stats_unavailable")
        else:
            stats, stats_error = _validate_internal_stats(before, after, config.requests)
            if stats_error is not None:
                errors.append(stats_error)
    server_stages: dict[str, dict[str, float] | bool] | None = None
    diagnostic_retention: DiagnosticRetention | None = None
    if config.diagnostics_url is not None and diagnostic_ids is not None:
        snapshot, error, diagnostics_schema_bad = await _fetch_asgi_diagnostics(
            client, config.diagnostics_url
        )
        schema_bad = schema_bad or diagnostics_schema_bad
        if error is not None or snapshot is None:
            errors.append(error or "diagnostics_unavailable")
        else:
            try:
                diagnostic_after = _serialize_diagnostic_snapshot(snapshot)
            except (AttributeError, TypeError, ValidationError, ValueError):
                errors.append("diagnostics:retention_schema_invalid")
            else:
                assert diagnostic_before is not None
                diagnostic_retention = {
                    "before": diagnostic_before,
                    "after": diagnostic_after,
                    "measured_ids": list(diagnostic_ids),
                }
                server_stages, diagnostics_error = _validate_asgi_diagnostics(
                    snapshot, measured, diagnostic_ids
                )
                if diagnostics_error is not None:
                    errors.append(diagnostics_error)
    return _report(
        config,
        measured,
        tuple(errors),
        schema_bad,
        stats,
        server_stages,
        diagnostic_retention,
    )


def benchmark_exit_code(report: BenchmarkReport) -> int:
    return int(
        not (
            report["errors"] == 0
            and report["samples"] == report["requests"]
            and report["schema_compatible"]
            and (not report["stats_configured"] or report["stats"] is not None)
            and (not report["diagnostics_configured"] or report["server_stages"] is not None)
            and (not report["diagnostics_configured"] or report["diagnostic_retention"] is not None)
        )
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="External world-memory stage benchmark")
    parser.add_argument("--surface", choices=("api", "coordinator"), default="api")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--stats-url", default=None)
    parser.add_argument("--requests", type=int, default=8)
    parser.add_argument("--concurrency", type=int, choices=(1, 16), default=1)
    parser.add_argument("--max-items", type=int, default=10)
    parser.add_argument("--query", default="historical")
    parser.add_argument("--query-mode", choices=("identical", "unique"), default="identical")
    parser.add_argument("--event-time-lte", type=datetime.fromisoformat, default=None)
    parser.add_argument(
        "--diagnostics-url",
        "--asgi-diagnostics-url",
        dest="diagnostics_url",
        default=None,
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    config = BenchmarkConfig(
        args.surface,
        args.base_url,
        args.requests,
        args.concurrency,
        args.max_items,
        args.query,
        args.query_mode,
        args.event_time_lte,
        args.stats_url,
        args.diagnostics_url,
    )

    async def execute() -> BenchmarkReport:
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await run_benchmark(client, config)

    try:
        report = anyio.run(execute)
    except argparse.ArgumentTypeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    output = dict(report)
    output["schema_compatibility"] = {"response_fields": report["schema_compatible"]}
    del output["schema_compatible"]
    if not report["stats_configured"]:
        del output["stats"]
    if not report["diagnostics_configured"]:
        del output["server_stages"]
        del output["client_stages"]
        del output["diagnostic_retention"]
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return benchmark_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
