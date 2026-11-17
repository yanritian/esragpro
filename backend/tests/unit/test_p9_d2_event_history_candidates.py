from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import final
from uuid import UUID, uuid4

import pytest
from elasticsearch.exceptions import ApiError

from app.memory_core.repositories.world_memory_authority import StoredEnvelope
from app.memory_core.world_memory_event_history_candidates import (
    EventHistoryCandidateInfrastructureError,
    EventHistoryCandidateProposal,
    EventHistoryCandidateQuery,
    EventHistoryCandidateSourceResult,
    EventHistoryElasticsearchCandidateSource,
    EventHistoryQueryCoordinator,
)
from app.memory_core.world_memory_projection._write_codec import JsonObject, JsonValue


@final
class _FakeElasticsearch:
    def __init__(self, response: Mapping[str, JsonValue]) -> None:
        self.response: Mapping[str, JsonValue] = response
        self.index: str | None = None
        self.body: JsonObject | None = None

    async def search(self, *, index: str, body: JsonObject) -> Mapping[str, JsonValue]:
        self.index = index
        self.body = body
        return self.response


@final
class _RejectedElasticsearchApi(ApiError):
    def __init__(self) -> None:
        Exception.__init__(self, "event history unavailable")

    def __str__(self) -> str:
        return "event history unavailable"


@final
class _UnavailableElasticsearch:
    async def search(self, *, index: str, body: JsonObject) -> Mapping[str, JsonValue]:
        del index, body
        raise _RejectedElasticsearchApi()


@final
class _FakeSource:
    def __init__(
        self,
        result: EventHistoryCandidateSourceResult,
        *,
        expected_query: str = "historic bearing",
    ) -> None:
        self._result: EventHistoryCandidateSourceResult = result
        self._expected_query: str = expected_query
        self.calls: int = 0

    async def propose_candidates(
        self, query: EventHistoryCandidateQuery
    ) -> EventHistoryCandidateSourceResult:
        self.calls += 1
        assert query.query == self._expected_query
        return self._result


@final
class _ExactAuthority:
    def __init__(self, rows: Sequence[StoredEnvelope]) -> None:
        self._rows: dict[UUID, StoredEnvelope] = {row.id: row for row in rows}
        self.calls: list[tuple[UUID, ...]] = []

    async def resolve_exact_envelopes(self, envelope_ids: Sequence[UUID]) -> list[StoredEnvelope]:
        self.calls.append(tuple(envelope_ids))
        return [self._rows[envelope_id] for envelope_id in reversed(envelope_ids)]


def _stored(
    envelope_id: UUID,
    *,
    object_id: UUID,
    occurred_at: datetime,
    content: str,
) -> StoredEnvelope:
    return StoredEnvelope(
        id=envelope_id,
        object_id=object_id,
        event_type="world.observation.ingested",
        schema_version=1,
        aggregate_id=UUID("00000000-0000-0000-0000-000000000001"),
        projection_target="elasticsearch",
        idempotency_key=str(envelope_id),
        occurred_at=occurred_at,
        kind="observation",
        envelope={"world_memory_snapshot": {"id": str(object_id), "content": content}},
        created_at=occurred_at,
    )


@pytest.mark.asyncio
async def test_event_history_adapter_uses_v2_envelope_ids_and_inclusive_cutoff() -> None:
    first, second, aggregate_id = uuid4(), uuid4(), uuid4()
    cutoff = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    client = _FakeElasticsearch(
        {
            "hits": {
                "hits": [
                    {"_source": {"authority_envelope_id": str(first)}},
                    {"_source": {"authority_envelope_id": str(second)}},
                ]
            }
        }
    )

    result = await EventHistoryElasticsearchCandidateSource(client).propose_candidates(
        EventHistoryCandidateQuery(
            query="historic bearing",
            event_time_lte=cutoff,
            kind="observation",
            aggregate_id=aggregate_id,
            max_items=7,
        )
    )

    assert client.index == "world-memory-events-v2"
    assert client.body == {
        "query": {
            "bool": {
                "must": [
                    {"match": {"content": {"query": "historic bearing", "operator": "and"}}}
                ],
                "filter": [
                    {"exists": {"field": "authority_envelope_id"}},
                    {"term": {"object_kind": "observation"}},
                    {"term": {"aggregate_id": str(aggregate_id)}},
                    {"range": {"occurred_at": {"lte": "2026-08-02T12:00:00+00:00"}}},
                ],
            }
        },
        "_source": ["authority_envelope_id"],
        "size": 7,
    }
    assert [proposal.envelope_id for proposal in result.proposals] == [first, second]
    assert [proposal.score for proposal in result.proposals] == [1.0, 0.5]


@pytest.mark.asyncio
async def test_event_history_adapter_refuses_missing_or_malformed_authority_markers() -> None:
    missing = _FakeElasticsearch(
        {"hits": {"hits": [{"_source": {"envelope_id": str(uuid4())}}]}}
    )
    malformed = _FakeElasticsearch(
        {"hits": {"hits": [{"_source": {"authority_envelope_id": "bad"}}]}}
    )

    missing_result = await EventHistoryElasticsearchCandidateSource(missing).propose_candidates(
        EventHistoryCandidateQuery(query="historic bearing")
    )
    malformed_result = await EventHistoryElasticsearchCandidateSource(malformed).propose_candidates(
        EventHistoryCandidateQuery(query="historic bearing")
    )

    assert missing_result.proposals == ()
    assert missing_result.error is not None
    assert missing_result.error.category == "malformed_response"
    assert malformed_result.proposals == ()
    assert malformed_result.error is not None
    assert malformed_result.error.category == "malformed_response"


