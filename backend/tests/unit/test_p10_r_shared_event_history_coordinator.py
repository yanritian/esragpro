"""Focused unit coverage for the shared event-history coordinator (P10-R).

Covers the observable boundaries of the coordinator contract without
starting any infrastructure:

- request normalization and the exact retrieval-identity schema;
- coordinator-app delegation to the PostgreSQL authority and envelope
  preservation;
- HTTP transport failure / non-success / invalid payload are all
  fail-closed as ``event_history_authority_unavailable``;
- the public world-memory query path forwards retrieval fields to the
  coordinator and applies each caller's token budget locally;
- coordinator refusal is returned fail-closed;
- closing the coordinator client cleans its cached state;
- main application shutdown includes coordinator HTTP client cleanup.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4, uuid5

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.api import world_memory_plane as wmp
from app.memory_core import world_memory_event_history_coordinator_client as client_module
from app.memory_core.repositories.world_memory_authority import StoredEnvelope
from app.memory_core.world_memory import WorldObservation
from app.memory_core.world_memory_event_history_coordinator import (
    EVENT_HISTORY_COORDINATOR_UNAVAILABLE,
    EventHistoryCoordinatorRequest,
    EventHistoryCoordinatorResponse,
    EventHistoryTemporalTrace,
    PreparedEventHistoryEntry,
)
import app.memory_core.world_memory_event_history_coordinator_app as coordinator_app
from app.memory_core.world_memory_event_history_coordinator_app import (
    world_memory_event_history_query,
)
from app.memory_core.world_memory_event_history_coordinator_client import (
    close_world_memory_event_history_coordinator_client,
)
from app.memory_core.world_memory_serializer import (
    EVENT_OBSERVATION_INGESTED,
    build_world_memory_envelope,
)


@pytest.fixture(autouse=True)
async def _reset_coordinator_client() -> AsyncIterator[None]:
    await close_world_memory_event_history_coordinator_client()
    yield
    await close_world_memory_event_history_coordinator_client()


def _make_stored_envelope(
    *,
    content: str,
    occurred_at: datetime,
) -> StoredEnvelope:
    envelope = build_world_memory_envelope(
        WorldObservation(
            id=uuid4(),
            content=content,
            observed_at=occurred_at,
            ingested_at=occurred_at,
            source="rescue-sensor",
            raw_evidence_ref="sensor://road-closure/1000",
        ),
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="elasticsearch",
    )
    return StoredEnvelope(
        id=uuid4(),
        event_type=envelope["event_type"],
        schema_version=envelope["schema_version"],
        aggregate_id=uuid4(),
        projection_target=envelope["projection_target"],
        idempotency_key=envelope["idempotency_key"],
        occurred_at=occurred_at,
        kind="observation",
        envelope=envelope,
        created_at=occurred_at,
    )


def _make_entry(
    *,
    content: str,
    occurred_at: datetime,
    evidence_ids: tuple[Any, ...] | None = None,
) -> PreparedEventHistoryEntry:
    envelope = build_world_memory_envelope(
        WorldObservation(
            id=uuid4(),
            content=content,
            observed_at=occurred_at,
            ingested_at=occurred_at,
            source="rescue-sensor",
            raw_evidence_ref="sensor://road-closure/1000",
        ),
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="elasticsearch",
    )
    # Mirror the coordinator app: a raw-evidence reference surfaces the same
    # deterministic evidence UUID the public response derives.
    if evidence_ids is None:
        evidence_ids = (
            uuid5(wmp._EVIDENCE_REF_NAMESPACE, "sensor://road-closure/1000"),
        )
    return PreparedEventHistoryEntry(
        envelope=envelope,
        kind="observation",
        id=uuid4(),
        content=content,
        evidence_ids=evidence_ids,
        temporal=EventHistoryTemporalTrace(event_occurred_at=occurred_at),
    )


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: dict[str, Any] | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error
        self._content: bytes | None = (
            None if payload is None else json.dumps(payload).encode("utf-8")
        )

    @property
    def content(self) -> bytes:
        if self._json_error is not None:
            raise self._json_error
        if self._content is None:
            raise ValueError("No JSON payload")
        return self._content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://coordinator/query")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=response,
            )

    def json(self) -> dict[str, Any]:
        if self._json_error is not None:
            raise self._json_error
        if self._payload is None:
            raise ValueError("No JSON payload")
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.posted: list[tuple[str, dict[str, Any] | None]] = []

    async def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:
        self.posted.append((url, json))
        if self._error is not None:
            raise self._error
        if self._response is None:
            raise AssertionError("fake client has no response configured")
        return self._response

    async def aclose(self) -> None:
        return None


# ============================================================================
# Request normalization + exact retrieval-identity schema
# ============================================================================


def test_coordinator_request_trims_query() -> None:
    cutoff = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    request = EventHistoryCoordinatorRequest(
        query="  road closure  ",
        event_time_lte=cutoff,
        kind="observation",
        aggregate_id=uuid4(),
        require_evidence=True,
        max_items=25,
    )
    assert request.query == "road closure"


def test_coordinator_request_rejects_blank_query() -> None:
    cutoff = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(ValidationError):
        EventHistoryCoordinatorRequest(
            query="   ",
            event_time_lte=cutoff,
            max_items=50,
        )


def test_coordinator_request_schema_is_exact_retrieval_identity() -> None:
    assert set(EventHistoryCoordinatorRequest.model_fields) == {
        "query",
        "event_time_lte",
        "kind",
        "aggregate_id",
        "require_evidence",
        "max_items",
    }
    assert "token_budget" not in EventHistoryCoordinatorRequest.model_fields


def test_coordinator_request_rejects_token_budget() -> None:
    cutoff = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(ValidationError):
        EventHistoryCoordinatorRequest(
            query="road closure",
            event_time_lte=cutoff,
            max_items=50,
            # The whole point is that token_budget is not part of the
            # coordinator schema; passing it must be rejected.
            token_budget=4000,  # pyright: ignore[reportCallIssue]
        )


# ============================================================================
# Coordinator app: delegation + PostgreSQL-authority contract preservation
# ============================================================================


async def test_coordinator_app_delegates_exact_identity_and_preserves_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occurred_at = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    stored = _make_stored_envelope(content="Historic road closure", occurred_at=occurred_at)
    captured: dict[str, Any] = {}

    async def fake_pg_event_history_query(**kwargs: Any) -> tuple[list[Any], list[Any], float, str | None]:
        captured.update(kwargs)
        return (
            [stored],
            [
                wmp.WorldMemorySourceTrace(
                    source="elasticsearch_event_history",
                    status="ok",
                    candidate_count=3,
                    latency_ms=7.0,
                )
            ],
            8.0,
            None,
        )

    monkeypatch.setattr(wmp, "_pg_event_history_query", fake_pg_event_history_query)

    request = EventHistoryCoordinatorRequest(
        query="  road closure  ",
        event_time_lte=occurred_at,
        kind="observation",
        aggregate_id=stored.aggregate_id,
        require_evidence=True,
        max_items=25,
    )
    response = await world_memory_event_history_query(request)

    # Delegation receives the normalized exact identity.
    assert captured["query"] == "road closure"
    assert captured["event_time_lte"] == occurred_at
    assert captured["kind"] == "observation"
    assert captured["aggregate_id"] == stored.aggregate_id
    assert captured["require_evidence"] is True
    assert captured["max_items"] == 25

    # PostgreSQL-authority envelopes/items, candidate count, latency preserved.
    assert response.refusal_reason is None
    assert response.candidate_count == 3
    assert response.candidate_latency_ms == 7.0
    assert len(response.entries) == 1
    entry = response.entries[0]
    assert entry.envelope == stored.envelope
    assert entry.kind == "observation"
    assert entry.content == "Historic road closure"
    assert entry.temporal.event_occurred_at == occurred_at


async def test_coordinator_app_preserves_refusal_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_pg_event_history_query(**kwargs: Any) -> tuple[list[Any], list[Any], float, str | None]:
        return (
            [],
            [
                wmp.WorldMemorySourceTrace(
                    source="elasticsearch_event_history",
                    status="refused",
                    candidate_count=1,
                    latency_ms=2.0,
                    error="insufficient_evidence",
                )
            ],
            3.0,
            "insufficient_evidence",
        )

    monkeypatch.setattr(wmp, "_pg_event_history_query", fake_pg_event_history_query)

    request = EventHistoryCoordinatorRequest(
        query="road closure",
        event_time_lte=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
        max_items=50,
    )
    response = await world_memory_event_history_query(request)

    assert response.refusal_reason == "insufficient_evidence"
    assert response.entries == ()
    assert response.candidate_count == 1
    assert response.candidate_latency_ms == 2.0


async def test_coordinator_prepared_entry_surfaces_observation_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coordinator prepared entry carries evidence for evidence-backed observations.

    Regression for the P10-M evidence gap through the shared coordinator path:
    an evidence-backed observation must surface a stable evidence ID in the
    prepared entry so the public response is verifiable.
    """
    occurred_at = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    stored = _make_stored_envelope(content="Historic road closure", occurred_at=occurred_at)

    async def fake_pg_event_history_query(**kwargs: Any) -> tuple[list[Any], list[Any], float, str | None]:
        return (
            [stored],
            [
                wmp.WorldMemorySourceTrace(
                    source="elasticsearch_event_history",
                    status="ok",
                    candidate_count=1,
                    latency_ms=3.0,
                )
            ],
            4.0,
            None,
        )

    monkeypatch.setattr(wmp, "_pg_event_history_query", fake_pg_event_history_query)

    request = EventHistoryCoordinatorRequest(
        query="road closure",
        event_time_lte=occurred_at,
        kind="observation",
        max_items=50,
        require_evidence=True,
    )
    response = await world_memory_event_history_query(request)

    assert response.refusal_reason is None
    assert len(response.entries) == 1
    entry = response.entries[0]
    assert entry.kind == "observation"
    assert len(entry.evidence_ids) == 1
    # The prepared entry surfaces the same deterministic evidence UUID as the
    # public response's _extract_context_items path.
    assert entry.evidence_ids == (
        uuid5(wmp._EVIDENCE_REF_NAMESPACE, "sensor://road-closure/1000"),
    )


