from __future__ import annotations

from collections import deque
from contextvars import ContextVar
from pathlib import Path
from platform import system
from time import get_clock_info, perf_counter_ns
from typing import Final, NewType

from fastapi import FastAPI
from fastapi.routing import BaseRoute
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from starlette.types import Message, Receive, Scope, Send

ASGI_DIAGNOSTICS_PATH: Final[str] = "/internal/world-memory/event-history/asgi-diagnostics"
ASGI_DIAGNOSTIC_HEADER_NAME: Final[str] = "x-world-memory-event-history-diagnostic-id"
ASGI_DIAGNOSTIC_HEADER_BYTES: Final[bytes] = ASGI_DIAGNOSTIC_HEADER_NAME.encode("ascii")
MAX_ASGI_DIAGNOSTIC_ID_LENGTH: Final[int] = 64
MAX_ASGI_DIAGNOSTIC_SAMPLES: Final[int] = 2048
DiagnosticId = NewType("DiagnosticId", str)


class ASGIDiagnosticSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    diagnostic_id: str = Field(min_length=1, max_length=MAX_ASGI_DIAGNOSTIC_ID_LENGTH)
    asgi_entry_ns: int = Field(ge=0)
    handler_entry_ns: int | None = Field(default=None, ge=0)
    handler_exit_ns: int | None = Field(default=None, ge=0)
    response_send_complete_ns: int | None = Field(default=None, ge=0)
    complete: bool = False
    error: str | None = None


class ASGIDiagnosticsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    samples: tuple[ASGIDiagnosticSample, ...] = ()
    snapshot_captured_ns: int | None = Field(default=None, ge=0)
    operating_system: str | None = None
    boot_id: str | None = None
    perf_counter_implementation: str | None = None
    perf_counter_resolution: float | None = Field(default=None, ge=0.0)
    perf_counter_monotonic: bool | None = None
    perf_counter_adjustable: bool | None = None
    fetch_start_ns: int | None = Field(default=None, ge=0)
    fetch_end_ns: int | None = Field(default=None, ge=0)


class _RequestState:
    __slots__ = (
        "asgi_entry_ns",
        "diagnostic_id",
        "handler_entry_ns",
        "handler_exit_ns",
        "recorded",
        "response_send_complete_ns",
    )

    def __init__(self, diagnostic_id: DiagnosticId, asgi_entry_ns: int) -> None:
        self.diagnostic_id = diagnostic_id
        self.asgi_entry_ns = asgi_entry_ns
        self.handler_entry_ns: int | None = None
        self.handler_exit_ns: int | None = None
        self.response_send_complete_ns: int | None = None
        self.recorded = False


_request_state: ContextVar[_RequestState | None] = ContextVar(
    "world_memory_event_history_asgi_request_state",
    default=None,
)


def mark_handler_entry() -> None:
    state = _request_state.get()
    if state is not None and state.handler_entry_ns is None:
        state.handler_entry_ns = perf_counter_ns()


def mark_handler_exit() -> None:
    state = _request_state.get()
    if state is not None and state.handler_exit_ns is None:
        state.handler_exit_ns = perf_counter_ns()


