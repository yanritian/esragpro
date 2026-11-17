from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
import importlib
from typing import Any, ClassVar, Self
from uuid import UUID, uuid4

import anyio
import anyio.lowlevel
import pytest

from app.api import world_memory_plane as wmp
from app.memory_core import world_memory_event_history_runtime as runtime
from app.memory_core.repositories.world_memory_authority import StoredEnvelope
from app.memory_core.world_memory_event_history_coordinator import (
    EventHistoryCoordinatorRequest,
    EventHistoryCoordinatorResponse,
)
from app.memory_core.world_memory_event_history_coordinator_app import (
    world_memory_event_history_query,
    world_memory_event_history_stats,
)
from app.memory_core.world_memory_event_history_diagnostics import (
    EventHistoryCallerTiming,
    EventHistoryPreparedTiming,
    EventHistoryTimingCollector,
    EventHistoryTimingRecorder,
)
from app.memory_core.world_memory_event_history_readiness import (
    EventHistoryReadiness,
    EventHistoryReadinessStatus,
)


def _request(query: str = "historical") -> EventHistoryCoordinatorRequest:
    return EventHistoryCoordinatorRequest(
        query=query,
        event_time_lte=datetime(2026, 8, 17, tzinfo=timezone.utc),
        require_evidence=True,
        max_items=10,
    )


def _stored() -> StoredEnvelope:
    occurred_at = datetime(2026, 8, 17, tzinfo=timezone.utc)
    return StoredEnvelope(
        id=uuid4(),
        event_type="observation.ingested",
        schema_version=1,
        aggregate_id=uuid4(),
        projection_target="elasticsearch",
        idempotency_key="p10-as-test",
        occurred_at=occurred_at,
        kind="observation",
        envelope={"world_memory_snapshot": {"raw_evidence_ref": "test://evidence"}},
        created_at=occurred_at,
    )


@pytest.mark.anyio
async def test_authority_resolution_reports_its_exact_duration() -> None:
    from app.memory_core.world_memory_event_history_candidates import (
        EventHistoryCandidateProposal,
        EventHistoryCandidateQuery,
        EventHistoryCandidateSourceResult,
        EventHistoryQueryCoordinator,
    )

    stored = _stored()

    class Source:
        async def propose_candidates(
            self, query: EventHistoryCandidateQuery
        ) -> EventHistoryCandidateSourceResult:
            del query
            return EventHistoryCandidateSourceResult(
                proposals=(EventHistoryCandidateProposal(stored.id, 1.0),),
                latency_ms=2.0,
            )

    class Authority:
        async def resolve_exact_envelopes(
            self, envelope_ids: Sequence[UUID]
        ) -> list[StoredEnvelope]:
            await anyio.lowlevel.checkpoint()
            del envelope_ids
            return [stored]

    result = await EventHistoryQueryCoordinator(Authority(), Source()).query(
        EventHistoryCandidateQuery(
            query="historical",
            event_time_lte=stored.occurred_at,
            require_evidence=True,
        )
    )

    assert result.items
    assert result.authority_latency_ms >= 0.0
    assert result.query_unattributed_ms >= 0.0