# ============================================================================
# HTTP transport failure / non-success / invalid payload -> authority unavailable
# ============================================================================


async def test_http_transport_failure_is_authority_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(error=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(
        client_module,
        "get_world_memory_event_history_coordinator_client",
        AsyncMock(return_value=fake),
    )
    monkeypatch.setattr(client_module, "_configured_url", lambda: "http://coordinator/query")

    request = EventHistoryCoordinatorRequest(
        query="road closure",
        event_time_lte=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
        max_items=50,
    )
    response = await client_module.query_world_memory_event_history_coordinator(request)

    assert response.refusal_reason == EVENT_HISTORY_COORDINATOR_UNAVAILABLE
    assert response.entries == ()
    assert len(fake.posted) == 1


async def test_non_success_http_response_is_authority_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(response=_FakeResponse(status_code=500))
    monkeypatch.setattr(
        client_module,
        "get_world_memory_event_history_coordinator_client",
        AsyncMock(return_value=fake),
    )
    monkeypatch.setattr(client_module, "_configured_url", lambda: "http://coordinator/query")

    request = EventHistoryCoordinatorRequest(
        query="road closure",
        event_time_lte=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
        max_items=50,
    )
    response = await client_module.query_world_memory_event_history_coordinator(request)

    assert response.refusal_reason == EVENT_HISTORY_COORDINATOR_UNAVAILABLE
    assert response.entries == ()
    assert len(fake.posted) == 1


async def test_invalid_payload_is_authority_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(response=_FakeResponse(status_code=200, payload={"bogus": True}))
    monkeypatch.setattr(
        client_module,
        "get_world_memory_event_history_coordinator_client",
        AsyncMock(return_value=fake),
    )
    monkeypatch.setattr(client_module, "_configured_url", lambda: "http://coordinator/query")

    request = EventHistoryCoordinatorRequest(
        query="road closure",
        event_time_lte=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
        max_items=50,
    )
    response = await client_module.query_world_memory_event_history_coordinator(request)

    assert response.refusal_reason == EVENT_HISTORY_COORDINATOR_UNAVAILABLE
    assert response.entries == ()


async def test_json_decode_error_is_authority_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(
        response=_FakeResponse(status_code=200, json_error=ValueError("invalid json"))
    )
    monkeypatch.setattr(
        client_module,
        "get_world_memory_event_history_coordinator_client",
        AsyncMock(return_value=fake),
    )
    monkeypatch.setattr(client_module, "_configured_url", lambda: "http://coordinator/query")

    request = EventHistoryCoordinatorRequest(
        query="road closure",
        event_time_lte=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
        max_items=50,
    )
    response = await client_module.query_world_memory_event_history_coordinator(request)

    assert response.refusal_reason == EVENT_HISTORY_COORDINATOR_UNAVAILABLE
    assert response.entries == ()


# ============================================================================
# Public query path: forwards retrieval identity, applies token budget locally
# ============================================================================


async def test_public_path_forwards_identity_and_budgets_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cutoff = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    entry1 = _make_entry(content="A" * 40, occurred_at=cutoff)
    entry2 = _make_entry(content="B" * 4, occurred_at=cutoff)
    captured_requests: list[EventHistoryCoordinatorRequest] = []

    async def fake_query_coordinator(
        coordinator_request: EventHistoryCoordinatorRequest,
    ) -> EventHistoryCoordinatorResponse:
        captured_requests.append(coordinator_request)
        return EventHistoryCoordinatorResponse(
            entries=(entry1, entry2),
            candidate_count=2,
            candidate_latency_ms=3.0,
        )

    monkeypatch.setattr(
        client_module,
        "world_memory_event_history_coordinator_configured",
        lambda: True,
    )
    monkeypatch.setattr(
        client_module,
        "query_world_memory_event_history_coordinator",
        fake_query_coordinator,
    )

    small = wmp.WorldMemoryQueryRequest(
        query="  road closure  ",
        event_time_lte=cutoff,
        kind="observation",
        aggregate_id=None,
        require_evidence=True,
        max_items=50,
        token_budget=10,
    )
    response_small = await wmp.world_memory_query(small)

    assert len(response_small.items) == 1
    assert response_small.items[0].content == "A" * 40
    assert response_small.envelopes == [entry1.envelope]
    assert response_small.token_used == 10
    assert response_small.token_budget == 10
    assert response_small.status == "ok"
    # An evidence-required positive response surfaces a stable evidence ID,
    # never an empty list.
    assert response_small.items[0].evidence_ids == [
        str(uuid5(wmp._EVIDENCE_REF_NAMESPACE, "sensor://road-closure/1000"))
    ]

    large = wmp.WorldMemoryQueryRequest(
        query="  road closure  ",
        event_time_lte=cutoff,
        kind="observation",
        aggregate_id=None,
        require_evidence=True,
        max_items=50,
        token_budget=100,
    )
    response_large = await wmp.world_memory_query(large)

    assert len(response_large.items) == 2
    assert response_large.token_used == 11
    assert response_large.token_budget == 100
    assert response_large.status == "ok"
    assert all(
        item.evidence_ids
        == [str(uuid5(wmp._EVIDENCE_REF_NAMESPACE, "sensor://road-closure/1000"))]
        for item in response_large.items
    )

    # The coordinator never sees a token budget and receives only the
    # normalized retrieval-identity fields.
    assert len(captured_requests) == 2
    for coordinator_request in captured_requests:
        assert coordinator_request.query == "road closure"
        assert coordinator_request.event_time_lte == cutoff
        assert coordinator_request.kind == "observation"
        assert coordinator_request.require_evidence is True
        assert coordinator_request.max_items == 50
        assert "token_budget" not in EventHistoryCoordinatorRequest.model_fields
        assert not hasattr(coordinator_request, "token_budget")


async def test_public_path_refuses_fail_closed_on_coordinator_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cutoff = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        client_module,
        "world_memory_event_history_coordinator_configured",
        lambda: True,
    )

    async def fake_query_coordinator(
        coordinator_request: EventHistoryCoordinatorRequest,
    ) -> EventHistoryCoordinatorResponse:
        return EventHistoryCoordinatorResponse(
            candidate_count=5,
            candidate_latency_ms=4.0,
            refusal_reason="insufficient_evidence",
        )

    monkeypatch.setattr(
        client_module,
        "query_world_memory_event_history_coordinator",
        fake_query_coordinator,
    )

    fallback_called = False

    async def fake_pg_event_history_query(**kwargs: Any) -> Any:
        nonlocal fallback_called
        fallback_called = True
        return [], [], 0.0, "should_not_happen"

    monkeypatch.setattr(wmp, "_is_pg_authority_configured", lambda: True)
    monkeypatch.setattr(wmp, "_pg_event_history_query", fake_pg_event_history_query)

    request = wmp.WorldMemoryQueryRequest(
        query="road closure",
        event_time_lte=cutoff,
        kind=None,
        aggregate_id=None,
        require_evidence=True,
        max_items=50,
        token_budget=4000,
    )
    response = await wmp.world_memory_query(request)

    assert response.status == "refused"
    assert response.refusal_reason == "insufficient_evidence"
    assert response.items == []
    assert response.envelopes == []
    assert response.candidate_sources[0].source == "elasticsearch_event_history"
    assert response.candidate_sources[0].status == "refused"
    assert response.candidate_sources[0].candidate_count == 5
    assert fallback_called is False


