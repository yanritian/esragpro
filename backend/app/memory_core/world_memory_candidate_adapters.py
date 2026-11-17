from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from time import perf_counter
from types import TracebackType
from typing import Final, Protocol, Self
from uuid import UUID

from elastic_transport import ObjectApiResponse
from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import TransportError
from neo4j import AsyncDriver
from neo4j.exceptions import DriverError, TransientError

from app.memory_core.world_memory_projection._write_codec import JsonObject, JsonValue

from .world_memory_candidate_source import (
    WorldMemoryCandidateInfrastructureError,
    WorldMemoryCandidateProposal,
    WorldMemoryCandidateQuery,
    WorldMemoryCandidateSource,
    WorldMemoryCandidateSourceKind,
    WorldMemoryCandidateSourceResult,
)

_MAX_CANDIDATES: Final[int] = 100
WORLD_MEMORY_NEO4J_FULLTEXT_INDEX: Final[str] = "world_memory_node_content_fulltext"


class _ElasticsearchClient(Protocol):
    async def search(self, *, index: str, body: JsonObject) -> Mapping[str, JsonValue]: ...


class _Neo4jRecord(Protocol):
    def __getitem__(self, key: str) -> JsonValue: ...


class _Neo4jResult(Protocol):
    def __aiter__(self) -> AsyncIterator[_Neo4jRecord]: ...


