# noqa: SIZE_OK
# /// script
# requires-python = ">=3.12"
# dependencies = ["anyio", "httpcore==1.0.9", "httpx==0.28.1"]
# ///
# ─── How to run ───
# uv run scripts/benchmark_world_memory_linux_dispatch_attribution.py --help
"""Standalone P10-AV Linux dispatch attribution helpers and CLI."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import marshal
import math
import platform
import struct
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from time import get_clock_info, perf_counter_ns
from typing import Final, Literal, TypedDict, assert_never

import anyio
import httpcore
import httpx
from httpcore._async.connection import AsyncHTTPConnection
from httpcore._async.connection_pool import AsyncPoolRequest
from httpcore._async.http11 import AsyncHTTP11Connection

_ASYNC_HTTP_TRANSPORT: Final[type[httpx.AsyncHTTPTransport]] = httpx.AsyncHTTPTransport
_ASYNC_CONNECTION_POOL: Final[type[httpcore.AsyncConnectionPool]] = httpcore.AsyncConnectionPool
_ASYNC_POOL_REQUEST: Final[type[AsyncPoolRequest]] = AsyncPoolRequest
_ASYNC_HTTP_CONNECTION: Final[type[AsyncHTTPConnection]] = AsyncHTTPConnection
_ASYNC_HTTP11_CONNECTION: Final[type[AsyncHTTP11Connection]] = AsyncHTTP11Connection
_ORIGINAL_POOL_HANDLE: Final = _ASYNC_CONNECTION_POOL.handle_async_request
_ORIGINAL_POOL_INIT: Final = _ASYNC_POOL_REQUEST.__init__
_ORIGINAL_POOL_ASSIGN: Final = _ASYNC_POOL_REQUEST.assign_to_connection
_ORIGINAL_POOL_WAIT: Final = _ASYNC_POOL_REQUEST.wait_for_connection
_ORIGINAL_POOL_CLEAR: Final = _ASYNC_POOL_REQUEST.clear_connection
_ORIGINAL_CONNECTION_HANDLE: Final = _ASYNC_HTTP_CONNECTION.handle_async_request
_ORIGINAL_HTTP11_HANDLE: Final = _ASYNC_HTTP11_CONNECTION.handle_async_request

HTTPX_VERSION: Final[str] = "0.28.1"
HTTPCORE_VERSION: Final[str] = "1.0.9"
MATRIX_SIZE: Final[int] = 100
LIMITER_CAPACITY: Final[int] = 16
RETENTION_DELTA: Final[int] = 101
QUERY: Final[str] = "p10b rehearsal telemetry reading"
CUTOFF: Final[str] = "2026-09-01T00:00:00+00:00"
ASGI_DIAGNOSTIC_HEADER_NAME: Final[str] = "x-world-memory-event-history-diagnostic-id"
ASGI_DIAGNOSTIC_HEADER_BYTES: Final[bytes] = ASGI_DIAGNOSTIC_HEADER_NAME.encode("ascii")
WORLD_MEMORY_QUERY_PATH: Final[str] = "/api/v1/world-memory/query"
MAX_DIAGNOSTIC_ID_LENGTH: Final[int] = 64
BUCKET_NAMES: Final[tuple[str, ...]] = (
    "client_pre_pool_ns",
    "pool_request_to_wait_return_ns",
    "connection_dispatch_to_first_trace_ns",
    "first_trace_to_asgi_ns",
)
TraceMode = Literal["burst", "fixed"]
Placement = Literal["sidecar", "loopback"]

DIAGNOSTIC_LIMITS: Final[httpx.Limits] = httpx.Limits(
    max_connections=LIMITER_CAPACITY,
    max_keepalive_connections=LIMITER_CAPACITY,
)


def new_diagnostic_client() -> httpx.AsyncClient:
    """Create the one explicit cleartext HTTP/1.1 client allowed by P10-AV."""
    transport = _ASYNC_HTTP_TRANSPORT(
        http1=True,
        http2=False,
        limits=DIAGNOSTIC_LIMITS,
        retries=0,
    )
    return httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        timeout=30.0,
    )


@dataclass(frozen=True, slots=True)
class RequestContractError(Exception):
    """Describe a request shape that cannot be sent under the locked contract."""

    code: str

    def __str__(self) -> str:
        return self.code


def build_request_payload() -> dict[str, str | int | bool]:
    """Build the immutable accepted P10-AU query payload for every request."""
    return {
        "query": QUERY,
        "event_time_lte": CUTOFF,
        "require_evidence": True,
        "max_items": 10,
    }


def build_request_headers(identifier: str) -> dict[str, str]:
    """Build the sole diagnostic header without changing request semantics."""
    if (
        type(identifier) is not str
        or not 1 <= len(identifier) <= MAX_DIAGNOSTIC_ID_LENGTH
        or not all(0x21 <= ord(character) <= 0x7E for character in identifier)
    ):
        raise RequestContractError("request:diagnostic_id_invalid")
    return {ASGI_DIAGNOSTIC_HEADER_NAME: identifier}


def build_request_url(base_url: str) -> str:
    """Append the fixed world-memory query path to one validated endpoint base."""
    if type(base_url) is not str or not base_url:
        raise RequestContractError("request:base_url_invalid")
    return f"{base_url.rstrip('/')}{WORLD_MEMORY_QUERY_PATH}"


_HTTP11: Final[tuple[str, ...]] = (
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
_CONNECT: Final[tuple[str, ...]] = (
    "connection.connect_tcp.started",
    "connection.connect_tcp.complete",
)
_FRESH_TRACE: Final[tuple[str, ...]] = _CONNECT + _HTTP11
_REUSED_TRACE: Final[tuple[str, ...]] = _HTTP11


@dataclass(frozen=True, slots=True)
class ClientShape:
    """Capabilities that must be true before a diagnostic request is sent."""

    httpx_version: str = HTTPX_VERSION
    httpcore_version: str = HTTPCORE_VERSION
    transport: str = "async-http-transport"
    transport_exact: bool = True
    pool_exact: bool = True
    http1: bool = True
    http2: bool = False
    trust_env: bool = False
    mounts: bool = False
    proxy: bool = False
    follow_redirects: bool = False
    max_connections: int = LIMITER_CAPACITY
    max_keepalive_connections: int = LIMITER_CAPACITY
    private_manifest_valid: bool = True
    nested_hooks: bool = False


def validate_capabilities(shape: ClientShape) -> str | None:
    """Return the first fail-closed capability error, or ``None``."""
    if type(shape) is not ClientShape:
        return "capability:shape_invalid"
    if (
        any(
            type(value) is not str
            for value in (shape.httpx_version, shape.httpcore_version, shape.transport)
        )
        or any(
            type(value) is not bool
            for value in (
                shape.transport_exact,
                shape.pool_exact,
                shape.http1,
                shape.http2,
                shape.trust_env,
                shape.mounts,
                shape.proxy,
                shape.follow_redirects,
                shape.private_manifest_valid,
                shape.nested_hooks,
            )
        )
        or any(
            type(value) is not int
            for value in (shape.max_connections, shape.max_keepalive_connections)
        )
    ):
        return "capability:shape_invalid"
    if shape.httpx_version != HTTPX_VERSION:
        return "capability:unsupported_httpx_version"
    if shape.httpcore_version != HTTPCORE_VERSION:
        return "capability:unsupported_httpcore_version"
    if shape.transport != "async-http-transport" or not shape.transport_exact:
        return "capability:unsupported_transport"
    if not shape.pool_exact:
        return "capability:unsupported_pool"
    if not shape.http1:
        return "capability:unsupported_http_protocol"
    if shape.http2:
        return "capability:unsupported_http_protocol"
    if shape.trust_env:
        return "capability:trust_env_enabled"
    if shape.mounts:
        return "capability:custom_mounts_unsupported"
    if shape.proxy:
        return "capability:proxy_unsupported"
    if shape.follow_redirects:
        return "capability:redirects_unsupported"
    if (
        shape.max_connections != LIMITER_CAPACITY
        or shape.max_keepalive_connections != LIMITER_CAPACITY
    ):
        return "capability:limits_not_explicit"
    if shape.nested_hooks:
        return "capability:nested_hooks"
    if not shape.private_manifest_valid:
        return "capability:private_fingerprint_mismatch"
    return None


def _client_shape(client: httpx.AsyncClient) -> ClientShape:
    """Extract the locked transport shape without sending a request."""
    transport = client._transport
    httpx_version = str(getattr(httpx, "__version__", ""))
    httpcore_version = str(getattr(httpcore, "__version__", ""))
    if type(transport) is not _ASYNC_HTTP_TRANSPORT:
        return ClientShape(
            httpx_version=httpx_version,
            httpcore_version=httpcore_version,
            transport=type(transport).__name__,
            transport_exact=False,
            pool_exact=False,
        )
    pool = transport._pool
    return ClientShape(
        httpx_version=httpx_version,
        httpcore_version=httpcore_version,
        transport_exact=True,
        pool_exact=type(pool) is _ASYNC_CONNECTION_POOL,
        http1=pool._http1,
        http2=pool._http2,
        trust_env=client._trust_env,
        mounts=bool(client._mounts),
        proxy=pool._proxy is not None,
        follow_redirects=client.follow_redirects,
        max_connections=pool._max_connections,
        max_keepalive_connections=pool._max_keepalive_connections,
        private_manifest_valid=validate_private_manifest(build_private_manifest()) is None,
    )


def _diagnostic_capability_error(client: httpx.AsyncClient) -> str | None:
    """Validate versions and private client shape before warmup."""
    if httpx.__version__ != HTTPX_VERSION:
        return "capability:unsupported_httpx_version"
    if httpcore.__version__ != HTTPCORE_VERSION:
        return "capability:unsupported_httpcore_version"
    return validate_capabilities(_client_shape(client))


@dataclass(frozen=True, slots=True)
class FrozenCallable:
    name: str
    class_path: str
    module: str
    qualname: str
    signature: str
    source_sha256: str
    callable_sha256: str


_FROZEN_CALLABLES: Final[tuple[FrozenCallable, ...]] = (
    FrozenCallable(
        "AsyncConnectionPool.handle_async_request",
        "httpcore.AsyncConnectionPool",
        "httpcore._async.connection_pool",
        "AsyncConnectionPool.handle_async_request",
        "(self, request: 'Request') -> 'Response'",
        "8a3251174ff06224b1eebd0e326134f644b712406b5c19961ff3c3fc17a8bc30",
        "e2143de9af1125edd833cb80d791ed6b45f76ed338bd043709684e258cdf4fc5",
    ),
    FrozenCallable(
        "AsyncPoolRequest.__init__",
        "httpcore._async.connection_pool.AsyncPoolRequest",
        "httpcore._async.connection_pool",
        "AsyncPoolRequest.__init__",
        "(self, request: 'Request') -> 'None'",
        "280ab07b6ef52fd57b279a6b23eb73a5457a25e077f246e9613e613e0a7a7452",
        "e1e346e9f662ce0a2f0ab90799f86380fb776da39eb71bcf77f5b50b8a1724c8",
    ),
    FrozenCallable(
        "AsyncPoolRequest.assign_to_connection",
        "httpcore._async.connection_pool.AsyncPoolRequest",
        "httpcore._async.connection_pool",
        "AsyncPoolRequest.assign_to_connection",
        "(self, connection: 'AsyncConnectionInterface | None') -> 'None'",
        "d6fd1be904143f82ea74218fb11f217efefc9bb52e1d16b644c2948bf4ce862a",
        "32f370b304e9ee3e7f23cdeb672eded45b1445787f06bdfc396965757885c8ab",
    ),
    FrozenCallable(
        "AsyncPoolRequest.wait_for_connection",
        "httpcore._async.connection_pool.AsyncPoolRequest",
        "httpcore._async.connection_pool",
        "AsyncPoolRequest.wait_for_connection",
        "(self, timeout: 'float | None' = None) -> 'AsyncConnectionInterface'",
        "94a7b4acb9819919bcc17831c61f1e032112b993573c21f494892a6609cdf139",
        "5bc3d481080fb869743d1773cb414f5aeab36e7e31af87f8e1edfcf254d9d25c",
    ),
    FrozenCallable(
        "AsyncPoolRequest.clear_connection",
        "httpcore._async.connection_pool.AsyncPoolRequest",
        "httpcore._async.connection_pool",
        "AsyncPoolRequest.clear_connection",
        "(self) -> 'None'",
        "6dac5cd0864e5a303ed64baf55c2383860b6631d6670173975b5c214a08c7443",
        "c613cc203aed402662ed9cda1174d03e2c14612405e5feb54dd6967526afe6ec",
    ),
    FrozenCallable(
        "AsyncHTTPConnection.handle_async_request",
        "httpcore.AsyncHTTPConnection",
        "httpcore._async.connection",
        "AsyncHTTPConnection.handle_async_request",
        "(self, request: 'Request') -> 'Response'",
        "b76bc6e38ffd6565aaca616a36fb6dc67a266cebd0cb2a5e2aab884ae4ba0123",
        "4feb4d8973f1eb369e69d0ccac6f82477ebe07f68923cfd13507d49b99a8fb19",
    ),
    FrozenCallable(
        "AsyncHTTP11Connection.handle_async_request",
        "httpcore.AsyncHTTP11Connection",
        "httpcore._async.http11",
        "AsyncHTTP11Connection.handle_async_request",
        "(self, request: 'Request') -> 'Response'",
        "e290325d4e334108dea869723879c6c57d1ec8eb8c2b1603033eeaedc20d75f3",
        "c163e76798a19ffdf71dd537d2d552b622c207fae899627310b56bb030f90290",
    ),
)


@dataclass(frozen=True, slots=True)
class PrivateManifest:
    class_identities: tuple[tuple[str, str], ...]
    signatures: tuple[tuple[str, str], ...]
    source_fingerprints: tuple[tuple[str, str], ...]
    callable_fingerprints: tuple[tuple[str, str], ...]


def private_manifest() -> PrivateManifest:
    """Return the byte-frozen accepted private callable manifest."""
    return PrivateManifest(
        tuple((item.name, item.class_path) for item in _FROZEN_CALLABLES),
        tuple((item.name, item.signature) for item in _FROZEN_CALLABLES),
        tuple((item.name, item.source_sha256) for item in _FROZEN_CALLABLES),
        tuple((item.name, item.callable_sha256) for item in _FROZEN_CALLABLES),
    )


def _fingerprint(fn: Callable[..., object]) -> str:  # noqa: OBJECT_OK
    """Hash one callable using the canonical P10AV-CALLABLE-FP-v1 bytes."""
    fields = (
        fn.__module__.encode("utf-8"),
        fn.__qualname__.encode("utf-8"),
        str(inspect.signature(fn)).encode("utf-8"),
        marshal.dumps(fn.__code__, 4),
    )
    payload = b"P10AV-CALLABLE-FP-v1\0" + b"".join(
        struct.pack(">Q", len(field)) + field for field in fields
    )
    return hashlib.sha256(payload).hexdigest()


def _source_hash(fn: Callable[..., object]) -> str:  # noqa: OBJECT_OK
    return hashlib.sha256(inspect.getsource(fn).encode("utf-8")).hexdigest()


def _private_targets() -> tuple[tuple[str, type[object], Callable[..., object]], ...]:  # noqa: OBJECT_OK
    """Return the exact classes and callables in the locked order."""
    return (
        (
            "AsyncConnectionPool.handle_async_request",
            _ASYNC_CONNECTION_POOL,
            _ASYNC_CONNECTION_POOL.handle_async_request,
        ),
        ("AsyncPoolRequest.__init__", _ASYNC_POOL_REQUEST, _ASYNC_POOL_REQUEST.__init__),
        (
            "AsyncPoolRequest.assign_to_connection",
            _ASYNC_POOL_REQUEST,
            _ASYNC_POOL_REQUEST.assign_to_connection,
        ),
        (
            "AsyncPoolRequest.wait_for_connection",
            _ASYNC_POOL_REQUEST,
            _ASYNC_POOL_REQUEST.wait_for_connection,
        ),
        (
            "AsyncPoolRequest.clear_connection",
            _ASYNC_POOL_REQUEST,
            _ASYNC_POOL_REQUEST.clear_connection,
        ),
        (
            "AsyncHTTPConnection.handle_async_request",
            _ASYNC_HTTP_CONNECTION,
            _ASYNC_HTTP_CONNECTION.handle_async_request,
        ),
        (
            "AsyncHTTP11Connection.handle_async_request",
            _ASYNC_HTTP11_CONNECTION,
            _ASYNC_HTTP11_CONNECTION.handle_async_request,
        ),
    )


_PRIVATE_ORIGINALS: Final = (
    ("AsyncConnectionPool.handle_async_request", _ORIGINAL_POOL_HANDLE),
    ("AsyncPoolRequest.__init__", _ORIGINAL_POOL_INIT),
    ("AsyncPoolRequest.assign_to_connection", _ORIGINAL_POOL_ASSIGN),
    ("AsyncPoolRequest.wait_for_connection", _ORIGINAL_POOL_WAIT),
    ("AsyncPoolRequest.clear_connection", _ORIGINAL_POOL_CLEAR),
    ("AsyncHTTPConnection.handle_async_request", _ORIGINAL_CONNECTION_HANDLE),
    ("AsyncHTTP11Connection.handle_async_request", _ORIGINAL_HTTP11_HANDLE),
)


def _unavailable_manifest() -> PrivateManifest:
    """Return a deterministic mismatch manifest when a private target is unusable."""
    names = tuple(item.name for item in _FROZEN_CALLABLES)
    return PrivateManifest(
        tuple((name, "<unavailable>") for name in names),
        tuple((name, "<unavailable>") for name in names),
        tuple((name, "<unavailable>") for name in names),
        tuple((name, "<unavailable>") for name in names),
    )


def build_private_manifest() -> PrivateManifest:
    """Generate the actual runtime manifest from installed private objects."""
    identities: list[tuple[str, str]] = []
    signatures: list[tuple[str, str]] = []
    sources: list[tuple[str, str]] = []
    fingerprints: list[tuple[str, str]] = []
    try:
        targets = _private_targets()
        for name, cls, fn in targets:
            item = next(item for item in _FROZEN_CALLABLES if item.name == name)
            if (
                f"{cls.__module__}.{cls.__qualname__}" != item.class_path
                or fn.__module__ != item.module
                or fn.__qualname__ != item.qualname
            ):
                return _unavailable_manifest()
            identities.append((name, f"{cls.__module__}.{cls.__qualname__}"))
            signatures.append((name, str(inspect.signature(fn))))
            sources.append((name, _source_hash(fn)))
            fingerprints.append((name, _fingerprint(fn)))
    except (AttributeError, OSError, TypeError, ValueError):
        return _unavailable_manifest()
    return PrivateManifest(
        tuple(identities), tuple(signatures), tuple(sources), tuple(fingerprints)
    )


def validate_private_manifest(manifest: PrivateManifest) -> str | None:
    """Require exact module, signature, source, and callable identity data."""
    return None if manifest == private_manifest() else "capability:private_fingerprint_mismatch"


@dataclass(frozen=True, slots=True)
class TraceEvent:
    name: str
    timestamp_ns: int


@dataclass(frozen=True, slots=True)
class TraceValidation:
    fresh: bool
    connect_pair_ns: tuple[int, int] | None
    first_event_ns: int
    request_body_ns: int
    events: tuple[TraceEvent, ...]


def validate_trace(
    events: Sequence[TraceEvent], *, http_version: str = "HTTP/1.1"
) -> tuple[TraceValidation | None, str | None]:
    """Accept only one cleartext HTTP/1.1 fresh or reused trace sequence."""
    if http_version != "HTTP/1.1":
        return None, "trace:unsupported_http_version"
    if any(type(event) is not TraceEvent or type(event.name) is not str for event in events):
        return None, "trace:event_invalid"
    names = tuple(event.name for event in events)
    if names not in (_FRESH_TRACE, _REUSED_TRACE):
        return None, "trace:sequence_invalid"
    if any(type(event.timestamp_ns) is not int for event in events):
        return None, "trace:timestamp_not_integer"
    if any(event.timestamp_ns < 0 for event in events):
        return None, "trace:timestamp_negative"
    if any(
        left.timestamp_ns > right.timestamp_ns
        for left, right in zip(events, events[1:], strict=False)
    ):
        return None, "trace:timestamp_out_of_order"
    offset = 2 if names == _FRESH_TRACE else 0
    connect = None if offset == 0 else (events[0].timestamp_ns, events[1].timestamp_ns)
    return (
        TraceValidation(
            fresh=offset == 2,
            connect_pair_ns=connect,
            first_event_ns=events[0].timestamp_ns,
            request_body_ns=events[offset + 3].timestamp_ns - events[offset + 2].timestamp_ns,
            events=tuple(events),
        ),
        None,
    )


@dataclass(frozen=True, slots=True)
class ClockMetadata:
    operating_system: str
    boot_id: str | None
    implementation: str
    resolution: float
    monotonic: bool
    adjustable: bool


def _linux_boot_id() -> str | None:
    """Read the kernel boot ID only on Linux; other hosts cannot share this clock."""
    if platform.system() != "Linux":
        return None
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value or None


def capture_clock_metadata() -> ClockMetadata:
    """Capture the monotonic clock identity used by client and server samples."""
    clock = get_clock_info("perf_counter")
    return ClockMetadata(
        operating_system=platform.system(),
        boot_id=_linux_boot_id(),
        implementation=clock.implementation,
        resolution=clock.resolution,
        monotonic=clock.monotonic,
        adjustable=clock.adjustable,
    )


@dataclass(frozen=True, slots=True)
class ClockBracket:
    fetch_start_ns: int
    fetch_end_ns: int
    snapshot_captured_ns: int


@dataclass(frozen=True, slots=True)
class ClockDecision:
    cross_clock: bool
    error: str | None
    pre_asgi_ns: int | None = None


def check_clock_compatibility(
    client: ClockMetadata, server: ClockMetadata, bracket: ClockBracket
) -> ClockDecision:
    """Permit subtraction only for equal Linux metadata and a valid fetch bracket."""
    if type(client) is not ClockMetadata or type(server) is not ClockMetadata:
        return ClockDecision(False, "clock:metadata_invalid")
    if type(bracket) is not ClockBracket:
        return ClockDecision(False, "clock:bracket_invalid")
    metadata = (client, server)
    if any(
        (
            type(item.operating_system) is not str
            or type(item.boot_id) is not str
            or not item.boot_id
            or type(item.implementation) is not str
            or not item.implementation
            or type(item.resolution) not in (int, float)
            or isinstance(item.resolution, bool)
            or not math.isfinite(item.resolution)
            or item.resolution <= 0
            or item.monotonic is not True
            or item.adjustable is not False
        )
        for item in metadata
    ):
        return ClockDecision(False, "clock:metadata_invalid")
    if client != server or client.operating_system != "Linux":
        return ClockDecision(False, "clock:metadata_mismatch")
    if any(
        type(value) is not int or value < 0
        for value in (
            bracket.fetch_start_ns,
            bracket.fetch_end_ns,
            bracket.snapshot_captured_ns,
        )
    ):
        return ClockDecision(False, "clock:bracket_invalid")
    if bracket.fetch_start_ns > bracket.fetch_end_ns:
        return ClockDecision(False, "clock:bracket_invalid")
    if not bracket.fetch_start_ns <= bracket.snapshot_captured_ns <= bracket.fetch_end_ns:
        return ClockDecision(False, "clock:fetch_bracket_mismatch")
    return ClockDecision(True, None)


@dataclass(frozen=True, slots=True)
class OperationLedger:
    pool_request_count: int = 1
    assignment_count: int = 1
    wait_success_count: int = 1
    outer_attempt_count: int = 1
    protocol_entry_count: int = 1
    clear_count: int = 0
    reassignment_count: int = 0
    retry_count: int = 0
    connection_not_available_count: int = 0


def validate_operation_ledger(ledger: OperationLedger) -> str | None:
    if type(ledger) is not OperationLedger:
        return "landmark:operation_count_invalid"
    counts = (
        ledger.pool_request_count,
        ledger.assignment_count,
        ledger.wait_success_count,
        ledger.outer_attempt_count,
        ledger.protocol_entry_count,
        ledger.clear_count,
        ledger.reassignment_count,
        ledger.retry_count,
        ledger.connection_not_available_count,
    )
    if any(type(count) is not int or count < 0 for count in counts):
        return "landmark:operation_count_invalid"
    if ledger.pool_request_count != 1:
        return "landmark:operation_count_invalid"
    expected_attempts = 1 + ledger.reassignment_count
    if (
        ledger.assignment_count != expected_attempts
        or ledger.wait_success_count != expected_attempts
        or ledger.outer_attempt_count != expected_attempts
        or ledger.protocol_entry_count != expected_attempts
        or ledger.clear_count != ledger.reassignment_count
        or ledger.retry_count != ledger.reassignment_count
        or ledger.connection_not_available_count != ledger.reassignment_count
    ):
        return "landmark:operation_count_invalid"
    return None


@dataclass(frozen=True, slots=True)
class ObserverPolicy:
    extra_trace_callbacks: int = 0
    performs_io: bool = False
    acquires_locks: bool = False
    awaits: bool = False
    schedules_tasks: bool = False
    mutates_requests: bool = False
    mutates_responses: bool = False
    mutates_limits: bool = False


def validate_observer_policy(policy: ObserverPolicy) -> str | None:
    """Enforce the bounded hook observer-effect boundary."""
    if type(policy) is not ObserverPolicy:
        return "probe:policy_invalid"
    if type(policy.extra_trace_callbacks) is not int or policy.extra_trace_callbacks < 0:
        return "probe:policy_invalid"
    if policy.extra_trace_callbacks != 0:
        return "probe:extra_trace_callback"
    if any(
        (
            policy.performs_io,
            policy.acquires_locks,
            policy.awaits,
            policy.schedules_tasks,
            policy.mutates_requests,
            policy.mutates_responses,
            policy.mutates_limits,
        )
    ):
        return "probe:observer_effect"
    return None


HookFunction = Callable[..., int]


@dataclass(frozen=True, slots=True)
class HookState:
    request_once: HookFunction
    private_hooks: tuple[HookFunction, ...]

    @classmethod
    def demo(cls) -> HookState:
        def hook(*_values: int) -> int:
            return 1

        return cls(hook, (hook, hook, hook, hook, hook, hook))


@dataclass(slots=True)  # noqa: MUTABLE_OK - lifecycle state is restored in finally.
class ProbeRun:
    original: HookState
    current: HookState | None = None
    restored: bool = False
    identity_restored: bool = False

    def __post_init__(self) -> None:
        self.current = self.original

    def finish(self, outcome: str) -> None:
        """Restore every original hook for every bounded exit outcome."""
        try:
            if outcome not in {"success", "exception", "cancelled", "unsupported"}:
                return
        finally:
            self.current = self.original
            self.restored = True
            self.identity_restored = self.current is self.original


def validate_hook_state(*, prepatched: bool = False, nested: bool = False) -> str | None:
    """Reject state that would make exact private restoration ambiguous."""
    if prepatched:
        return "probe:prepatched"
    if nested:
        return "probe:nested"
    return None


@dataclass(frozen=True, slots=True)
class ProbeInstallationError(Exception):
    code: str

    def __str__(self) -> str:
        return self.code


@dataclass(slots=True)  # noqa: MUTABLE_OK - one bounded observation is updated per ID.
class ProbeObservation:
    """Bounded private-operation observations for one exact diagnostic ID."""

    diagnostic_id: str
    pool_request_created_ns: int | None = None
    assignment_ns: int | None = None
    pool_wait_return_ns: int | None = None
    connection_dispatch_enter_ns: int | None = None
    protocol_entry_ns: int | None = None
    pool_request_count: int = 0
    assignment_count: int = 0
    wait_success_count: int = 0
    outer_attempt_count: int = 0
    protocol_entry_count: int = 0
    clear_count: int = 0
    reassignment_count: int = 0
    retry_count: int = 0
    connection_not_available_count: int = 0
    unknown_id_count: int = 0


def _request_diagnostic_id(request: httpcore.Request) -> str | None:
    """Look up the bounded diagnostic ID from an httpcore request header."""
    for name, value in request.headers:
        if name.lower() == ASGI_DIAGNOSTIC_HEADER_BYTES:
            try:
                identifier = value.decode("ascii")
            except UnicodeDecodeError:
                return None
            return identifier or None
    return None


@dataclass(slots=True)  # noqa: MUTABLE_OK
class PrivateProbe:
    """Install benchmark-only wrappers and restore exact private objects."""

    originals: tuple[tuple[type[object], str, Callable[..., object]], ...] = ()  # noqa: OBJECT_OK
    restored: bool = False
    records: dict[str, ProbeObservation] = field(default_factory=dict)
    unknown_id_events: int = 0
    installed: bool = False

    def begin(self, diagnostic_ids: Sequence[str]) -> None:
        """Start one bounded per-ID observation store before dispatch begins."""
        identifiers = tuple(diagnostic_ids)
        if len(set(identifiers)) != len(identifiers):
            raise ProbeInstallationError("probe:duplicate_diagnostic_id")
        self.records = {identifier: ProbeObservation(identifier) for identifier in identifiers}
        self.unknown_id_events = 0

    def _lookup(self, identifier: str | None) -> ProbeObservation | None:
        if identifier is None:
            self.unknown_id_events += 1
            return None
        observation = self.records.get(identifier)
        if observation is None:
            self.unknown_id_events += 1
        return observation

    def operation_ledger(self, identifier: str) -> OperationLedger:
        """Snapshot one observation's operation counts for post-request validation."""
        observation = self.records.get(identifier)
        if observation is None:
            return OperationLedger(
                pool_request_count=0,
                assignment_count=0,
                wait_success_count=0,
                outer_attempt_count=0,
                protocol_entry_count=0,
            )
        return OperationLedger(
            pool_request_count=observation.pool_request_count,
            assignment_count=observation.assignment_count,
            wait_success_count=observation.wait_success_count,
            outer_attempt_count=observation.outer_attempt_count,
            protocol_entry_count=observation.protocol_entry_count,
            clear_count=observation.clear_count,
            reassignment_count=observation.reassignment_count,
            retry_count=observation.retry_count,
            connection_not_available_count=observation.connection_not_available_count,
        )

    def _pool_request_observation(self, value: object) -> ProbeObservation | None:  # noqa: OBJECT_OK
        if type(value) is not _ASYNC_POOL_REQUEST:
            return None
        return self._lookup(_request_diagnostic_id(value.request))

    def _request_observation(self, args: tuple[object, ...]) -> ProbeObservation | None:  # noqa: OBJECT_OK
        if len(args) < 2 or not isinstance(args[1], httpcore.Request):
            return None
        return self._lookup(_request_diagnostic_id(args[1]))

    def _wrap_sync(
        self,
        name: str,
        original: Callable[..., object],  # noqa: OBJECT_OK
    ) -> Callable[..., object]:  # noqa: OBJECT_OK
        """Wrap a synchronous private operation with bounded timestamp assignment."""

        def wrapper(*args: object, **kwargs: object) -> object:  # noqa: OBJECT_OK
            observation: ProbeObservation | None
            if name == "AsyncPoolRequest.__init__":
                result = original(*args, **kwargs)
                observation = (
                    self._lookup(_request_diagnostic_id(args[1]))
                    if len(args) >= 2 and isinstance(args[1], httpcore.Request)
                    else None
                )
                if observation is not None:
                    observation.pool_request_created_ns = perf_counter_ns()
                    observation.pool_request_count += 1
                return result
            if name == "AsyncPoolRequest.assign_to_connection":
                observation = self._pool_request_observation(args[0]) if args else None
                if observation is not None:
                    timestamp_ns = perf_counter_ns()
                    if observation.assignment_ns is not None:
                        observation.reassignment_count += 1
                    observation.assignment_ns = timestamp_ns
                    observation.assignment_count += 1
                return original(*args, **kwargs)
            if name == "AsyncPoolRequest.clear_connection":
                observation = self._pool_request_observation(args[0]) if args else None
                if observation is not None:
                    observation.clear_count += 1
                return original(*args, **kwargs)
            return original(*args, **kwargs)

        return wrapper

    def _wrap_async(
        self,
        name: str,
        original: Callable[..., object],  # noqa: OBJECT_OK
    ) -> Callable[..., object]:  # noqa: OBJECT_OK
        """Wrap an async private operation without adding I/O or scheduling."""

        def wrapper(*args: object, **kwargs: object) -> object:  # noqa: OBJECT_OK
            observation = self._request_observation(args)
            if name == "AsyncPoolRequest.wait_for_connection":
                observation = self._pool_request_observation(args[0]) if args else None
            if name == "AsyncHTTPConnection.handle_async_request" and observation is not None:
                observation.connection_dispatch_enter_ns = perf_counter_ns()
                observation.outer_attempt_count += 1
            if name == "AsyncHTTP11Connection.handle_async_request" and observation is not None:
                observation.protocol_entry_ns = perf_counter_ns()
                observation.protocol_entry_count += 1
            result = original(*args, **kwargs)
            if not isinstance(result, Awaitable):
                return result

            async def finish(awaitable: Awaitable[object]) -> object:  # noqa: OBJECT_OK
                try:
                    value = await awaitable
                except httpcore.ConnectionNotAvailable:
                    if (
                        name == "AsyncHTTPConnection.handle_async_request"
                        and observation is not None
                    ):
                        observation.retry_count += 1
                        observation.connection_not_available_count += 1
                    raise
                if name == "AsyncPoolRequest.wait_for_connection" and observation is not None:
                    observation.pool_wait_return_ns = perf_counter_ns()
                    observation.wait_success_count += 1
                return value

            return finish(result)

        return wrapper

    def install(self) -> None:
        """Install only when every frozen private callable is still original."""
        if self.originals and not self.restored:
            raise ProbeInstallationError("probe:nested")
        entries = _private_targets()
        expected = dict(_PRIVATE_ORIGINALS)
        for name, target, current in entries:
            if current is not expected[name]:
                raise ProbeInstallationError("probe:prepatched_or_nested")
        self.originals = tuple(
            (target, current.__name__, expected[name]) for name, target, current in entries
        )
        try:
            for name, target, attribute, original in (
                (name, target, current.__name__, expected[name])
                for name, target, current in entries
            ):
                wrapper = (
                    self._wrap_sync(name, original)
                    if name.startswith("AsyncPoolRequest.")
                    and name != "AsyncPoolRequest.wait_for_connection"
                    else self._wrap_async(name, original)
                )
                setattr(target, attribute, wrapper)
        except (AttributeError, TypeError) as exc:
            self.restore()
            raise ProbeInstallationError("probe:installation_failed") from exc
        self.restored = False
        self.installed = True

    def restore(self) -> None:
        """Restore each original object and prove identity before client close."""
        for target, attribute, original in self.originals:
            setattr(target, attribute, original)
        self.restored = all(
            getattr(target, attribute) is original for target, attribute, original in self.originals
        )
        self.installed = False

    def __enter__(self) -> PrivateProbe:
        self.install()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object | None,  # noqa: OBJECT_OK
    ) -> Literal[False]:
        del exception_type, exception, traceback
        try:
            return False
        finally:
            self.restore()


