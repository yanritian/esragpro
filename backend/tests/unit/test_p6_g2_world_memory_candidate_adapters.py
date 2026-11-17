from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest
from neo4j.exceptions import ServiceUnavailable

from app.memory_core.world_memory_candidate_adapters import (
    WORLD_MEMORY_NEO4J_FULLTEXT_INDEX,
    WorldMemoryElasticsearchCandidateSource,
    WorldMemoryNeo4jCandidateSource,
)
from app.memory_core.world_memory_candidate_source import (
    WorldMemoryCandidateInfrastructureError,
    WorldMemoryCandidateQuery,
    WorldMemoryCandidateSourceKind,
)
from app.memory_core.world_memory_projection._write_codec import JsonObject, JsonValue


class _FakeElasticsearch:
    def __init__(self, response: Mapping[str, JsonValue]) -> None:
        self.response = response
        self.index: str | None = None
        self.body: JsonObject | None = None

    async def search(self, *, index: str, body: JsonObject) -> Mapping[str, JsonValue]:
        self.index = index
        self.body = body
        return self.response


class _FakeNeo4jResult:
    def __init__(self, records: list[dict[str, JsonValue]]) -> None:
        self.records = records

    async def _iterate(self) -> AsyncIterator[dict[str, JsonValue]]:
        for record in self.records:
            yield record

    def __aiter__(self) -> AsyncIterator[dict[str, JsonValue]]:
        return self._iterate()


class _FakeNeo4jSession:
    def __init__(self, result: _FakeNeo4jResult | None = None) -> None:
        self.result = result
        self.database: str | None = None
        self.query: str | None = None
        self.parameters: dict[str, JsonValue] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def run(self, query: str, **parameters: JsonValue) -> _FakeNeo4jResult:
        self.query = query
        self.parameters = parameters
        if self.result is None:
            raise AssertionError("result was not configured")
        return self.result


class _FakeNeo4jDriver:
    def __init__(self, session: _FakeNeo4jSession) -> None:
        self._session = session

    def session(self, *, database: str) -> _FakeNeo4jSession:
        self._session.database = database
        return self._session


def _query(
    *, max_items: int = 50, kind: str | None = None, aggregate_id: UUID | None = None
) -> WorldMemoryCandidateQuery:
    return WorldMemoryCandidateQuery(
        query="bearing",
        kind=kind,
        aggregate_id=aggregate_id,
        max_items=max_items,
    )


@pytest.mark.asyncio
async def test_elasticsearch_builds_bounded_lexical_body_and_maps_uuid_hits() -> None:
    first = uuid4()
    second = uuid4()
    client = _FakeElasticsearch(
        {
            "hits": {
                "hits": [
                    {"_source": {"id": str(first), "content": "hidden"}},
                    {"_source": {"id": "not-a-uuid"}},
                    {"_source": {"id": str(second)}},
                ]
            }
        }
    )

    result = await WorldMemoryElasticsearchCandidateSource(client).propose_candidates(
        _query(max_items=7)
    )

    assert client.index == "world-memory-history-v1"
    assert client.body == {
        "query": {
            "bool": {
                "must": [{"match": {"content": "bearing"}}],
                "filter": [],
            }
        },
        "_source": ["id"],
        "size": 7,
    }
    assert [proposal.object_id for proposal in result.proposals] == [first, second]
    assert [proposal.score for proposal in result.proposals] == [1.0, 0.5]
    assert result.kind is WorldMemoryCandidateSourceKind.ELASTICSEARCH


@pytest.mark.asyncio
async def test_elasticsearch_adds_exact_optional_filters() -> None:
    aggregate_id = uuid4()
    client = _FakeElasticsearch({"hits": {"hits": []}})

    await WorldMemoryElasticsearchCandidateSource(
        client, index_name="accepted-history"
    ).propose_candidates(_query(kind="observation", aggregate_id=aggregate_id))

    assert client.index == "accepted-history"
    assert client.body is not None
    assert client.body["query"] == {
        "bool": {
            "must": [{"match": {"content": "bearing"}}],
            "filter": [
                {"term": {"object_kind": "observation"}},
                {"term": {"projection_metadata.aggregate_id": str(aggregate_id)}},
            ],
        }
    }
    assert client.body["_source"] == ["id"]


@pytest.mark.asyncio
async def test_elasticsearch_transport_failure_is_typed() -> None:
    class _UnavailableElasticsearch(_FakeElasticsearch):
        async def search(self, *, index: str, body: JsonObject) -> Mapping[str, JsonValue]:
            raise ConnectionError("offline")

    source = WorldMemoryElasticsearchCandidateSource(
        _UnavailableElasticsearch({"hits": {"hits": []}})
    )

    with pytest.raises(WorldMemoryCandidateInfrastructureError) as error:
        await source.propose_candidates(_query())

    assert error.value.source_kind is WorldMemoryCandidateSourceKind.ELASTICSEARCH
    assert error.value.category == "unavailable"


@pytest.mark.asyncio
async def test_neo4j_query_selects_database_and_scores_direct_before_neighbor() -> None:
    direct_id = uuid4()
    neighbor_id = uuid4()
    session = _FakeNeo4jSession(
        _FakeNeo4jResult(
            [
                {"identity": str(neighbor_id), "direct": False, "seed_rank": 1},
                {"identity": str(direct_id), "direct": True, "seed_rank": 1},
                {"identity": "malformed", "direct": True, "seed_rank": 1},
            ]
        )
    )
    source = WorldMemoryNeo4jCandidateSource(_FakeNeo4jDriver(session), database="world-memory")

    result = await source.propose_candidates(_query(max_items=2))

    assert session.database == "world-memory"
    assert session.query is not None
    assert "CALL db.index.fulltext.queryNodes" in session.query
    assert "OPTIONAL MATCH (seed)-[]-(neighbor:WorldMemoryNode)" in session.query
    assert "LIMIT $candidate_limit" in session.query
    assert session.parameters == {
        "index_name": WORLD_MEMORY_NEO4J_FULLTEXT_INDEX,
        "query_text": "bearing",
        "seed_limit": 2,
        "candidate_limit": 2,
    }
    assert [proposal.object_id for proposal in result.proposals] == [direct_id, neighbor_id]
    assert [proposal.score for proposal in result.proposals] == [1.0, 0.5]
    assert all(not hasattr(proposal, "content") for proposal in result.proposals)


@pytest.mark.asyncio
async def test_neo4j_driver_failure_is_typed() -> None:
    class _UnavailableSession(_FakeNeo4jSession):
        async def run(self, query: str, **parameters: JsonValue) -> _FakeNeo4jResult:
            raise ServiceUnavailable("offline")

    source = WorldMemoryNeo4jCandidateSource(
        _FakeNeo4jDriver(_UnavailableSession()), database="neo4j"
    )

    with pytest.raises(WorldMemoryCandidateInfrastructureError) as error:
        await source.propose_candidates(_query())

    assert error.value.source_kind is WorldMemoryCandidateSourceKind.NEO4J
    assert error.value.category == "unavailable"


@pytest.mark.asyncio
async def test_candidate_response_shape_errors_surface() -> None:
    client = _FakeElasticsearch({"hits": []})

    with pytest.raises(TypeError):
        await WorldMemoryElasticsearchCandidateSource(client).propose_candidates(_query())
