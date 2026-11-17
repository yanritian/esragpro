from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Final, Protocol, final, override
from uuid import UUID

from elastic_transport import ObjectApiResponse
from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import ApiError, TransportError

from app.memory_core.repositories.world_memory_authority import StoredEnvelope, has_evidence
from app.memory_core.world_memory_projection._write_codec import JsonObject, JsonValue

_DEFAULT_INDEX_NAME: Final = "world-memory-events-v2"
_MAX_CANDIDATES: Final = 100


class _ElasticsearchClient(Protocol):
    async def search(self, *, index: str, body: JsonObject) -> Mapping[str, JsonValue]: ...


class _ExactEnvelopeAuthority(Protocol):
    async def resolve_exact_envelopes(
        self, envelope_ids: Sequence[UUID]
    ) -> list[StoredEnvelope]: ...


@dataclass(frozen=True, slots=True)
class EventHistoryCandidateInfrastructureError(Exception):
    detail: str
    category: str = "unavailable"

    def __post_init__(self) -> None:
        Exception.__init__(self, self.__str__())

    @override
    def __str__(self) -> str:
        return f"event history candidate infrastructure error [{self.category}]: {self.detail}"


@dataclass(frozen=True, slots=True)
class EventHistoryCandidateQuery:
    query: str
    event_time_lte: datetime | None = None
    kind: str | None = None
    aggregate_id: UUID | None = None
    require_evidence: bool = False
    max_items: int = 50


@dataclass(frozen=True, slots=True)
class EventHistoryCandidateProposal:
    envelope_id: UUID
    score: float


@dataclass(frozen=True, slots=True)
class EventHistoryCandidateSourceResult:
    proposals: tuple[EventHistoryCandidateProposal, ...]
    latency_ms: float
    error: EventHistoryCandidateInfrastructureError | None = None


class EventHistoryCandidateSource(Protocol):
    async def propose_candidates(
        self, query: EventHistoryCandidateQuery
    ) -> EventHistoryCandidateSourceResult: ...


@dataclass(frozen=True, slots=True)
class EventHistoryQueryItem:
    envelope_id: UUID
    authority: StoredEnvelope
    rank: int
    score: float


@dataclass(frozen=True, slots=True)
class EventHistoryQueryResult:
    items: tuple[EventHistoryQueryItem, ...]
    source: EventHistoryCandidateSourceResult | None
    latency_ms: float
    authority_latency_ms: float = 0.0
    query_unattributed_ms: float = 0.0


def _bounded_size(max_items: int) -> int:
    return min(max(1, max_items), _MAX_CANDIDATES)