def _passthrough_wrapper(original: Callable[..., object]) -> Callable[..., object]:  # noqa: OBJECT_OK
    """Return an observation-free wrapper; hooks never perform I/O or awaits."""

    def wrapper(*args: object, **kwargs: object) -> object:  # noqa: OBJECT_OK
        return original(*args, **kwargs)

    return wrapper


@dataclass(frozen=True, slots=True)
class ServerSample:
    diagnostic_id: str
    asgi_entry_ns: int
    response_send_complete_ns: int
    server_total_ns: int
    post_send_ns: int
    status_code: int
    response_bytes: bytes
    schema_compatible: bool
    refusal_reason: str | None


@dataclass(frozen=True, slots=True)
class RequestAttempt:
    """Bounded client result retained until it can be joined by diagnostic ID."""

    diagnostic_id: str
    request_start_ns: int
    request_built_ns: int
    client_complete_ns: int
    trace_events: tuple[TraceEvent, ...]
    status_code: int | None
    response_bytes: bytes | None
    http_version: str | None
    error: str | None
    clock_metadata: ClockMetadata | None = None


@dataclass(frozen=True, slots=True)
class PrivateLandmarks:
    diagnostic_id: str
    task_ready_ns: int | None
    limiter_acquired_ns: int | None
    client_request_start_ns: int | None
    request_built_ns: int | None
    pool_request_created_ns: int | None
    assignment_ns: int | None
    pool_wait_return_ns: int | None
    connection_dispatch_enter_ns: int | None
    first_trace_ns: int | None
    protocol_entry_ns: int | None
    asgi_entry_ns: int | None
    client_complete_ns: int | None
    response_send_complete_ns: int | None
    trace_events: tuple[TraceEvent, ...]
    clock_metadata: ClockMetadata
    fresh_connection: bool
    non_additive_overlap_ns: int
    operation_ledger: OperationLedger = field(default_factory=OperationLedger)


