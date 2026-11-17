from __future__ import annotations

from collections.abc import AsyncIterator

import anyio
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from starlette.types import Message, Scope

from app.core.config import settings
from app.memory_core.world_memory_event_history_asgi_diagnostics import (
    ASGI_DIAGNOSTIC_HEADER_NAME,
    ASGI_DIAGNOSTICS_PATH,
    ASGIDiagnosticApp,
    ASGIDiagnosticCollector,
    ASGIDiagnosticSample,
    DiagnosticId,
    _RequestState,
    mark_handler_entry,
    mark_handler_exit,
)
from app.memory_core.world_memory_event_history_coordinator_app import (
    create_world_memory_event_history_coordinator_app,
)


class _RouteFailure(RuntimeError):
    pass


def _scope(path: str = "/", diagnostic_id: str | None = "p10at-id") -> Scope:
    headers = (
        []
        if diagnostic_id is None
        else [(ASGI_DIAGNOSTIC_HEADER_NAME.encode(), diagnostic_id.encode())]
    )
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "server": ("test", 80),
        "client": ("test", 1),
    }


async def _receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


def _marked_fastapi(path: str = "/"):
    app = FastAPI()

    @app.get(path)
    async def endpoint() -> dict[str, bool]:
        mark_handler_entry()
        try:
            return {"ok": True}
        finally:
            mark_handler_exit()

    return app


async def _compare_public_response(
    app: FastAPI,
    collector: ASGIDiagnosticCollector,
    diagnostic_id: str,
) -> tuple[httpx.Response, httpx.Response]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        off = await client.get("/")
    wrapped = ASGIDiagnosticApp(app, collector)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="http://test"
    ) as client:
        on = await client.get("/", headers={ASGI_DIAGNOSTIC_HEADER_NAME: diagnostic_id})
    return off, on


def _marked_refusal_fastapi() -> FastAPI:
    app = FastAPI()

    @app.get("/")
    async def refusal() -> JSONResponse:
        mark_handler_entry()
        try:
            return JSONResponse(
                status_code=200,
                content={
                    "entries": [],
                    "candidate_count": 0,
                    "candidate_latency_ms": 0.0,
                    "refusal_reason": "insufficient_evidence",
                },
            )
        finally:
            mark_handler_exit()

    return app


def _marked_error_fastapi() -> FastAPI:
    app = FastAPI()

    @app.get("/")
    async def error() -> JSONResponse:
        mark_handler_entry()
        try:
            raise HTTPException(status_code=409, detail="conflict")
        finally:
            mark_handler_exit()

    return app


def test_asgi_diagnostics_toggle_off_keeps_internal_route_absent(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "WORLD_MEMORY_EVENT_HISTORY_ASGI_DIAGNOSTICS_ENABLED",
        False,
        raising=False,
    )

    coordinator_app = create_world_memory_event_history_coordinator_app()

    assert all(
        not (isinstance(route, APIRoute) and route.path == ASGI_DIAGNOSTICS_PATH)
        for route in coordinator_app.routes
    )


def test_asgi_diagnostics_toggle_on_registers_an_openapi_hidden_route(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "WORLD_MEMORY_EVENT_HISTORY_ASGI_DIAGNOSTICS_ENABLED",
        True,
        raising=False,
    )

    coordinator_app = create_world_memory_event_history_coordinator_app()

    routes = [
        route
        for route in coordinator_app.routes
        if isinstance(route, APIRoute) and route.path == ASGI_DIAGNOSTICS_PATH
    ]
    assert len(routes) == 1
    assert routes[0].include_in_schema is False


def test_toggle_does_not_change_public_openapi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WORLD_MEMORY_EVENT_HISTORY_ASGI_DIAGNOSTICS_ENABLED", False)
    off = create_world_memory_event_history_coordinator_app().openapi()
    monkeypatch.setattr(settings, "WORLD_MEMORY_EVENT_HISTORY_ASGI_DIAGNOSTICS_ENABLED", True)
    on = create_world_memory_event_history_coordinator_app().openapi()
    assert on == off


@pytest.mark.anyio
async def test_wrapper_preserves_public_response_bytes_and_fields() -> None:
    collector = ASGIDiagnosticCollector()
    off, on = await _compare_public_response(_marked_fastapi(), collector, "public-id")

    assert off.status_code == on.status_code == 200
    assert off.content == on.content
    assert set(off.json()) == set(on.json())
    assert tuple(sample.complete for sample in collector.snapshot().samples) == (True,)