@pytest.mark.anyio
async def test_pg_event_history_query_reports_timing_without_changing_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((0.000, 0.010, 0.012, 0.014, 0.016, 0.018))
    monkeypatch.setattr(wmp, "perf_counter", lambda: next(clock))
    stored = _stored()
    session = _Session()
    factory = _SessionFactory(session)
    client = _Client(stored)
    recorder = EventHistoryTimingRecorder()
    ready = EventHistoryReadiness(
        EventHistoryReadinessStatus.READY,
        authority_count=1,
        history_count=1,
    )

    async def fake_get_client() -> _Client:
        return client

    async def fake_assess(_assessment: Any) -> runtime.EventHistoryReadiness:
        return ready

    monkeypatch.setattr(runtime, "get_world_memory_event_history_client", fake_get_client)
    monkeypatch.setattr(runtime, "assess_world_memory_event_history_readiness", fake_assess)
    live_mps = importlib.import_module("app.memory_core.memory_plane_session")
    memory_core = importlib.import_module("app.memory_core")
    monkeypatch.setattr(memory_core, "memory_plane_session", live_mps)
    monkeypatch.setattr(live_mps, "_state", lambda: {"session_factory": factory})

    async def unexpected_init(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("timing test must use its injected session factory")

    monkeypatch.setattr(live_mps, "init_session_factory", unexpected_init)

    from app.memory_core.repositories import world_memory_authority as authority_module

    monkeypatch.setattr(authority_module, "WorldMemoryAuthorityRepository", _Repository)
    _Repository.rows = [stored]

    result = await wmp._pg_event_history_query(
        query="historical",
        event_time_lte=stored.occurred_at,
        kind=None,
        aggregate_id=None,
        require_evidence=True,
        max_items=10,
        diagnostic_sink=recorder,
    )

    assert len(result) == 4
    timing = recorder.retrieval_timing
    assert timing is not None
    assert timing.retrieval_ms >= 0.0
    assert timing.readiness_ms >= 0.0
    assert timing.retrieval_ms == pytest.approx(4.0)
    assert timing.candidate_ms >= 0.0
    assert timing.authority_ms >= 0.0
    assert timing.query_unattributed_ms >= 0.0


@pytest.mark.anyio
async def test_identical_concurrent_calls_record_one_leader_and_one_joiner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = await world_memory_event_history_stats()
    release = anyio.Event()

    async def prepare(_request: EventHistoryCoordinatorRequest) -> EventHistoryCoordinatorResponse:
        await release.wait()
        return EventHistoryCoordinatorResponse()

    monkeypatch.setattr(
        "app.memory_core.world_memory_event_history_coordinator_app._prepare_event_history_response",
        prepare,
    )

    results: list[EventHistoryCoordinatorResponse] = []

    async def call() -> None:
        results.append(await world_memory_event_history_query(_request("same")))

    async with anyio.create_task_group() as group:
        group.start_soon(call)
        await anyio.lowlevel.checkpoint()
        group.start_soon(call)
        await anyio.lowlevel.checkpoint()
        release.set()

    after = await world_memory_event_history_stats()
    assert results == [EventHistoryCoordinatorResponse(), EventHistoryCoordinatorResponse()]
    assert after.leader_count - before.leader_count == 1
    assert after.join_count - before.join_count == 1
    samples = after.timing_samples[before.last_sequence :]
    assert {sample.role for sample in samples} == {"leader", "joiner"}
    for sample in samples:
        assert sample.caller_wait_ms >= 0.0
        assert sample.leader_execution_ms >= 0.0
        assert sample.retrieval_ms >= 0.0
        assert sample.preparation_ms >= 0.0


def test_timing_collector_caps_recent_samples_and_sequences_monotonically() -> None:
    collector = EventHistoryTimingCollector()
    timing = EventHistoryCallerTiming(role="joiner")

    for _ in range(2050):
        collector.record(timing)

    snapshot = collector.snapshot()
    assert len(snapshot.timing_samples) == 2048
    assert snapshot.last_sequence == 2050
    assert [sample.sequence for sample in snapshot.timing_samples] == list(range(3, 2051))


def test_completed_leader_duration_is_shared_after_delayed_caller_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((100.000, 100.004, 100.120, 100.140, 100.160))
    monkeypatch.setattr(
        "app.memory_core.world_memory_event_history_diagnostics.perf_counter",
        lambda: next(clock),
    )
    timing = EventHistoryPreparedTiming()
    timing.mark_complete()
    collector = EventHistoryTimingCollector()

    timing.record_completed("leader", 100.000, collector)
    timing.record_completed("joiner", 100.005, collector)

    samples = collector.snapshot().timing_samples
    assert [sample.leader_execution_ms for sample in samples] == pytest.approx([4.0, 4.0])


def test_public_response_field_sets_remain_unchanged() -> None:
    from app.api.world_memory_plane import WorldMemoryQueryResponse

    assert set(EventHistoryCoordinatorResponse.model_fields) == {
        "entries",
        "candidate_count",
        "candidate_latency_ms",
        "refusal_reason",
    }
    assert set(WorldMemoryQueryResponse.model_fields) == {
        "query_id",
        "items",
        "envelopes",
        "token_budget",
        "token_used",
        "status",
        "refusal_reason",
        "candidate_sources",
        "latency_ms",
    }


@pytest.mark.anyio
async def test_refusal_remains_fail_closed_and_records_a_completed_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(_request: EventHistoryCoordinatorRequest) -> EventHistoryCoordinatorResponse:
        return EventHistoryCoordinatorResponse(refusal_reason="insufficient_evidence")

    monkeypatch.setattr(
        "app.memory_core.world_memory_event_history_coordinator_app._prepare_event_history_response",
        refuse,
    )
    response = await world_memory_event_history_query(_request("refused"))

    assert response.entries == ()
    assert response.refusal_reason == "insufficient_evidence"
    stats = await world_memory_event_history_stats()
    assert stats.last_sequence >= 1


class _Session:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def close(self) -> None:
        return None


class _SessionFactory:
    def __init__(self, session: _Session) -> None:
        self._session = session

    def __call__(self) -> _Session:
        return self._session


class _Client:
    def __init__(self, stored: StoredEnvelope) -> None:
        self._stored = stored

    async def search(self, *, index: str, body: dict[str, Any]) -> dict[str, Any]:
        del index, body
        return {"hits": {"hits": [{"_source": {"authority_envelope_id": str(self._stored.id)}}]}}


class _Repository:
    rows: ClassVar[list[StoredEnvelope]] = []

    def __init__(self, _session: _Session) -> None:
        return None

    async def resolve_exact_envelopes(self, ids: tuple[UUID, ...]) -> list[StoredEnvelope]:
        return [row for row in self.rows if row.id in ids]