@dataclass(frozen=True, slots=True)
class DiagnosticRecord:
    diagnostic_id: str
    task_ready_ns: int | None
    limiter_acquired_ns: int | None
    client_request_start_ns: int | None
    request_built_ns: int | None
    pool_request_created_ns: int | None
    assignment_ns: int | None
    pool_wait_return_ns: int | None
    connection_dispatch_enter_ns: int | None
    first_trace_ns: int | None
    protocol_entry_ns: int | None
    asgi_entry_ns: int | None
    client_complete_ns: int | None
    response_send_complete_ns: int | None
    trace_events: tuple[TraceEvent, ...]
    clock_metadata: ClockMetadata
    fresh_connection: bool
    non_additive_overlap_ns: int
    operation_ledger: OperationLedger
    server_sample: ServerSample
    error: str | None = None


def build_record(landmarks: PrivateLandmarks, sample: ServerSample) -> DiagnosticRecord:
    """Copy accepted server data into one bounded per-ID record."""
    return DiagnosticRecord(
        landmarks.diagnostic_id,
        landmarks.task_ready_ns,
        landmarks.limiter_acquired_ns,
        landmarks.client_request_start_ns,
        landmarks.request_built_ns,
        landmarks.pool_request_created_ns,
        landmarks.assignment_ns,
        landmarks.pool_wait_return_ns,
        landmarks.connection_dispatch_enter_ns,
        landmarks.first_trace_ns,
        landmarks.protocol_entry_ns,
        landmarks.asgi_entry_ns,
        landmarks.client_complete_ns,
        landmarks.response_send_complete_ns,
        landmarks.trace_events,
        landmarks.clock_metadata,
        landmarks.fresh_connection,
        landmarks.non_additive_overlap_ns,
        landmarks.operation_ledger,
        sample,
    )