@pytest.mark.anyio
async def test_wrapper_preserves_refusal_response_and_records_complete_sample() -> None:
    collector = ASGIDiagnosticCollector()
    off, on = await _compare_public_response(_marked_refusal_fastapi(), collector, "refusal-id")

    assert off.status_code == on.status_code == 200
    assert off.content == on.content
    assert set(off.json()) == set(on.json())
    assert tuple(sample.complete for sample in collector.snapshot().samples) == (True,)


@pytest.mark.anyio
async def test_wrapper_preserves_converted_error_response_and_records_complete_sample() -> None:
    collector = ASGIDiagnosticCollector()
    off, on = await _compare_public_response(_marked_error_fastapi(), collector, "error-id")

    assert off.status_code == on.status_code == 409
    assert off.content == on.content
    assert set(off.json()) == set(on.json())
    assert tuple(sample.complete for sample in collector.snapshot().samples) == (True,)


@pytest.mark.anyio
async def test_final_send_is_recorded_only_after_await_returns() -> None:
    collector = ASGIDiagnosticCollector()
    wrapped = ASGIDiagnosticApp(_marked_fastapi(), collector)
    release = anyio.Event()
    final_started = anyio.Event()

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            final_started.set()
            await release.wait()

    async with anyio.create_task_group() as group:
        group.start_soon(wrapped, _scope(), _receive, send)
        await final_started.wait()
        assert collector.snapshot().samples == ()
        release.set()

    samples = collector.snapshot().samples
    assert len(samples) == 1
    assert samples[0].complete is True
    assert samples[0].response_send_complete_ns is not None


@pytest.mark.anyio
async def test_streaming_records_only_the_final_body() -> None:
    app = FastAPI()

    async def chunks() -> AsyncIterator[bytes]:
        yield b"first"
        yield b"last"

    @app.get("/")
    async def stream() -> StreamingResponse:
        mark_handler_entry()
        try:
            return StreamingResponse(chunks())
        finally:
            mark_handler_exit()

    collector = ASGIDiagnosticCollector()
    wrapped = ASGIDiagnosticApp(app, collector)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="http://test"
    ) as client:
        response = await client.get("/", headers={ASGI_DIAGNOSTIC_HEADER_NAME: "stream-id"})

    assert response.status_code == 200
    assert response.content == b"firstlast"
    samples = collector.snapshot().samples
    assert len(samples) == 1
    assert samples[0].complete is True


@pytest.mark.anyio
async def test_exception_and_send_failure_record_incomplete_and_stay_loud() -> None:
    failing = FastAPI()

    @failing.get("/")
    async def error() -> dict[str, bool]:
        mark_handler_entry()
        raise _RouteFailure("route failure")

    collector = ASGIDiagnosticCollector()
    wrapped = ASGIDiagnosticApp(failing, collector)
    with pytest.raises(RuntimeError, match="route failure"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=wrapped), base_url="http://test"
        ) as client:
            await client.get("/", headers={ASGI_DIAGNOSTIC_HEADER_NAME: "error-id"})
    assert collector.snapshot().samples[-1].complete is False

    async def send_failure(message: Message) -> None:
        if message["type"] == "http.response.body":
            raise OSError("send failure")

    with pytest.raises(OSError, match="send failure"):
        await wrapped(_scope(diagnostic_id="send-id"), _receive, send_failure)
    assert collector.snapshot().samples[-1].complete is False


def test_collector_retention_is_bounded() -> None:
    collector = ASGIDiagnosticCollector()
    for index in range(2050):
        sample = ASGIDiagnosticSample(
            diagnostic_id=f"id-{index}",
            asgi_entry_ns=1,
            handler_entry_ns=2,
            handler_exit_ns=3,
            response_send_complete_ns=4,
            complete=True,
        )
        assert sample.complete
        state = _RequestState(DiagnosticId(sample.diagnostic_id), sample.asgi_entry_ns)
        state.handler_entry_ns = sample.handler_entry_ns
        state.handler_exit_ns = sample.handler_exit_ns
        state.response_send_complete_ns = sample.response_send_complete_ns
        collector.record(state, complete=True)
    samples = collector.snapshot().samples
    assert len(samples) == 2048
    assert samples[0].diagnostic_id == "id-2"