@pytest.mark.asyncio
async def test_event_history_adapter_converts_elasticsearch_api_failures_to_typed_unavailability(
) -> None:
    result = await EventHistoryElasticsearchCandidateSource(
        _UnavailableElasticsearch()
    ).propose_candidates(EventHistoryCandidateQuery(query="historic bearing"))

    assert isinstance(result.error, EventHistoryCandidateInfrastructureError)
    assert result.error.category == "unavailable"


@pytest.mark.asyncio
async def test_event_history_adapter_filters_evidence_before_the_rank_cap() -> None:
    client = _FakeElasticsearch({"hits": {"hits": []}})

    _ = await EventHistoryElasticsearchCandidateSource(client).propose_candidates(
        EventHistoryCandidateQuery(
            query="historic bearing",
            require_evidence=True,
            max_items=1,
        )
    )

    assert client.body is not None
    filters = client.body["query"]["bool"]["filter"]
    assert filters == [
        {"exists": {"field": "authority_envelope_id"}},
        {"term": {"has_evidence": True}},
    ]
    assert client.body["size"] == 1


@pytest.mark.asyncio
async def test_coordinator_rehydrates_exact_historical_envelopes_in_candidate_order() -> None:
    historic_id, current_id, other_id, object_id = uuid4(), uuid4(), uuid4(), uuid4()
    historic_time = datetime(2026, 8, 1, tzinfo=timezone.utc)
    current_time = datetime(2026, 8, 3, tzinfo=timezone.utc)
    historic = _stored(
        historic_id,
        object_id=object_id,
        occurred_at=historic_time,
        content="historic",
    )
    current = _stored(current_id, object_id=object_id, occurred_at=current_time, content="current")
    other = _stored(other_id, object_id=uuid4(), occurred_at=historic_time, content="other")
    source = _FakeSource(
        EventHistoryCandidateSourceResult(
            proposals=(
                EventHistoryCandidateProposal(envelope_id=historic_id, score=0.9),
                EventHistoryCandidateProposal(envelope_id=other_id, score=0.8),
            ),
            latency_ms=1.0,
        )
    )
    authority = _ExactAuthority((historic, current, other))
    coordinator = EventHistoryQueryCoordinator(authority, source)

    result = await coordinator.query(
        EventHistoryCandidateQuery(
            query="historic bearing",
            event_time_lte=datetime(2026, 8, 2, tzinfo=timezone.utc),
        )
    )

    assert authority.calls == [(historic_id, other_id)]
    assert [item.authority.id for item in result.items] == [historic_id, other_id]
    assert [item.rank for item in result.items] == [1, 2]
    assert result.items[0].authority.envelope["world_memory_snapshot"]["content"] == "historic"


@pytest.mark.asyncio
async def test_coordinator_excludes_evidence_less_authority_when_evidence_required() -> None:
    """An evidence-less authority row fails closed even when the source proposes it.

    Regression for the P10-M evidence gap: an evidence-less positive result.
    The candidate source may propose an envelope id, but the authority
    rehydration must exclude the row when ``require_evidence=true`` so the
    query surface reports no items (and the caller refuses) instead of
    returning an evidence-less positive.
    """
    evidence_less_id, object_id = uuid4(), uuid4()
    occurred_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    evidence_less = _stored(
        evidence_less_id,
        object_id=object_id,
        occurred_at=occurred_at,
        content="p10b rehearsal telemetry reading shadow",
    )
    source = _FakeSource(
        EventHistoryCandidateSourceResult(
            proposals=(
                EventHistoryCandidateProposal(envelope_id=evidence_less_id, score=0.9),
            ),
            latency_ms=1.0,
        ),
        expected_query="p10b rehearsal telemetry reading shadow",
    )
    authority = _ExactAuthority((evidence_less,))
    coordinator = EventHistoryQueryCoordinator(authority, source)

    result = await coordinator.query(
        EventHistoryCandidateQuery(
            query="p10b rehearsal telemetry reading shadow",
            event_time_lte=datetime(2026, 8, 2, tzinfo=timezone.utc),
            require_evidence=True,
        )
    )

    assert result.items == ()


@pytest.mark.asyncio
async def test_coordinator_keeps_evidence_backed_authority_when_evidence_required() -> None:
    """A legitimate evidence-backed positive remains readable (P10-S).

    An authority row carrying ``raw_evidence_ref`` must survive the authority
    rehydration scope check when ``require_evidence=true``, so a positive
    evidence-backed query still returns its items.
    """
    evidence_id, object_id = uuid4(), uuid4()
    occurred_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    evidence_backed = StoredEnvelope(
        id=evidence_id,
        object_id=object_id,
        event_type="world.observation.ingested",
        schema_version=1,
        aggregate_id=UUID("00000000-0000-0000-0000-000000000001"),
        projection_target="elasticsearch",
        idempotency_key=str(evidence_id),
        occurred_at=occurred_at,
        kind="observation",
        envelope={
            "world_memory_snapshot": {
                "id": str(object_id),
                "content": "p10b rehearsal telemetry reading 00",
                "raw_evidence_ref": "p10b-evidence-00",
            }
        },
        created_at=occurred_at,
    )
    source = _FakeSource(
        EventHistoryCandidateSourceResult(
            proposals=(
                EventHistoryCandidateProposal(envelope_id=evidence_id, score=0.9),
            ),
            latency_ms=1.0,
        ),
        expected_query="p10b rehearsal telemetry reading",
    )
    authority = _ExactAuthority((evidence_backed,))
    coordinator = EventHistoryQueryCoordinator(authority, source)

    result = await coordinator.query(
        EventHistoryCandidateQuery(
            query="p10b rehearsal telemetry reading",
            event_time_lte=datetime(2026, 8, 2, tzinfo=timezone.utc),
            require_evidence=True,
        )
    )

    assert [item.authority.id for item in result.items] == [evidence_id]