def validate_landmarks(record: DiagnosticRecord) -> str | None:
    """Validate integer types, ordering, trace shape, and exact operation counts."""
    values = (
        record.task_ready_ns,
        record.limiter_acquired_ns,
        record.client_request_start_ns,
        record.request_built_ns,
        record.pool_request_created_ns,
        record.assignment_ns,
        record.pool_wait_return_ns,
        record.connection_dispatch_enter_ns,
        record.first_trace_ns,
        record.protocol_entry_ns,
        record.asgi_entry_ns,
        record.response_send_complete_ns,
        record.client_complete_ns,
    )
    if any(value is None or type(value) is not int for value in values):
        return "landmark:timestamp_missing_or_not_integer"
    if any(value < 0 for value in values if value is not None):
        return "landmark:timestamp_negative"
    if type(record.non_additive_overlap_ns) is not int or record.non_additive_overlap_ns < 0:
        return "landmark:overlap_invalid"
    if type(record.fresh_connection) is not bool:
        return "landmark:connection_class_invalid"
    timestamps = tuple(value for value in values[:9] + values[10:] if value is not None)
    if any(left > right for left, right in zip(timestamps, timestamps[1:], strict=False)):
        return "landmark:timestamp_out_of_order"
    parsed, trace_error = validate_trace(record.trace_events)
    if trace_error is not None or parsed is None:
        return f"landmark:{trace_error or 'trace_missing'}"
    if parsed.fresh != record.fresh_connection or parsed.first_event_ns != record.first_trace_ns:
        return "landmark:trace_join_invalid"
    assert record.protocol_entry_ns is not None
    assert record.connection_dispatch_enter_ns is not None
    assert record.assignment_ns is not None
    assert record.pool_wait_return_ns is not None
    assert record.first_trace_ns is not None
    assert record.asgi_entry_ns is not None
    if record.assignment_ns > record.pool_wait_return_ns:
        return "landmark:private_timestamp_order_invalid"
    first_http11_event = parsed.events[2] if parsed.fresh else parsed.events[0]
    if parsed.fresh:
        valid_protocol_order = (
            record.first_trace_ns
            <= record.protocol_entry_ns
            <= first_http11_event.timestamp_ns
            <= record.asgi_entry_ns
        )
    else:
        valid_protocol_order = (
            record.connection_dispatch_enter_ns
            <= record.protocol_entry_ns
            <= record.first_trace_ns
            <= record.asgi_entry_ns
        )
    if not valid_protocol_order:
        return "landmark:protocol_entry_order_invalid"
    if record.request_built_ns is None or record.client_complete_ns is None:
        return "landmark:timestamp_missing_or_not_integer"
    if (
        parsed.events[0].timestamp_ns < record.request_built_ns
        or parsed.events[-1].timestamp_ns > record.client_complete_ns
    ):
        return "landmark:trace_out_of_bounds"
    sample = record.server_sample
    if (
        sample.diagnostic_id != record.diagnostic_id
        or sample.asgi_entry_ns != record.asgi_entry_ns
        or sample.response_send_complete_ns != record.response_send_complete_ns
        or type(sample.asgi_entry_ns) is not int
        or type(sample.response_send_complete_ns) is not int
        or type(sample.server_total_ns) is not int
        or type(sample.post_send_ns) is not int
        or type(sample.status_code) is not int
        or type(sample.response_bytes) is not bytes
        or type(sample.schema_compatible) is not bool
        or sample.status_code < 100
        or sample.status_code > 599
        or (sample.refusal_reason is not None and type(sample.refusal_reason) is not str)
        or any(
            value < 0
            for value in (
                sample.asgi_entry_ns,
                sample.response_send_complete_ns,
                sample.server_total_ns,
                sample.post_send_ns,
            )
        )
        or not sample.schema_compatible
        or sample.server_total_ns != sample.response_send_complete_ns - sample.asgi_entry_ns
    ):
        return "landmark:server_sample_invalid"
    if sample.response_send_complete_ns < sample.asgi_entry_ns:
        return "landmark:server_timestamp_order_invalid"
    if type(record.operation_ledger) is not OperationLedger:
        return "landmark:operation_count_invalid"
    ledger_error = validate_operation_ledger(record.operation_ledger)
    if ledger_error is not None:
        return ledger_error
    return None