class _Neo4jSession(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def run(self, query: str, **parameters: JsonValue) -> _Neo4jResult: ...


class _Neo4jDriver(Protocol):
    def session(self, *, database: str) -> _Neo4jSession: ...


_NEO4J_QUERY: Final[str] = """
CALL db.index.fulltext.queryNodes($index_name, $query_text, {limit: $seed_limit})
YIELD node AS seed, score
WITH seed, score
ORDER BY score DESC, seed.identity ASC
WITH collect(seed) AS seeds
UNWIND CASE WHEN size(seeds) = 0 THEN [] ELSE range(0, size(seeds) - 1) END AS seed_index
WITH seeds[seed_index] AS seed, seed_index + 1 AS seed_rank
OPTIONAL MATCH (seed)-[]-(neighbor:WorldMemoryNode)
WITH seed, seed_rank, neighbor
UNWIND [
    {identity: seed.identity, direct: true, seed_rank: seed_rank},
    CASE WHEN neighbor IS NULL THEN NULL
         ELSE {identity: neighbor.identity, direct: false, seed_rank: seed_rank}
    END
] AS candidate
WITH candidate
WHERE candidate IS NOT NULL
RETURN candidate.identity AS identity,
       candidate.direct AS direct,
       candidate.seed_rank AS seed_rank
ORDER BY direct DESC, seed_rank ASC, identity ASC
LIMIT $candidate_limit
"""


def _bounded_size(max_items: int) -> int:
    return min(max(1, max_items), _MAX_CANDIDATES)


def _build_elasticsearch_body(query: WorldMemoryCandidateQuery) -> JsonObject:
    filters: list[JsonValue] = []
    if query.kind is not None:
        filters.append({"term": {"object_kind": query.kind}})
    if query.aggregate_id is not None:
        filters.append({"term": {"projection_metadata.aggregate_id": str(query.aggregate_id)}})
    bool_query: JsonObject = {
        "must": [{"match": {"content": query.query}}],
        "filter": filters,
    }
    return {
        "query": {"bool": bool_query},
        "_source": ["id"],
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
        raise TypeError(f"world-memory candidate response field {label!r} is not an object")
    return value


def _infrastructure_error(
    kind: WorldMemoryCandidateSourceKind,
    error: BaseException,
) -> WorldMemoryCandidateInfrastructureError:
    category = "timeout" if isinstance(error, TimeoutError) else "unavailable"
    detail = str(error) or type(error).__name__
    return WorldMemoryCandidateInfrastructureError(kind, detail=detail, category=category)


def _map_elasticsearch_hits(
    response: Mapping[str, JsonValue] | ObjectApiResponse[dict[str, JsonValue]],
) -> list[WorldMemoryCandidateProposal]:
    body = _response_body(response)
    hits = _require_object(body.get("hits"), "hits")
    raw_hits = hits.get("hits")
    if not isinstance(raw_hits, list):
        raise TypeError("world-memory candidate response field 'hits.hits' is not a list")

    proposals: list[WorldMemoryCandidateProposal] = []
    for raw_hit in raw_hits:
        hit = _require_object(raw_hit, "hits.hits[]")
        source = _require_object(hit.get("_source"), "hits.hits[]._source")
        raw_id = source.get("id")
        if not isinstance(raw_id, str):
            continue
        try:
            object_id = UUID(raw_id)
        except ValueError:
            continue
        rank = len(proposals) + 1
        proposals.append(WorldMemoryCandidateProposal(object_id=object_id, score=1.0 / rank))
    return proposals


def _map_neo4j_records(
    records: Sequence[tuple[UUID, bool, int]],
    limit: int,
) -> list[WorldMemoryCandidateProposal]:
    ranked: dict[UUID, tuple[float, bool, int]] = {}
    for object_id, direct, seed_rank in records:
        weight = 1.0 if direct else 0.5
        candidate = (weight / seed_rank, direct, seed_rank)
        previous = ranked.get(object_id)
        if previous is None or candidate > previous:
            ranked[object_id] = candidate
    ordered = sorted(
        ranked.items(),
        key=lambda item: (-item[1][0], not item[1][1], item[1][2], str(item[0])),
    )
    return [
        WorldMemoryCandidateProposal(object_id=object_id, score=details[0])
        for object_id, details in ordered[:limit]
    ]


class WorldMemoryElasticsearchCandidateSource:
    def __init__(
        self,
        client: AsyncElasticsearch | _ElasticsearchClient,
        *,
        index_name: str = "world-memory-history-v1",
    ) -> None:
        self._client = client
        self._index_name = index_name

    @property
    def kind(self) -> WorldMemoryCandidateSourceKind:
        return WorldMemoryCandidateSourceKind.ELASTICSEARCH

    async def propose_candidates(
        self, query: WorldMemoryCandidateQuery
    ) -> WorldMemoryCandidateSourceResult:
        body = _build_elasticsearch_body(query)
        started = perf_counter()
        try:
            if isinstance(self._client, AsyncElasticsearch):
                response = await self._client.search(index=self._index_name, body=body)
            else:
                response = await self._client.search(index=self._index_name, body=body)
        except (TransportError, OSError) as error:
            raise _infrastructure_error(self.kind, error) from error
        proposals = _map_elasticsearch_hits(response)
        return WorldMemoryCandidateSourceResult(
            kind=self.kind,
            proposals=tuple(proposals),
            error=None,
            latency_ms=(perf_counter() - started) * 1000.0,
        )


class WorldMemoryNeo4jCandidateSource:
    def __init__(self, driver: AsyncDriver | _Neo4jDriver, *, database: str = "neo4j") -> None:
        self._driver = driver
        self._database = database

    @property
    def kind(self) -> WorldMemoryCandidateSourceKind:
        return WorldMemoryCandidateSourceKind.NEO4J

    async def propose_candidates(
        self, query: WorldMemoryCandidateQuery
    ) -> WorldMemoryCandidateSourceResult:
        limit = _bounded_size(query.max_items)
        started = perf_counter()
        try:
            async with self._driver.session(database=self._database) as session:
                result = await session.run(
                    _NEO4J_QUERY,
                    index_name=WORLD_MEMORY_NEO4J_FULLTEXT_INDEX,
                    query_text=query.query,
                    seed_limit=limit,
                    candidate_limit=limit,
                )
                records: list[tuple[UUID, bool, int]] = []
                async for record in result:
                    raw_id = record["identity"]
                    if not isinstance(raw_id, str):
                        continue
                    try:
                        object_id = UUID(raw_id)
                    except ValueError:
                        continue
                    direct = record["direct"]
                    seed_rank = record["seed_rank"]
                    if (
                        not isinstance(direct, bool)
                        or not isinstance(seed_rank, int)
                        or isinstance(seed_rank, bool)
                    ):
                        raise TypeError("world-memory Neo4j candidate rank data is malformed")
                    if seed_rank < 1:
                        raise ValueError("world-memory Neo4j candidate rank must be positive")
                    records.append((object_id, direct, seed_rank))
        except (DriverError, TransientError, OSError) as error:
            raise _infrastructure_error(self.kind, error) from error
        proposals = _map_neo4j_records(records, limit)
        return WorldMemoryCandidateSourceResult(
            kind=self.kind,
            proposals=tuple(proposals),
            error=None,
            latency_ms=(perf_counter() - started) * 1000.0,
        )


ElasticsearchWorldMemoryCandidateSource: type[WorldMemoryCandidateSource] = (
    WorldMemoryElasticsearchCandidateSource
)
Neo4jWorldMemoryCandidateSource: type[WorldMemoryCandidateSource] = WorldMemoryNeo4jCandidateSource

__all__ = [
    "ElasticsearchWorldMemoryCandidateSource",
    "Neo4jWorldMemoryCandidateSource",
    "WORLD_MEMORY_NEO4J_FULLTEXT_INDEX",
    "WorldMemoryElasticsearchCandidateSource",
    "WorldMemoryNeo4jCandidateSource",
]