class ASGIDiagnosticCollector:
    __slots__ = ("_samples",)

    def __init__(self) -> None:
        self._samples: deque[ASGIDiagnosticSample] = deque(maxlen=MAX_ASGI_DIAGNOSTIC_SAMPLES)

    def record(self, state: _RequestState, *, complete: bool) -> ASGIDiagnosticSample:
        handler_entry_ns = state.handler_entry_ns
        handler_exit_ns = state.handler_exit_ns
        response_send_complete_ns = state.response_send_complete_ns
        if handler_entry_ns is None or handler_exit_ns is None or response_send_complete_ns is None:
            ordered = False
        else:
            ordered = (
                state.asgi_entry_ns
                <= handler_entry_ns
                <= handler_exit_ns
                <= response_send_complete_ns
            )
        terminal_complete = complete and ordered
        sample = ASGIDiagnosticSample(
            diagnostic_id=state.diagnostic_id,
            asgi_entry_ns=state.asgi_entry_ns,
            handler_entry_ns=state.handler_entry_ns,
            handler_exit_ns=state.handler_exit_ns,
            response_send_complete_ns=response_send_complete_ns,
            complete=terminal_complete,
            error=None if terminal_complete else "incomplete",
        )
        self._samples.append(sample)
        state.recorded = True
        return sample

    def snapshot(self) -> ASGIDiagnosticsSnapshot:
        clock = get_clock_info("perf_counter")
        return ASGIDiagnosticsSnapshot(
            samples=tuple(self._samples),
            snapshot_captured_ns=perf_counter_ns(),
            operating_system=system(),
            boot_id=_linux_boot_id(),
            perf_counter_implementation=clock.implementation,
            perf_counter_resolution=clock.resolution,
            perf_counter_monotonic=clock.monotonic,
            perf_counter_adjustable=clock.adjustable,
        )

    def clear(self) -> None:
        self._samples.clear()


class ASGIDiagnosticApp:
    __slots__ = ("_app", "_collector")

    def __init__(self, app: FastAPI, collector: ASGIDiagnosticCollector) -> None:
        self._app = app
        self._collector = collector

    @property
    def routes(self) -> list[BaseRoute]:
        return self._app.routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        diagnostic_id = _diagnostic_id(scope)
        if diagnostic_id is None:
            await self._app(scope, receive, send)
            return

        state = _RequestState(diagnostic_id, perf_counter_ns())
        token = _request_state.set(state)

        async def send_wrapper(message: Message) -> None:
            await send(message)
            if (
                not state.recorded
                and message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                state.response_send_complete_ns = perf_counter_ns()
                self._collector.record(state, complete=True)

        try:
            await self._app(scope, receive, send_wrapper)
        finally:
            if not state.recorded:
                self._collector.record(state, complete=False)
            _request_state.reset(token)

    def openapi(self) -> dict[str, JsonValue]:
        return self._app.openapi()


def _diagnostic_id(scope: Scope) -> DiagnosticId | None:
    if scope.get("type") != "http":
        return None
    values = [
        value for name, value in scope.get("headers", ()) if name == ASGI_DIAGNOSTIC_HEADER_BYTES
    ]
    if len(values) != 1:
        return None
    try:
        value = values[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    if not 1 <= len(value) <= MAX_ASGI_DIAGNOSTIC_ID_LENGTH:
        return None
    if not all(0x21 <= ord(character) <= 0x7E for character in value):
        return None
    return DiagnosticId(value)


def _linux_boot_id() -> str | None:
    if system() != "Linux":
        return None
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value or None


_collector = ASGIDiagnosticCollector()


def get_asgi_diagnostic_snapshot() -> ASGIDiagnosticsSnapshot:
    return _collector.snapshot()


def build_asgi_diagnostic_app(app: FastAPI) -> ASGIDiagnosticApp:
    return ASGIDiagnosticApp(app, _collector)


__all__ = [
    "ASGI_DIAGNOSTICS_PATH",
    "ASGI_DIAGNOSTIC_HEADER_BYTES",
    "ASGI_DIAGNOSTIC_HEADER_NAME",
    "MAX_ASGI_DIAGNOSTIC_ID_LENGTH",
    "MAX_ASGI_DIAGNOSTIC_SAMPLES",
    "ASGIDiagnosticApp",
    "ASGIDiagnosticCollector",
    "ASGIDiagnosticSample",
    "ASGIDiagnosticsSnapshot",
    "DiagnosticId",
    "build_asgi_diagnostic_app",
    "get_asgi_diagnostic_snapshot",
    "mark_handler_entry",
    "mark_handler_exit",
]