@dataclass(frozen=True, slots=True)
class ClientIntervals:
    task_ready_ns: int
    client_complete_ns: int
    limiter_wait_ns: int
    external_wall_ns: int


def derive_client_intervals(record: DiagnosticRecord) -> ClientIntervals:
    """Derive limiter wait and accepted external wall without mixing endpoints."""
    assert record.task_ready_ns is not None and record.limiter_acquired_ns is not None
    assert record.client_request_start_ns is not None and record.client_complete_ns is not None
    return ClientIntervals(
        record.task_ready_ns,
        record.client_complete_ns,
        record.limiter_acquired_ns - record.task_ready_ns,
        record.client_complete_ns - record.client_request_start_ns,
    )


@dataclass(frozen=True, slots=True)
class BucketValues:
    client_pre_pool_ns: int
    pool_request_to_wait_return_ns: int
    connection_dispatch_to_first_trace_ns: int
    first_trace_to_asgi_ns: int
    accepted_pre_asgi_ns: int
    assignment_ns: int
    overlap_ns: int
    overlap_counted: bool = False

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (
            self.client_pre_pool_ns,
            self.pool_request_to_wait_return_ns,
            self.connection_dispatch_to_first_trace_ns,
            self.first_trace_to_asgi_ns,
        )


@dataclass(frozen=True, slots=True)
class BucketResult:
    values: BucketValues | None
    error: str | None


def compute_buckets(record: DiagnosticRecord) -> BucketResult:
    """Compute only the four approved additive integer bucket intervals."""
    landmark_error = validate_landmarks(record)
    if landmark_error is not None:
        return BucketResult(None, f"bucket:{landmark_error}")
    assert record.client_request_start_ns is not None
    assert record.pool_request_created_ns is not None
    assert record.assignment_ns is not None and record.pool_wait_return_ns is not None
    assert record.first_trace_ns is not None and record.asgi_entry_ns is not None
    values = BucketValues(
        record.pool_request_created_ns - record.client_request_start_ns,
        record.pool_wait_return_ns - record.pool_request_created_ns,
        record.first_trace_ns - record.pool_wait_return_ns,
        record.asgi_entry_ns - record.first_trace_ns,
        record.asgi_entry_ns - record.client_request_start_ns,
        record.assignment_ns,
        record.non_additive_overlap_ns,
    )
    if (
        any(value < 0 for value in values.as_tuple())
        or sum(values.as_tuple()) != values.accepted_pre_asgi_ns
    ):
        return BucketResult(None, "bucket:arithmetic_invalid")
    return BucketResult(values, None)


@dataclass(frozen=True, slots=True)
class IdentityResult:
    accepted_pre_asgi_ns: int | None
    external_wall_ns: int | None
    error: str | None


def validate_identities(record: DiagnosticRecord) -> IdentityResult:
    """Validate the unchanged P10-AU external identity and additive pre-ASGI identity."""
    buckets = compute_buckets(record)
    if buckets.error is not None or buckets.values is None:
        return IdentityResult(None, None, buckets.error)
    intervals = derive_client_intervals(record)
    sample = record.server_sample
    if sample.diagnostic_id != record.diagnostic_id:
        return IdentityResult(None, None, "identity:diagnostic_id_mismatch")
    if any(
        type(value) is not int or value < 0
        for value in (sample.server_total_ns, sample.post_send_ns)
    ):
        return IdentityResult(None, None, "identity:server_value_invalid")
    expected = buckets.values.accepted_pre_asgi_ns + sample.server_total_ns + sample.post_send_ns
    if expected != intervals.external_wall_ns:
        return IdentityResult(None, None, "identity:external_wall_mismatch")
    if sample.asgi_entry_ns != record.asgi_entry_ns:
        return IdentityResult(None, None, "identity:asgi_join_mismatch")
    return IdentityResult(buckets.values.accepted_pre_asgi_ns, intervals.external_wall_ns, None)


@dataclass(frozen=True, slots=True)
class JoinResult:
    records: tuple[DiagnosticRecord, ...]
    error: str | None


def join_by_id(records: Sequence[DiagnosticRecord], samples: Sequence[ServerSample]) -> JoinResult:
    """Join reversed or interleaved server samples only by exact diagnostic ID."""
    record_ids = tuple(record.diagnostic_id for record in records)
    sample_ids = tuple(sample.diagnostic_id for sample in samples)
    if any(
        type(identifier) is not str or not identifier for identifier in (*record_ids, *sample_ids)
    ):
        return JoinResult((), "join:invalid_id")
    if len(set(record_ids)) != len(record_ids) or len(set(sample_ids)) != len(sample_ids):
        return JoinResult((), "join:duplicate_id")
    if set(record_ids) != set(sample_ids):
        return JoinResult((), "join:missing_or_unrelated_id")
    samples_by_id = {sample.diagnostic_id: sample for sample in samples}
    return JoinResult(
        tuple(
            replace(record, server_sample=samples_by_id[record.diagnostic_id]) for record in records
        ),
        None,
    )


@dataclass(frozen=True, slots=True)
class FailedRecord:
    diagnostic_id: str
    error: str
    restored: bool


def failed_record(identifier: str, error: str, *, restored: bool) -> FailedRecord:
    """Retain an explicit failed request outcome instead of inventing timestamps."""
    return FailedRecord(identifier, error, restored)


def validate_failed_record(record: FailedRecord) -> str | None:
    if (
        type(record) is not FailedRecord
        or type(record.diagnostic_id) is not str
        or not record.diagnostic_id
        or type(record.error) is not str
        or not record.error
        or type(record.restored) is not bool
    ):
        return "failure:record_invalid"
    return None if record.restored else "failure:cleanup_unproven"


@dataclass(frozen=True, slots=True)
class ReleasePlan:
    mode: TraceMode
    indexes: tuple[int, ...]
    diagnostic_ids: tuple[str, ...]
    workers: int
    persistent: bool
    limiter_capacity: int
    release_batches: tuple[tuple[str, ...], ...]
    query: str = QUERY
    cutoff: str = CUTOFF
    max_items: int = 10
    require_evidence: bool = True
    worker_assignments: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class ReleasePlanError(Exception):
    """Describe a release-shape rejection without sending any request."""

    code: str

    def __str__(self) -> str:
        return self.code