def _normalized_cutoff(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("event_time_lte must be timezone-aware")
    return value.astimezone(timezone.utc)


def _build_elasticsearch_body(query: EventHistoryCandidateQuery) -> JsonObject:
    filters: list[JsonValue] = [{"exists": {"field": "authority_envelope_id"}}]
    if query.kind is not None:
        filters.append({"term": {"object_kind": query.kind}})
    if query.aggregate_id is not None:
        filters.append({"term": {"aggregate_id": str(query.aggregate_id)}})
    if query.require_evidence:
        filters.append({"term": {"has_evidence": True}})
    cutoff = _normalized_cutoff(query.event_time_lte)
    if cutoff is not None:
        filters.append({"range": {"occurred_at": {"lte": cutoff.isoformat()}}})
    return {
        "query": {
            "bool": {
                "must": [{"match": {"content": {"query": query.query, "operator": "and"}}}],
                "filter": filters,
            }
        },
        "_source": ["authority_envelope_id"],
        "size": _bounded_size(query.max_items),
    }


def _response_body(
    response: Mapping[str, JsonValue] | ObjectApiResponse[dict[str, JsonValue]],
) -> Mapping[str, JsonValue]:
    if isinstance(response, ObjectApiResponse):
        return response.body
    return response


def _require_object(value: JsonValue | None, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise TypeError(f"event history candidate response field {label!r} is not an object")
    return value


def _map_elasticsearch_hits(
    response: Mapping[str, JsonValue] | ObjectApiResponse[dict[str, JsonValue]],
) -> tuple[EventHistoryCandidateProposal, ...]:
    body = _response_body(response)
    hits = _require_object(body.get("hits"), "hits")
    raw_hits = hits.get("hits")
    if not isinstance(raw_hits, list):
        raise TypeError("event history candidate response field 'hits.hits' is not a list")

    proposals: list[EventHistoryCandidateProposal] = []
    seen: set[UUID] = set()
    for raw_hit in raw_hits:
        hit = _require_object(raw_hit, "hits.hits[]")
        source = _require_object(hit.get("_source"), "hits.hits[]._source")
        raw_envelope_id = source.get("authority_envelope_id")
        if not isinstance(raw_envelope_id, str):
            raise TypeError("event history candidate hit has no authority_envelope_id")
        try:
            envelope_id = UUID(raw_envelope_id)
        except ValueError as exc:
            raise ValueError(
                "event history candidate hit has malformed authority_envelope_id"
            ) from exc
        if envelope_id in seen:
            continue
        seen.add(envelope_id)
        proposals.append(
            EventHistoryCandidateProposal(
                envelope_id=envelope_id,
                score=1.0 / (len(proposals) + 1),
            )
        )
    return tuple(proposals)


def _infrastructure_error(error: BaseException) -> EventHistoryCandidateInfrastructureError:
    category = "timeout" if isinstance(error, TimeoutError) else "unavailable"
    return EventHistoryCandidateInfrastructureError(
        detail=str(error) or type(error).__name__,
        category=category,
    )


def _matches_authority_scope(
    stored: StoredEnvelope,
    query: EventHistoryCandidateQuery,
) -> bool:
    cutoff = _normalized_cutoff(query.event_time_lte)
    if cutoff is not None and stored.occurred_at > cutoff:
        return False
    if query.kind is not None and stored.kind != query.kind:
        return False
    if query.aggregate_id is not None and stored.aggregate_id != query.aggregate_id:
        return False
    return not query.require_evidence or has_evidence(stored)


@final
class EventHistoryElasticsearchCandidateSource:
    def __init__(
        self,
        client: AsyncElasticsearch | _ElasticsearchClient,
        *,
        index_name: str = _DEFAULT_INDEX_NAME,
    ) -> None:
        self._client: AsyncElasticsearch | _ElasticsearchClient = client
        self._index_name: str = index_name

    async def propose_candidates(
        self, query: EventHistoryCandidateQuery
    ) -> EventHistoryCandidateSourceResult:
        body = _build_elasticsearch_body(query)
        started = perf_counter()
        try:
            response = await self._client.search(index=self._index_name, body=body)
        except (ApiError, TransportError, OSError) as exc:
            return EventHistoryCandidateSourceResult(
                proposals=(),
                latency_ms=(perf_counter() - started) * 1000.0,
                error=_infrastructure_error(exc),
            )
        try:
            proposals = _map_elasticsearch_hits(response)
        except (TypeError, ValueError) as exc:
            return EventHistoryCandidateSourceResult(
                proposals=(),
                latency_ms=(perf_counter() - started) * 1000.0,
                error=EventHistoryCandidateInfrastructureError(
                    detail=str(exc),
                    category="malformed_response",
                ),
            )
        return EventHistoryCandidateSourceResult(
            proposals=proposals,
            latency_ms=(perf_counter() - started) * 1000.0,
        )


@final
class EventHistoryQueryCoordinator:
    def __init__(
        self,
        authority_repository: _ExactEnvelopeAuthority,
        source: EventHistoryCandidateSource,
    ) -> None:
        self._authority_repository: _ExactEnvelopeAuthority = authority_repository
        self._source: EventHistoryCandidateSource = source

    async def query(self, query: EventHistoryCandidateQuery) -> EventHistoryQueryResult:
        started = perf_counter()
        _ = _normalized_cutoff(query.event_time_lte)
        if not query.query.strip():
            return EventHistoryQueryResult(items=(), source=None, latency_ms=_elapsed_ms(started))
        source_result = await self._propose(query)
        if source_result.error is not None:
            return EventHistoryQueryResult(
                items=(),
                source=source_result,
                latency_ms=_elapsed_ms(started),
            )
        proposals = source_result.proposals[: max(query.max_items, 0)]
        authority_started = perf_counter()
        rows = await self._authority_repository.resolve_exact_envelopes(
            tuple(proposal.envelope_id for proposal in proposals)
        )
        authority_latency_ms = _elapsed_ms(authority_started)
        by_envelope_id = {row.id: row for row in rows}
        items = tuple(
            EventHistoryQueryItem(
                envelope_id=proposal.envelope_id,
                authority=stored,
                rank=rank,
                score=proposal.score,
            )
            for rank, proposal in enumerate(proposals, start=1)
            if (stored := by_envelope_id.get(proposal.envelope_id)) is not None
            and _matches_authority_scope(stored, query)
        )
        latency_ms = _elapsed_ms(started)
        query_unattributed_ms = max(
            0.0,
            latency_ms - max(0.0, source_result.latency_ms) - authority_latency_ms,
        )
        return EventHistoryQueryResult(
            items=items,
            source=source_result,
            latency_ms=latency_ms,
            authority_latency_ms=authority_latency_ms,
            query_unattributed_ms=query_unattributed_ms,
        )

    async def _propose(
        self,
        query: EventHistoryCandidateQuery,
    ) -> EventHistoryCandidateSourceResult:
        started = perf_counter()
        try:
            return await self._source.propose_candidates(query)
        except EventHistoryCandidateInfrastructureError as exc:
            return EventHistoryCandidateSourceResult(
                proposals=(),
                error=exc,
                latency_ms=_elapsed_ms(started),
            )
        except (TimeoutError, OSError) as exc:
            return EventHistoryCandidateSourceResult(
                proposals=(),
                error=_infrastructure_error(exc),
                latency_ms=_elapsed_ms(started),
            )


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000.0


__all__ = [
    "EventHistoryCandidateInfrastructureError",
    "EventHistoryCandidateProposal",
    "EventHistoryCandidateQuery",
    "EventHistoryCandidateSource",
    "EventHistoryCandidateSourceResult",
    "EventHistoryElasticsearchCandidateSource",
    "EventHistoryQueryCoordinator",
    "EventHistoryQueryItem",
    "EventHistoryQueryResult",
]