async def test_public_path_refuses_evidence_less_coordinator_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An evidence-less coordinator entry fails closed when evidence is required.

    Regression for the P10-S boundary gap: the public path must not emit an
    evidence-required item with an empty evidence-ID list just because the
    coordinator returned a positive entry.
    """
    cutoff = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    evidence_less = _make_entry(
        content="Historic road closure",
        occurred_at=cutoff,
        evidence_ids=(),
    )

    monkeypatch.setattr(
        client_module,
        "world_memory_event_history_coordinator_configured",
        lambda: True,
    )

    async def fake_query_coordinator(
        coordinator_request: EventHistoryCoordinatorRequest,
    ) -> EventHistoryCoordinatorResponse:
        return EventHistoryCoordinatorResponse(
            entries=(evidence_less,),
            candidate_count=1,
            candidate_latency_ms=2.0,
        )

    monkeypatch.setattr(
        client_module,
        "query_world_memory_event_history_coordinator",
        fake_query_coordinator,
    )

    request = wmp.WorldMemoryQueryRequest(
        query="road closure",
        event_time_lte=cutoff,
        kind="observation",
        aggregate_id=None,
        require_evidence=True,
        max_items=50,
        token_budget=4000,
    )
    response = await wmp.world_memory_query(request)

    assert response.status == "refused"
    assert response.refusal_reason == "insufficient_evidence"
    assert response.items == []
    assert response.envelopes == []
    assert response.candidate_sources[0].source == "elasticsearch_event_history"
    assert response.candidate_sources[0].status == "ok"
    assert response.candidate_sources[0].candidate_count == 1


# ============================================================================
# Coordinator client lifecycle
# ============================================================================


async def test_close_coordinator_client_cleans_cached_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client_module.settings,
        "WORLD_MEMORY_EVENT_HISTORY_COORDINATOR_URL",
        "http://coordinator/query",
    )

    client = await client_module.get_world_memory_event_history_coordinator_client()
    assert client is not None
    assert client_module._coordinator_http_client is client
    assert client_module._coordinator_http_url == "http://coordinator/query"

    await close_world_memory_event_history_coordinator_client()

    assert client_module._coordinator_http_client is None
    assert client_module._coordinator_http_url is None


async def test_close_coordinator_client_aclosses_cached_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = AsyncMock()
    monkeypatch.setattr(client_module, "_coordinator_http_client", fake_client)
    monkeypatch.setattr(client_module, "_coordinator_http_url", "http://coordinator/query")

    await close_world_memory_event_history_coordinator_client()

    fake_client.aclose.assert_awaited_once()
    assert client_module._coordinator_http_client is None
    assert client_module._coordinator_http_url is None


# ============================================================================
# Main application shutdown
# ============================================================================


async def test_main_lifespan_shutdown_awaits_coordinator_client_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_module

    coordinator_close = AsyncMock()
    monkeypatch.setattr(
        main_module,
        "close_world_memory_event_history_coordinator_client",
        coordinator_close,
    )
    monkeypatch.setattr(main_module, "close_world_memory_event_history_client", AsyncMock())
    monkeypatch.setattr(main_module, "close_world_memory_candidate_sources", AsyncMock())
    monkeypatch.setattr(main_module, "close_db_pool", AsyncMock())
    monkeypatch.setattr(main_module, "close_session_factory", AsyncMock())
    monkeypatch.setattr(main_module, "get_db_pool", AsyncMock())
    monkeypatch.setattr(main_module, "init_session_factory", AsyncMock())
    monkeypatch.setattr(main_module, "get_world_memory_candidate_sources", AsyncMock())
    monkeypatch.setattr(main_module, "get_world_memory_event_history_client", AsyncMock())
    monkeypatch.setattr(main_module.es_client, "connect", AsyncMock())
    monkeypatch.setattr(main_module.es_client, "create_index", AsyncMock(return_value=True))
    monkeypatch.setattr(main_module.es_client, "close", AsyncMock())

    async with main_module.lifespan(FastAPI()):
        coordinator_close.assert_not_called()
    coordinator_close.assert_awaited_once()


# ============================================================================
# P10-R shared prepared-response singleflight
# ============================================================================


async def test_concurrent_identical_calls_share_one_preparation_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identical concurrent requests join one leader task and clean up.

    The preparation authority is entered exactly once, both callers receive
    the identical prepared response, and ``in_flight`` returns to zero after
    the shared task completes.
    """
    occurred_at = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    stored = _make_stored_envelope(content="Historic road closure", occurred_at=occurred_at)
    entered = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def fake_pg_event_history_query(**kwargs: Any) -> tuple[list[Any], list[Any], float, str | None]:
        nonlocal call_count
        call_count += 1
        entered.set()
        await release.wait()
        return (
            [stored],
            [
                wmp.WorldMemorySourceTrace(
                    source="elasticsearch_event_history",
                    status="ok",
                    candidate_count=3,
                    latency_ms=7.0,
                )
            ],
            8.0,
            None,
        )

    monkeypatch.setattr(wmp, "_pg_event_history_query", fake_pg_event_history_query)

    request = EventHistoryCoordinatorRequest(
        query="  road closure  ",
        event_time_lte=occurred_at,
        kind="observation",
        aggregate_id=stored.aggregate_id,
        require_evidence=True,
        max_items=25,
    )

    baseline_leader_count = coordinator_app._prepared_response_leader_count

    caller_a = asyncio.create_task(world_memory_event_history_query(request))
    await entered.wait()
    caller_b = asyncio.create_task(world_memory_event_history_query(request))
    # Let caller B attach to the shared shield before releasing the leader, so
    # it deterministically joins the in-flight task rather than starting a new
    # leader after the first one completes.
    await asyncio.sleep(0)
    release.set()
    responses = await asyncio.gather(caller_a, caller_b)

    stats = await coordinator_app.world_memory_event_history_stats()
    assert stats.in_flight == 0
    assert stats.leader_count == baseline_leader_count + 1
    assert call_count == 1
    assert responses[0] == responses[1]
    assert responses[0].refusal_reason is None
    assert len(responses[0].entries) == 1
    assert responses[0].entries[0].content == "Historic road closure"