def build_release_plan(mode: TraceMode, diagnostic_ids: tuple[str, ...]) -> ReleasePlan:
    """Build the exact burst or sixteen-worker fixed release shape."""
    if mode not in ("burst", "fixed"):
        raise ReleasePlanError("matrix:mode_invalid")
    if len(diagnostic_ids) != MATRIX_SIZE or any(
        type(identifier) is not str or not identifier for identifier in diagnostic_ids
    ):
        raise ReleasePlanError("matrix:diagnostic_ids_invalid")
    if len(set(diagnostic_ids)) != MATRIX_SIZE:
        raise ReleasePlanError("matrix:diagnostic_ids_not_unique")
    indexes = tuple(range(MATRIX_SIZE))
    match mode:
        case "burst":
            return ReleasePlan(
                mode,
                indexes,
                diagnostic_ids,
                0,
                False,
                LIMITER_CAPACITY,
                (diagnostic_ids,),
            )
        case "fixed":
            assignments = tuple(
                diagnostic_ids[offset::LIMITER_CAPACITY] for offset in range(LIMITER_CAPACITY)
            )
            return ReleasePlan(
                mode,
                indexes,
                diagnostic_ids,
                LIMITER_CAPACITY,
                True,
                LIMITER_CAPACITY,
                (),
                worker_assignments=assignments,
            )
        case unreachable:
            assert_never(unreachable)


def validate_release_plan(plan: ReleasePlan) -> str | None:
    """Validate one deterministic release plan before any task is started."""
    if type(plan) is not ReleasePlan:
        return "matrix:release_plan_invalid"
    if (
        len(plan.indexes) != MATRIX_SIZE
        or plan.indexes != tuple(range(MATRIX_SIZE))
        or len(plan.diagnostic_ids) != MATRIX_SIZE
        or any(type(identifier) is not str or not identifier for identifier in plan.diagnostic_ids)
        or len(set(plan.diagnostic_ids)) != MATRIX_SIZE
        or plan.limiter_capacity != LIMITER_CAPACITY
        or plan.query != QUERY
        or plan.cutoff != CUTOFF
        or plan.max_items != 10
        or plan.require_evidence is not True
    ):
        return "matrix:release_contract_invalid"
    if plan.mode == "burst":
        return (
            None
            if plan.workers == 0
            and plan.persistent is False
            and plan.release_batches == (plan.diagnostic_ids,)
            and plan.worker_assignments == ()
            else "matrix:burst_release_invalid"
        )
    if plan.mode == "fixed":
        assigned = tuple(identifier for group in plan.worker_assignments for identifier in group)
        return (
            None
            if plan.workers == LIMITER_CAPACITY
            and plan.persistent is True
            and plan.release_batches == ()
            and len(plan.worker_assignments) == LIMITER_CAPACITY
            and len(assigned) == MATRIX_SIZE
            and len(set(assigned)) == MATRIX_SIZE
            and set(assigned) == set(plan.diagnostic_ids)
            else "matrix:fixed_release_invalid"
        )
    return "matrix:mode_invalid"


def capacity_limiter() -> anyio.CapacityLimiter:
    """Create the one bounded AnyIO limiter shape used by every case."""
    return anyio.CapacityLimiter(LIMITER_CAPACITY)


def compare_pair_contracts(burst: ReleasePlan, fixed: ReleasePlan) -> str | None:
    """Ensure paired rows differ only in the planner release mode."""
    if burst.mode != "burst" or fixed.mode != "fixed":
        return "matrix:pair_mode_invalid"
    if burst.diagnostic_ids != fixed.diagnostic_ids or burst.indexes != fixed.indexes:
        return "matrix:pair_ids_changed"
    if (
        burst.query,
        burst.cutoff,
        burst.max_items,
        burst.require_evidence,
        burst.limiter_capacity,
    ) != (
        fixed.query,
        fixed.cutoff,
        fixed.max_items,
        fixed.require_evidence,
        fixed.limiter_capacity,
    ):
        return "matrix:pair_request_changed"
    return None


@dataclass(frozen=True, slots=True)
class RequestContract:
    placement: Placement
    base_url: str
    image: str = "esragpro-p10av"
    corpus: str = "p10b-rehearsal"
    query: str = QUERY
    cutoff: str = CUTOFF
    max_items: int = 10
    request_count: int = MATRIX_SIZE
    concurrency: int = LIMITER_CAPACITY
    worker_count: int = 1
    schema: str = "accepted-p10au"
    http2: bool = False
    trust_env: bool = False
    follow_redirects: bool = False
    mounts: bool = False
    proxy: bool = False
    warmup_count: int = 1
    limiter_capacity: int = LIMITER_CAPACITY
    no_cursor: bool = True


def request_contract(placement: Placement) -> RequestContract:
    """Return the shared sidecar/loopback contract with only endpoint placement varying."""
    if placement not in ("sidecar", "loopback"):
        raise RequestContractError("request:placement_invalid")
    base_url = "http://p10av-sidecar" if placement == "sidecar" else "http://127.0.0.1:18092"
    return RequestContract(placement, base_url)


def validate_request_contract(contract: RequestContract) -> str | None:
    """Reject any request contract that changes the locked matrix inputs."""
    if type(contract) is not RequestContract:
        return "request:contract_invalid"
    if any(
        type(value) is not bool
        for value in (
            contract.http2,
            contract.trust_env,
            contract.follow_redirects,
            contract.mounts,
            contract.proxy,
            contract.no_cursor,
        )
    ) or any(
        type(value) is not int
        for value in (
            contract.max_items,
            contract.request_count,
            contract.concurrency,
            contract.worker_count,
            contract.warmup_count,
            contract.limiter_capacity,
        )
    ):
        return "request:contract_types_invalid"
    try:
        expected = request_contract(contract.placement)
    except RequestContractError:
        return "request:placement_invalid"
    return None if contract == expected else "request:contract_changed"


@dataclass(frozen=True, slots=True)
class MatrixCase:
    case_id: str
    placement: Placement
    mode: TraceMode
    repeat: int
    pair: str
    order: int
    warmup_count: int = 1
    measured_count: int = MATRIX_SIZE


def matrix_cases() -> tuple[MatrixCase, ...]:
    """Return the exact counterbalanced twelve-row P10-AV matrix."""
    rows: tuple[tuple[str, Placement, TraceMode, int], ...] = (
        ("sidecar-burst-r1", "sidecar", "burst", 1),
        ("sidecar-fixed-r1", "sidecar", "fixed", 1),
        ("sidecar-fixed-r2", "sidecar", "fixed", 2),
        ("sidecar-burst-r2", "sidecar", "burst", 2),
        ("sidecar-burst-r3", "sidecar", "burst", 3),
        ("sidecar-fixed-r3", "sidecar", "fixed", 3),
        ("loopback-fixed-r1", "loopback", "fixed", 1),
        ("loopback-burst-r1", "loopback", "burst", 1),
        ("loopback-burst-r2", "loopback", "burst", 2),
        ("loopback-fixed-r2", "loopback", "fixed", 2),
        ("loopback-fixed-r3", "loopback", "fixed", 3),
        ("loopback-burst-r3", "loopback", "burst", 3),
    )
    return tuple(
        MatrixCase(case_id, placement, mode, repeat, f"{placement}-{repeat}", index + 1)
        for index, (case_id, placement, mode, repeat) in enumerate(rows)
    )


def warmup_id(case: MatrixCase) -> str:
    """Return the one excluded, case-specific warmup ID."""
    return f"p10av-{case.case_id}-warmup"


def measured_ids(case: MatrixCase) -> tuple[str, ...]:
    """Return the exact indexed IDs for one case, independent of completion order."""
    return tuple(f"p10av-{case.case_id}-m{index:03d}" for index in range(MATRIX_SIZE))


@dataclass(frozen=True, slots=True)
class RetentionRow:
    case_index: int
    before: int
    after: int
    delta: int


@dataclass(frozen=True, slots=True)
class RetentionReport:
    rows: tuple[RetentionRow, ...]
    sequence: tuple[int, ...]
    measured_total: int
    warmup_total: int
    final: int
    no_cursor: bool
    snapshots_present: bool = True
    valid: bool = True
    error: str | None = None


def retention_accounting(case_indexes: Sequence[int]) -> RetentionReport:
    """Derive the exact one-warmup plus one-hundred-measured retention sequence."""
    indexes = tuple(case_indexes)
    exact_matrix = indexes == tuple(range(len(matrix_cases())))
    current = 0
    rows: list[RetentionRow] = []
    sequence = [current]
    for index in indexes:
        row = RetentionRow(index, current, current + RETENTION_DELTA, RETENTION_DELTA)
        rows.append(row)
        current = row.after
        sequence.append(current)
    return RetentionReport(
        tuple(rows),
        tuple(sequence),
        len(rows) * MATRIX_SIZE,
        len(rows),
        current,
        True,
        True,
        exact_matrix and current == len(matrix_cases()) * RETENTION_DELTA,
        None
        if exact_matrix and current == len(matrix_cases()) * RETENTION_DELTA
        else "retention:exact_twelve_case_sequence_required",
    )


def validate_retention(report: RetentionReport) -> str | None:
    """Require the exact 12-case snapshots, deltas, and 1212 final count."""
    if type(report) is not RetentionReport:
        return "retention:report_invalid"
    if (
        type(report.no_cursor) is not bool
        or type(report.snapshots_present) is not bool
        or type(report.valid) is not bool
        or type(report.rows) is not tuple
        or type(report.sequence) is not tuple
    ):
        return "retention:report_invalid"
    if report.error is not None or not report.valid:
        return report.error or "retention:invalid"
    expected_rows = tuple(
        RetentionRow(index, index * RETENTION_DELTA, (index + 1) * RETENTION_DELTA, RETENTION_DELTA)
        for index in range(len(matrix_cases()))
    )
    if report.rows != expected_rows or not report.snapshots_present:
        return "retention:snapshot_or_row_invalid"
    if report.sequence != tuple(index * RETENTION_DELTA for index in range(13)):
        return "retention:sequence_invalid"
    if report.measured_total != 1200 or report.warmup_total != 12:
        return "retention:aggregate_invalid"
    if report.final != 1212 or report.final >= 2048 or not report.no_cursor:
        return "retention:final_invalid"
    return None


@dataclass(frozen=True, slots=True)
class TailRecord:
    diagnostic_id: str
    accepted_pre_asgi_ns: int
    buckets: tuple[int, int, int, int]
    arithmetic_valid: bool = True


@dataclass(frozen=True, slots=True)
class TailSelection:
    selected: tuple[TailRecord, ...]
    winner: str | None
    share_pass: bool
    eligible: bool = False
    winner_counts: tuple[int, int, int, int] = (0, 0, 0, 0)


def _winner(record: TailRecord) -> int:
    return max(range(4), key=lambda index: (record.buckets[index], -index))


def select_tail(records: Sequence[TailRecord]) -> TailSelection:
    """Select five positive tails and use fixed-priority exact rational shares."""
    positive = sorted(
        (
            record
            for record in records
            if type(record) is TailRecord
            and type(record.diagnostic_id) is str
            and bool(record.diagnostic_id)
            and type(record.arithmetic_valid) is bool
            and record.arithmetic_valid
            and type(record.accepted_pre_asgi_ns) is int
            and record.accepted_pre_asgi_ns > 0
            and len(record.buckets) == 4
            and all(type(value) is int and value >= 0 for value in record.buckets)
        ),
        key=lambda record: (-record.accepted_pre_asgi_ns, record.diagnostic_id.encode("utf-8")),
    )[:5]
    if len(positive) < 5:
        return TailSelection(tuple(positive), None, False, False)
    winners = tuple(_winner(record) for record in positive)
    counts = (
        winners.count(0),
        winners.count(1),
        winners.count(2),
        winners.count(3),
    )
    candidate = max(range(4), key=lambda index: (counts[index], -index))
    shares = sorted(
        (Fraction(record.buckets[candidate], record.accepted_pre_asgi_ns) for record in positive)
    )
    median = shares[2]
    median_num, median_den = median.numerator, median.denominator
    passed = counts[candidate] >= 4 and 5 * median_num >= 3 * median_den
    return TailSelection(tuple(positive), BUCKET_NAMES[candidate], passed, True, counts)


@dataclass(frozen=True, slots=True)
class MetricValue:
    diagnostic_id: str
    value: int


@dataclass(frozen=True, slots=True)
class MetricPopulationError(Exception):
    """Describe a percentile population that cannot support a case verdict."""

    code: str

    def __str__(self) -> str:
        return self.code


def nearest_rank(values: Sequence[MetricValue], quantile: float) -> int:
    """Return the exact nearest-rank integer percentile with deterministic ties."""
    if (
        not values
        or type(quantile) not in (int, float)
        or isinstance(quantile, bool)
        or not math.isfinite(quantile)
        or not 0 < quantile <= 1
    ):
        raise MetricPopulationError("metric:nearest_rank_input_invalid")
    if any(
        type(item) is not MetricValue
        or type(item.diagnostic_id) is not str
        or not item.diagnostic_id
        or type(item.value) is not int
        or item.value < 0
        for item in values
    ):
        raise MetricPopulationError("metric:value_invalid")
    ordered = sorted(values, key=lambda item: (item.value, item.diagnostic_id.encode("utf-8")))
    return ordered[math.ceil(quantile * len(ordered)) - 1].value


def phase_promotion(
    tails: Sequence[TailSelection],
    *,
    placement: Placement,
    modes: Sequence[TraceMode] | None = None,
) -> str | None:
    """Promote only a repeated burst tail, never a single or fixed observation."""
    del placement
    if modes is not None and (
        len(tails) != 3 or len(modes) != len(tails) or any(mode != "burst" for mode in modes)
    ):
        return None
    winners = tuple(
        tail.winner
        for tail in tails
        if tail.eligible and tail.share_pass and tail.winner is not None
    )
    return winners[0] if len(winners) >= 2 and len(set(winners)) == 1 else None


def phase_promotion_from_cases(
    cases: Sequence[MatrixCase], tails: Sequence[TailSelection]
) -> str | None:
    """Apply the three-repeat burst-only promotion rule for one placement."""
    if len(cases) != 3 or len(tails) != 3:
        return None
    if any(case.mode != "burst" for case in cases):
        return None
    if {case.repeat for case in cases} != {1, 2, 3}:
        return None
    if len({case.placement for case in cases}) != 1:
        return None
    return phase_promotion(tails, placement=cases[0].placement, modes=("burst",) * 3)


@dataclass(frozen=True, slots=True)
class CaseMetrics:
    """Raw paired metric populations; no percentile is stored as a float."""

    client_pre_pool: tuple[MetricValue, ...]
    pre_asgi: tuple[MetricValue, ...]
    pool_wait: tuple[MetricValue, ...]
    dispatch: tuple[MetricValue, ...]
    first_trace_to_asgi: tuple[MetricValue, ...]
    post_send: tuple[MetricValue, ...]


@dataclass(frozen=True, slots=True)
class H1Result:
    client_pre_pool_reduction: bool
    pre_asgi_reduction: bool
    downstream_migration_ns: int
    accepted: bool


def h1_repeats_accepted(results: Sequence[H1Result]) -> bool:
    """Require at least two successful same-repeat pairs out of three."""
    return len(results) == 3 and sum(result.accepted for result in results) >= 2


def _migration(burst: Sequence[MetricValue], fixed: Sequence[MetricValue]) -> int:
    return max(0, nearest_rank(fixed, 0.95) - nearest_rank(burst, 0.95))


def evaluate_h1_pair(burst: CaseMetrics, fixed: CaseMetrics) -> H1Result:
    """Apply exact A/B P95 reductions and downstream migration predicates."""
    burst_client = nearest_rank(burst.client_pre_pool, 0.95)
    fixed_client = nearest_rank(fixed.client_pre_pool, 0.95)
    burst_pre = nearest_rank(burst.pre_asgi, 0.95)
    fixed_pre = nearest_rank(fixed.pre_asgi, 0.95)
    client_reduction = burst_client > 0 and 2 * fixed_client <= burst_client
    pre_reduction = burst_pre > 0 and 10 * fixed_pre <= 7 * burst_pre
    downstream = max(
        _migration(burst.pool_wait, fixed.pool_wait),
        _migration(burst.dispatch, fixed.dispatch),
        _migration(burst.first_trace_to_asgi, fixed.first_trace_to_asgi),
        _migration(burst.post_send, fixed.post_send),
    )
    return H1Result(
        client_reduction,
        pre_reduction,
        downstream,
        client_reduction and pre_reduction and downstream == 0,
    )


@dataclass(frozen=True, slots=True)
class CaseEvidence:
    measured_count: int
    response_count: int
    sample_count: int
    errors: int
    in_flight_after: int
    schema_compatible: bool = True
    arithmetic_valid: bool = True
    retention_delta: int = RETENTION_DELTA
    exact_ids: bool = True
    complete_sample_count: int = MATRIX_SIZE
    valid_client_record_count: int = MATRIX_SIZE
    warmup_count: int = 1
    snapshot_before_present: bool = True
    snapshot_after_present: bool = True
    no_cursor: bool = True
    retained_before: int = 0
    retained_after: int = RETENTION_DELTA
    measured_ids: tuple[str, ...] = ()
    warmup_id: str | None = None


def validate_case(evidence: CaseEvidence) -> str | None:
    """Require every measured response, sample, identity, and cleanup counter."""
    if type(evidence) is not CaseEvidence:
        return "case:evidence_invalid"
    counts = (
        evidence.measured_count,
        evidence.response_count,
        evidence.sample_count,
        evidence.errors,
        evidence.in_flight_after,
        evidence.retention_delta,
        evidence.complete_sample_count,
        evidence.valid_client_record_count,
        evidence.warmup_count,
        evidence.retained_before,
        evidence.retained_after,
    )
    if any(type(value) is not int or value < 0 for value in counts):
        return "case:counts_invalid"
    if any(
        type(value) is not bool
        for value in (
            evidence.schema_compatible,
            evidence.arithmetic_valid,
            evidence.exact_ids,
            evidence.snapshot_before_present,
            evidence.snapshot_after_present,
            evidence.no_cursor,
        )
    ):
        return "case:flags_invalid"
    if evidence.measured_count != MATRIX_SIZE or evidence.response_count != MATRIX_SIZE:
        return "case:response_count_invalid"
    if evidence.sample_count != MATRIX_SIZE:
        return "case:sample_count_invalid"
    if (
        evidence.complete_sample_count != MATRIX_SIZE
        or evidence.valid_client_record_count != MATRIX_SIZE
        or evidence.warmup_count != 1
    ):
        return "case:complete_record_count_invalid"
    if evidence.errors != 0 or evidence.in_flight_after != 0:
        return "case:errors_or_in_flight"
    if (
        not evidence.schema_compatible
        or not evidence.arithmetic_valid
        or evidence.retention_delta != RETENTION_DELTA
        or not evidence.snapshot_before_present
        or not evidence.snapshot_after_present
        or not evidence.no_cursor
        or evidence.retained_after != evidence.retained_before + RETENTION_DELTA
    ):
        return "case:validation_failed"
    if evidence.warmup_id is not None and (
        type(evidence.warmup_id) is not str or not evidence.warmup_id
    ):
        return "case:ids_invalid"
    if evidence.measured_ids:
        if (
            type(evidence.measured_ids) is not tuple
            or len(evidence.measured_ids) != MATRIX_SIZE
            or any(
                type(identifier) is not str or not identifier
                for identifier in evidence.measured_ids
            )
            or len(set(evidence.measured_ids)) != MATRIX_SIZE
            or (evidence.warmup_id is not None and evidence.warmup_id in evidence.measured_ids)
        ):
            return "case:ids_invalid"
    return None if evidence.exact_ids else "case:ids_invalid"