async def test_cancelling_one_caller_does_not_cancel_shared_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled joiner leaves the shared leader task untouched.

    The second caller attaches to the same shield and is cancelled while the
    leader is still preparing; the leader survives, the remaining caller gets
    the prepared response, and ``in_flight`` returns to zero.
    """
    occurred_at = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    stored = _make_stored_envelope(content="Historic road closure", occurred_at=occurred_at)
    entered = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def fake_pg_event_history_query(**kwargs: Any) -> tuple[list[Any], list[Any], float, str | None]:
        nonlocal call_count
        call_count += 1
        entered.set()
        await release.wait()
        return (
            [stored],
            [
                wmp.WorldMemorySourceTrace(
                    source="elasticsearch_event_history",
                    status="ok",
                    candidate_count=3,
                    latency_ms=7.0,
                )
            ],
            8.0,
            None,
        )

    monkeypatch.setattr(wmp, "_pg_event_history_query", fake_pg_event_history_query)

    request = EventHistoryCoordinatorRequest(
        query="  road closure  ",
        event_time_lte=occurred_at,
        kind="observation",
        aggregate_id=stored.aggregate_id,
        require_evidence=True,
        max_items=25,
    )

    baseline_leader_count = coordinator_app._prepared_response_leader_count

    # Deterministic seam: observe the real shield join rather than wrapping the
    # coordinator entry. The wrapper counts asyncio.shield invocations and sets
    # an event on the second call, which is caller B joining the shared leader
    # shield. Original shield behavior is preserved and the patch is restored by
    # pytest's monkeypatch fixture.
    original_shield = coordinator_app.asyncio.shield
    shield_call_count = 0
    caller_b_joined_shield = asyncio.Event()

    def tracked_shield(
        task: asyncio.Future[EventHistoryCoordinatorResponse],
    ) -> asyncio.Future[EventHistoryCoordinatorResponse]:
        nonlocal shield_call_count
        shield_call_count += 1
        if shield_call_count == 2:
            caller_b_joined_shield.set()
        return original_shield(task)

    monkeypatch.setattr(coordinator_app.asyncio, "shield", tracked_shield)

    caller_a = asyncio.create_task(world_memory_event_history_query(request))
    await entered.wait()
    caller_b = asyncio.create_task(world_memory_event_history_query(request))
    # Deterministically wait until caller B has joined the shared leader's
    # shield before cancelling it.
    await caller_b_joined_shield.wait()
    caller_b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller_b

    # The shared preparation leader must survive the cancelled joiner.
    assert not caller_a.done()

    release.set()
    response_a = await caller_a

    stats = await coordinator_app.world_memory_event_history_stats()
    assert stats.in_flight == 0
    # The completed prepared-response task must have been removed from the
    # registry, locking the existing cleanup contract.
    assert coordinator_app._prepared_response_tasks == {}
    assert stats.leader_count == baseline_leader_count + 1
    assert call_count == 1
    assert response_a.refusal_reason is None
    assert len(response_a.entries) == 1
    assert response_a.entries[0].content == "Historic road closure"


# ============================================================================
# Public temporal mapping
# ============================================================================


def test_public_temporal_mapping_preserves_all_seven_fields() -> None:
    """The coordinator temporal trace maps onto the public response model.

    All seven temporal fields survive ``_coordinator_temporal_to_public_temporal``,
    not only ``event_occurred_at``.
    """
    event_occurred_at = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    observed_at = datetime(2026, 8, 2, 10, 1, tzinfo=timezone.utc)
    ingested_at = datetime(2026, 8, 2, 10, 2, tzinfo=timezone.utc)
    validity_start = datetime(2026, 8, 2, 10, 3, tzinfo=timezone.utc)
    validity_end = datetime(2026, 8, 2, 10, 4, tzinfo=timezone.utc)
    created_at = datetime(2026, 8, 2, 10, 5, tzinfo=timezone.utc)
    verified_at = datetime(2026, 8, 2, 10, 6, tzinfo=timezone.utc)

    temporal = EventHistoryTemporalTrace(
        event_occurred_at=event_occurred_at,
        observed_at=observed_at,
        ingested_at=ingested_at,
        validity_start=validity_start,
        validity_end=validity_end,
        created_at=created_at,
        verified_at=verified_at,
    )

    public_temporal = wmp._coordinator_temporal_to_public_temporal(temporal)

    assert set(wmp.WorldMemoryTemporalTrace.model_fields) == {
        "event_occurred_at",
        "observed_at",
        "ingested_at",
        "validity_start",
        "validity_end",
        "created_at",
        "verified_at",
    }
    assert public_temporal.event_occurred_at == event_occurred_at
    assert public_temporal.observed_at == observed_at
    assert public_temporal.ingested_at == ingested_at
    assert public_temporal.validity_start == validity_start
    assert public_temporal.validity_end == validity_end
    assert public_temporal.created_at == created_at
    assert public_temporal.verified_at == verified_at


async def test_public_query_path_preserves_all_seven_temporal_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public query response item carries all seven coordinator temporal fields.

    Exercises the full public path end to end: the coordinator response entry
    supplies a complete temporal trace and ``world_memory_query`` maps every
    field onto the public ``WorldMemoryTemporalTrace``.
    """
    cutoff = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
    observed_at = datetime(2026, 8, 2, 10, 1, tzinfo=timezone.utc)
    ingested_at = datetime(2026, 8, 2, 10, 2, tzinfo=timezone.utc)
    validity_start = datetime(2026, 8, 2, 10, 3, tzinfo=timezone.utc)
    validity_end = datetime(2026, 8, 2, 10, 4, tzinfo=timezone.utc)
    created_at = datetime(2026, 8, 2, 10, 5, tzinfo=timezone.utc)
    verified_at = datetime(2026, 8, 2, 10, 6, tzinfo=timezone.utc)

    envelope = build_world_memory_envelope(
        WorldObservation(
            id=uuid4(),
            content="Historic road closure",
            observed_at=observed_at,
            ingested_at=ingested_at,
            source="rescue-sensor",
            raw_evidence_ref="sensor://road-closure/1000",
        ),
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="elasticsearch",
    )
    entry = PreparedEventHistoryEntry(
        envelope=envelope,
        kind="observation",
        id=uuid4(),
        content="Historic road closure",
        evidence_ids=(),
        temporal=EventHistoryTemporalTrace(
            event_occurred_at=cutoff,
            observed_at=observed_at,
            ingested_at=ingested_at,
            validity_start=validity_start,
            validity_end=validity_end,
            created_at=created_at,
            verified_at=verified_at,
        ),
    )

    monkeypatch.setattr(
        client_module,
        "world_memory_event_history_coordinator_configured",
        lambda: True,
    )

    async def fake_query_coordinator(
        coordinator_request: EventHistoryCoordinatorRequest,
    ) -> EventHistoryCoordinatorResponse:
        return EventHistoryCoordinatorResponse(
            entries=(entry,),
            candidate_count=1,
            candidate_latency_ms=3.0,
        )

    monkeypatch.setattr(
        client_module,
        "query_world_memory_event_history_coordinator",
        fake_query_coordinator,
    )

    request = wmp.WorldMemoryQueryRequest(
        query="road closure",
        event_time_lte=cutoff,
        kind="observation",
        aggregate_id=None,
        # This test is about temporal-field mapping, not evidence; the entry
        # is intentionally evidence-less, so evidence is not required.
        require_evidence=False,
        max_items=50,
        token_budget=4000,
    )
    response = await wmp.world_memory_query(request)

    assert response.status == "ok"
    assert len(response.items) == 1
    public_temporal = response.items[0].temporal
    assert public_temporal.event_occurred_at == cutoff
    assert public_temporal.observed_at == observed_at
    assert public_temporal.ingested_at == ingested_at
    assert public_temporal.validity_start == validity_start
    assert public_temporal.validity_end == validity_end
    assert public_temporal.created_at == created_at
    assert public_temporal.verified_at == verified_at