class PublicResponse(TypedDict):
    bytes: bytes
    fields: tuple[str, ...]
    openapi: str
    refusal: str | None
    error: str


@dataclass(frozen=True, slots=True)
class PublicProjection:
    bytes: bytes
    fields: tuple[str, ...]
    openapi: str
    refusal: str | None
    error: str


def public_projection(response: PublicResponse, *, probe: bool) -> PublicProjection:
    """Expose the unchanged public response projection; probe state is client-local."""
    del probe
    return PublicProjection(
        response["bytes"],
        response["fields"],
        response["openapi"],
        response["refusal"],
        response["error"],
    )


@dataclass(frozen=True, slots=True)
class NativeCapture:
    stdout: str
    stderr: str
    stdout_present: bool
    stderr_present: bool
    exit_code: int


def capture_native(stdout: str | None, stderr: str | None, exit_code: int) -> NativeCapture:
    """Preserve absent, blank, and nonzero native process streams distinctly."""
    return NativeCapture(
        stdout or "", stderr or "", stdout is not None, stderr is not None, exit_code
    )


@dataclass(frozen=True, slots=True)
class CleanupProof:
    owned_resources_absent: bool
    port_released: bool
    probe_restored: bool


def validate_cleanup(proof: CleanupProof) -> str | None:
    if type(proof) is not CleanupProof:
        return "cleanup:proof_invalid"
    if not all(
        type(value) is bool
        for value in (
            proof.owned_resources_absent,
            proof.port_released,
            proof.probe_restored,
        )
    ):
        return "cleanup:proof_invalid"
    return (
        None
        if proof.owned_resources_absent and proof.port_released and proof.probe_restored
        else "cleanup:unproven"
    )


@dataclass(frozen=True, slots=True)
class CliResult:
    matrix_cases: int
    cleanup: bool
    failed_gates: int
    matrix_done: bool = True
    cleanup_done: bool = True


def cli_exit_code(result: CliResult) -> int:
    return (
        0
        if type(result) is CliResult
        and type(result.matrix_cases) is int
        and result.matrix_cases == len(matrix_cases())
        and type(result.failed_gates) is int
        and result.failed_gates >= 0
        and type(result.cleanup) is bool
        and type(result.matrix_done) is bool
        and type(result.cleanup_done) is bool
        and result.cleanup
        and result.failed_gates == 0
        and result.matrix_done
        and result.cleanup_done
        else 1
    )


def render_cli_result(result: CliResult) -> str:
    """Render compact machine JSON followed by one deterministic completion marker."""
    passed = cli_exit_code(result) == 0
    payload = json.dumps(
        {
            "cleanup": result.cleanup,
            "cleanup_done": result.cleanup_done,
            "failed_gates": result.failed_gates,
            "matrix_cases": f"{result.matrix_cases}/12",
            "matrix_done": result.matrix_done,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    marker = f"P10AV_READY15_FINAL status={'passed' if passed else 'blocked'} blocker={'none' if passed else 'gate'} matrix_cases={result.matrix_cases}/12 cleanup={result.cleanup}"
    return f"{payload}\n{marker}"


def render_lifecycle_markers(result: CliResult) -> tuple[str, ...]:
    """Emit lifecycle markers only after their corresponding gate is proven."""
    markers: list[str] = []
    if (
        result.matrix_done
        and result.matrix_cases == len(matrix_cases())
        and result.failed_gates == 0
    ):
        markers.append("P10AV_READY15_MATRIX_DONE")
    if result.cleanup_done and result.cleanup:
        markers.append("P10AV_READY15_CLEANUP_DONE")
    return tuple(markers)


def render_cli_execution(result: CliResult) -> str:
    """Render the machine payload, ordered lifecycle markers, and final marker."""
    payload, final_marker = render_cli_result(result).splitlines()
    return "\n".join((payload, *render_lifecycle_markers(result), final_marker))


def source_sha256(path: Path) -> str:
    """Hash source bytes for before/after launcher continuity."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_sha256(value: str | None) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    operating_system: str
    implementation: str
    version_info: tuple[int, int, int, str, int]
    sys_version: str
    executable: str
    machine: str
    libc: tuple[str, str]
    build: tuple[str, str]
    cache_tag: str | None
    image_digest: str | None
    httpx_version: str
    httpcore_version: str
    private_manifest: PrivateManifest | None = None
    source_sha256: str | None = None


def runtime_manifest(*, image_digest: str | None = None) -> RuntimeManifest:
    """Capture runtime identity; no host value substitutes for an image digest."""
    return RuntimeManifest(
        operating_system=platform.system(),
        implementation=platform.python_implementation(),
        version_info=(
            int(sys.version_info[0]),
            int(sys.version_info[1]),
            int(sys.version_info[2]),
            str(sys.version_info[3]),
            int(sys.version_info[4]),
        ),
        sys_version=sys.version,
        executable=sys.executable,
        machine=platform.machine(),
        libc=platform.libc_ver(),
        build=platform.python_build(),
        cache_tag=sys.implementation.cache_tag,
        image_digest=image_digest,
        httpx_version=httpx.__version__,
        httpcore_version=httpcore.__version__,
        private_manifest=build_private_manifest(),
        source_sha256=source_sha256(Path(__file__)),
    )


def validate_runtime_manifest(manifest: RuntimeManifest) -> str | None:
    """Require accepted CPython 3.12 final Linux identity and dependencies."""
    if type(manifest) is not RuntimeManifest:
        return "runtime:manifest_incomplete"
    if (
        manifest.operating_system != "Linux"
        or manifest.implementation != "CPython"
        or type(manifest.version_info) is not tuple
        or len(manifest.version_info) != 5
        or manifest.version_info[:2] != (3, 12)
        or manifest.version_info[3] != "final"
    ):
        return "runtime:unsupported_python"
    if (
        not (
            type(manifest.image_digest) is str
            and manifest.image_digest.startswith("sha256:")
            and _is_sha256(manifest.image_digest[7:])
        )
        or manifest.httpx_version != HTTPX_VERSION
        or manifest.httpcore_version != HTTPCORE_VERSION
        or not manifest.sys_version
        or not manifest.executable
        or not manifest.machine
        or not manifest.cache_tag
        or type(manifest.libc) is not tuple
        or len(manifest.libc) != 2
        or not all(type(value) is str and value for value in manifest.libc)
        or type(manifest.build) is not tuple
        or len(manifest.build) != 2
        or not all(type(value) is str and value for value in manifest.build)
        or manifest.private_manifest is None
        or validate_private_manifest(manifest.private_manifest) is not None
        or not _is_sha256(manifest.source_sha256)
    ):
        return "runtime:manifest_incomplete"
    return None


def runtime_manifest_payload(
    manifest: RuntimeManifest,
) -> dict[str, str | tuple[str, ...] | tuple[tuple[str, str], ...] | None]:
    """Return a stable scalar payload for JSON evidence serialization."""
    private = manifest.private_manifest
    return {
        "operating_system": manifest.operating_system,
        "implementation": manifest.implementation,
        "version": ".".join(str(part) for part in manifest.version_info[:3]),
        "release_level": manifest.version_info[3],
        "sys_version": manifest.sys_version,
        "executable": manifest.executable,
        "machine": manifest.machine,
        "libc": manifest.libc,
        "build": manifest.build,
        "cache_tag": manifest.cache_tag,
        "image_digest": manifest.image_digest,
        "httpx_version": manifest.httpx_version,
        "httpcore_version": manifest.httpcore_version,
        "private_class_identities": None if private is None else private.class_identities,
        "private_signatures": None if private is None else private.signatures,
        "private_source_fingerprints": None if private is None else private.source_fingerprints,
        "private_callable_fingerprints": (
            None if private is None else private.callable_fingerprints
        ),
        "source_sha256": manifest.source_sha256,
    }


async def _request_once(client: httpx.AsyncClient, url: str, identifier: str) -> RequestAttempt:
    """Issue one fixed-shape request and retain only bounded client observations."""
    headers = build_request_headers(identifier)
    trace_events: list[TraceEvent] = []

    async def trace(name: str, _info: dict[str, object]) -> None:  # noqa: OBJECT_OK
        trace_events.append(TraceEvent(name, perf_counter_ns()))

    request_start_ns = perf_counter_ns()
    request = client.build_request(
        "POST",
        url,
        json=build_request_payload(),
        headers=headers,
        extensions={"trace": trace},
    )
    request_built_ns = perf_counter_ns()
    response: httpx.Response | None = None
    status_code: int | None = None
    response_bytes: bytes | None = None
    http_version: str | None = None
    error: str | None = None
    try:
        response = await client.send(request, stream=False)
        status_code = response.status_code
        response_bytes = response.content
        http_version = response.http_version
    except (
        httpx.HTTPError,
        httpcore.ConnectionNotAvailable,
        httpcore.NetworkError,
        httpcore.ProtocolError,
        OSError,
        RuntimeError,
        TimeoutError,
    ) as exc:
        error = type(exc).__name__
    finally:
        if response is not None:
            try:
                await response.aclose()
            except (
                httpx.HTTPError,
                httpcore.ConnectionNotAvailable,
                httpcore.NetworkError,
                httpcore.ProtocolError,
                OSError,
                RuntimeError,
                TimeoutError,
            ) as exc:
                if error is None:
                    error = type(exc).__name__
    return RequestAttempt(
        identifier,
        request_start_ns,
        request_built_ns,
        perf_counter_ns(),
        tuple(trace_events),
        status_code,
        response_bytes,
        http_version,
        error,
        capture_clock_metadata(),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P10-AV Linux dispatch attribution diagnostic")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--failed-gates", type=int, default=0)
    parser.add_argument("--matrix-cases", type=int, default=0)
    parser.add_argument("--cleanup", action="store_true")
    return parser


def main() -> int:
    """Emit a fail-closed result until a validated matrix runner owns the result."""
    args = _build_parser().parse_args()
    del args
    result = CliResult(0, False, 1, False, False)
    print(render_cli_execution(result))
    return cli_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
