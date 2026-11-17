"""Tests for P2 candidate adapters (ES + Redis) and P8-B graph adapter.

Proves:
1. ElasticsearchCandidateSource uses BM25 fallback when no embedding
   provider is supplied.
2. ElasticsearchCandidateSource uses hybrid search when an embedding
   provider is supplied.
3. Scope filters (project_id, session_id, etc.) propagate correctly.
4. Malformed UUIDs in projection data are silently dropped.
5. Results are bounded by query.max_items.
6. RedisHotStateCandidateSource returns bounded session candidates in
   hot-state order.
7. RedisHotStateCandidateSource returns empty when query has no
   session_id.
8. Invalid/malformed data does not become authoritative.
9. GraphCandidateSource passes dimension filters and active status to
   the reader.
10. GraphCandidateSource drops malformed UUIDs and is bounded.
11. GraphCandidateSource returns no authoritative content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence
from uuid import UUID, uuid4

import pytest

from app.memory_core.candidate_adapters import (
    ElasticsearchCandidateSource,
    GraphCandidateSource,
    RedisHotStateCandidateSource,
    _safe_parse_uuid,
)
from app.memory_core.candidate_source import CandidateSourceKind
from app.memory_core.unpermissioned_contracts import UnpermissionedMemoryQuery


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass
class FakeSearchResult:
    """Mimics ElasticsearchSearchResult for adapter tests."""
    memory_id: str
    bm25_rank: Optional[int] = None
    vector_rank: Optional[int] = None
    rrf_score: Optional[float] = None
    content: str = ""


@dataclass
class FakeHotItemRead:
    """Mimics HotItemRead for adapter tests."""
    snapshot: dict = field(default_factory=dict)
    pttl_ms: Optional[int] = 1000


@dataclass
class FakeHotSessionRead:
    """Mimics HotSessionRead for adapter tests."""
    items: list[FakeHotItemRead] = field(default_factory=list)
    session_pttl_ms: Optional[int] = 5000


class FakeESReader:
    """Fake ES reader that records calls and returns configured results."""

    def __init__(
        self,
        bm25_results: Optional[Sequence[FakeSearchResult]] = None,
        hybrid_results: Optional[Sequence[FakeSearchResult]] = None,
    ) -> None:
        self._bm25_results = list(bm25_results) if bm25_results else []
        self._hybrid_results = list(hybrid_results) if hybrid_results else []
        self.bm25_calls: list[dict] = []
        self.hybrid_calls: list[dict] = []

    async def search_bm25(self, *, query, size=10, filters=None):
        self.bm25_calls.append({
            "query": query,
            "size": size,
            "filters": filters,
        })
        return list(self._bm25_results)

    async def search_hybrid(self, *, query, query_vector, size=10,
                            candidate_window=20, rrf_k=60, filters=None):
        self.hybrid_calls.append({
            "query": query,
            "query_vector": query_vector,
            "size": size,
            "candidate_window": candidate_window,
            "rrf_k": rrf_k,
            "filters": filters,
        })
        return list(self._hybrid_results)


class FakeEmbeddingProvider:
    """Fake embedding provider that returns a fixed vector."""

    def __init__(self, vector: Optional[Sequence[float]] = None) -> None:
        self._vector = list(vector) if vector else [0.1, 0.2, 0.3]
        self.call_count = 0
        self.last_query: Optional[str] = None

    def embed_query(self, query: str) -> list[float]:
        self.call_count += 1
        self.last_query = query
        return list(self._vector)


class FakeRedisReader:
    """Fake Redis hot-state reader that returns configured session data."""

    def __init__(self, session_read=None):
        self._session_read = session_read or FakeHotSessionRead()
        self.calls = []

    async def list_session_items_typed(self, session_id, *, limit=20):
        self.calls.append({
            "session_id": session_id,
            "limit": limit,
        })
        return self._session_read


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_query(
    query_text: str = "test query",
    *,
    session_id=None,
    project_id=None,
    agent_id=None,
    knowledge_base_id=None,
    max_items: int = 10,
) -> UnpermissionedMemoryQuery:
    return UnpermissionedMemoryQuery(
        query=query_text,
        session_id=session_id,
        project_id=project_id,
        agent_id=agent_id,
        knowledge_base_id=knowledge_base_id,
        max_items=max_items,
    )


# ---------------------------------------------------------------------------
# Tests: Elasticsearch BM25 fallback
# ---------------------------------------------------------------------------

class TestElasticsearchBM25Fallback:
    """When no embedding provider is supplied, the ES adapter uses BM25."""

    @pytest.mark.asyncio
    async def test_bm25_fallback_called_without_embedding(self):
        """No embedding → BM25 search is used."""
        uid = uuid4()
        reader = FakeESReader(
            bm25_results=[FakeSearchResult(memory_id=str(uid), bm25_rank=1)],
        )
        source = ElasticsearchCandidateSource(reader)
        result = await source.propose_candidates(_make_query("hello"))

        assert len(reader.bm25_calls) == 1
        assert len(reader.hybrid_calls) == 0
        assert result.kind == CandidateSourceKind.HYBRID_SEARCH
        assert len(result.proposals) == 1
        assert result.proposals[0].memory_id == uid

    @pytest.mark.asyncio
    async def test_bm25_score_from_rank(self):
        """BM25 proposals use 1/rank as score."""
        uid1, uid2 = uuid4(), uuid4()
        reader = FakeESReader(
            bm25_results=[
                FakeSearchResult(memory_id=str(uid1), bm25_rank=1),
                FakeSearchResult(memory_id=str(uid2), bm25_rank=4),
            ],
        )
        source = ElasticsearchCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        assert result.proposals[0].score == pytest.approx(1.0 / 1)
        assert result.proposals[1].score == pytest.approx(1.0 / 4)

    @pytest.mark.asyncio
    async def test_bm25_empty_results(self):
        """BM25 returns nothing → empty proposals (not an error)."""
        reader = FakeESReader(bm25_results=[])
        source = ElasticsearchCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        assert result.proposals == []
        assert result.error is None


# ---------------------------------------------------------------------------
# Tests: Elasticsearch hybrid embedding path
# ---------------------------------------------------------------------------

class TestElasticsearchHybridPath:
    """When an embedding provider is supplied, hybrid search is used."""

    @pytest.mark.asyncio
    async def test_hybrid_called_with_embedding(self):
        """With embedding → hybrid search is used, not BM25."""
        uid = uuid4()
        reader = FakeESReader(
            hybrid_results=[
                FakeSearchResult(memory_id=str(uid), rrf_score=0.042),
            ],
        )
        embedder = FakeEmbeddingProvider(vector=[0.1, 0.2, 0.3])
        source = ElasticsearchCandidateSource(
            reader, embedding_provider=embedder,
        )

        await source.propose_candidates(_make_query("test hybrid"))

        assert len(reader.hybrid_calls) == 1
        assert len(reader.bm25_calls) == 0
        assert embedder.call_count == 1
        assert embedder.last_query == "test hybrid"
        call = reader.hybrid_calls[0]
        assert call["query_vector"] == [0.1, 0.2, 0.3]

    @pytest.mark.asyncio
    async def test_hybrid_score_from_rrf(self):
        """Hybrid proposals use rrf_score directly."""
        uid = uuid4()
        reader = FakeESReader(
            hybrid_results=[
                FakeSearchResult(memory_id=str(uid), rrf_score=0.0125),
            ],
        )
        embedder = FakeEmbeddingProvider()
        source = ElasticsearchCandidateSource(
            reader, embedding_provider=embedder,
        )
        result = await source.propose_candidates(_make_query())

        assert result.proposals[0].score == pytest.approx(0.0125)

    @pytest.mark.asyncio
    async def test_hybrid_null_rrf_score_defaults_to_zero(self):
        """Null rrf_score → score defaults to 0.0."""
        uid = uuid4()
        reader = FakeESReader(
            hybrid_results=[
                FakeSearchResult(memory_id=str(uid), rrf_score=None),
            ],
        )
        embedder = FakeEmbeddingProvider()
        source = ElasticsearchCandidateSource(
            reader, embedding_provider=embedder,
        )
        result = await source.propose_candidates(_make_query())

        assert result.proposals[0].score == 0.0


# ---------------------------------------------------------------------------
# Tests: Scope filter propagation
# ---------------------------------------------------------------------------

class TestScopeFilterPropagation:
    """Query dimensions propagate into ES SearchFilters."""

    @pytest.mark.asyncio
    async def test_project_id_filter_propagated(self):
        """project_id on query → SearchFilters.project_id."""
        project_id = uuid4()
        reader = FakeESReader()
        source = ElasticsearchCandidateSource(reader)
        await source.propose_candidates(
            _make_query(project_id=project_id),
        )

        filters = reader.bm25_calls[0]["filters"]
        assert filters.project_id == str(project_id)

    @pytest.mark.asyncio
    async def test_session_id_filter_propagated(self):
        """session_id on query → SearchFilters.session_id."""
        session_id = uuid4()
        reader = FakeESReader()
        source = ElasticsearchCandidateSource(reader)
        await source.propose_candidates(
            _make_query(session_id=session_id),
        )

        filters = reader.bm25_calls[0]["filters"]
        assert filters.session_id == str(session_id)

    @pytest.mark.asyncio
    async def test_all_dimensions_propagated(self):
        """All dimensions propagate simultaneously."""
        pid, sid, aid, kbid = uuid4(), uuid4(), uuid4(), uuid4()
        reader = FakeESReader()
        source = ElasticsearchCandidateSource(reader)
        await source.propose_candidates(
            _make_query(
                project_id=pid,
                session_id=sid,
                agent_id=aid,
                knowledge_base_id=kbid,
            ),
        )

        filters = reader.bm25_calls[0]["filters"]
        assert filters.project_id == str(pid)
        assert filters.session_id == str(sid)
        assert filters.agent_id == str(aid)
        assert filters.knowledge_base_id == str(kbid)

    @pytest.mark.asyncio
    async def test_no_dimensions_no_filters(self):
        """No dimensions → all filter fields are None (except status)."""
        reader = FakeESReader()
        source = ElasticsearchCandidateSource(reader)
        await source.propose_candidates(_make_query())

        filters = reader.bm25_calls[0]["filters"]
        assert filters.project_id is None
        assert filters.session_id is None
        assert filters.agent_id is None
        assert filters.knowledge_base_id is None
        assert filters.status == "active"


# ---------------------------------------------------------------------------
# Tests: Bounded ordering
# ---------------------------------------------------------------------------

class TestBoundedOrdering:
    """Results are bounded by query.max_items."""

    @pytest.mark.asyncio
    async def test_es_bounded_by_max_items(self):
        """max_items is passed as the ES search size."""
        reader = FakeESReader()
        source = ElasticsearchCandidateSource(reader)
        await source.propose_candidates(_make_query(max_items=5))

        assert reader.bm25_calls[0]["size"] == 5

    @pytest.mark.asyncio
    async def test_es_max_items_minimum_one(self):
        """max_items=0 is clamped to 1."""
        reader = FakeESReader()
        source = ElasticsearchCandidateSource(reader)
        await source.propose_candidates(_make_query(max_items=0))

        assert reader.bm25_calls[0]["size"] == 1


# ---------------------------------------------------------------------------
# Tests: UUID handling
# ---------------------------------------------------------------------------

class TestUUIDHandling:
    """Malformed UUIDs in projection data are silently dropped."""

    @pytest.mark.asyncio
    async def test_malformed_uuid_dropped(self):
        """Invalid memory_id string → proposal skipped."""
        good_id = uuid4()
        reader = FakeESReader(
            bm25_results=[
                FakeSearchResult(memory_id="not-a-uuid", bm25_rank=1),
                FakeSearchResult(memory_id=str(good_id), bm25_rank=2),
            ],
        )
        source = ElasticsearchCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        assert len(result.proposals) == 1
        assert result.proposals[0].memory_id == good_id

    @pytest.mark.asyncio
    async def test_empty_memory_id_dropped(self):
        """Empty memory_id → proposal skipped."""
        reader = FakeESReader(
            bm25_results=[
                FakeSearchResult(memory_id="", bm25_rank=1),
            ],
        )
        source = ElasticsearchCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        assert result.proposals == []

    @pytest.mark.asyncio
    async def test_redis_malformed_uuid_dropped(self):
        """Redis snapshot with bad id → proposal skipped."""
        good_id = uuid4()
        session = FakeHotSessionRead(
            items=[
                FakeHotItemRead(snapshot={"id": "garbage"}),
                FakeHotItemRead(snapshot={"id": str(good_id)}),
            ],
        )
        reader = FakeRedisReader(session_read=session)
        source = RedisHotStateCandidateSource(reader)
        query = _make_query(session_id=uuid4())
        result = await source.propose_candidates(query)

        assert len(result.proposals) == 1
        assert result.proposals[0].memory_id == good_id

    @pytest.mark.asyncio
    async def test_redis_missing_id_dropped(self):
        """Redis snapshot without id field → proposal skipped."""
        session = FakeHotSessionRead(
            items=[FakeHotItemRead(snapshot={"content": "no id here"})],
        )
        reader = FakeRedisReader(session_read=session)
        source = RedisHotStateCandidateSource(reader)
        result = await source.propose_candidates(
            _make_query(session_id=uuid4()),
        )

        assert result.proposals == []

    def test_safe_parse_uuid_valid(self):
        """Valid UUID string parses correctly."""
        uid = uuid4()
        assert _safe_parse_uuid(str(uid)) == uid

    def test_safe_parse_uuid_invalid(self):
        """Invalid strings return None."""
        assert _safe_parse_uuid("not-a-uuid") is None
        assert _safe_parse_uuid("") is None
        assert _safe_parse_uuid(None) is None


# ---------------------------------------------------------------------------
# Tests: Redis no-session behavior
# ---------------------------------------------------------------------------

class TestRedisNoSession:
    """Redis adapter returns empty when query has no session_id."""

    @pytest.mark.asyncio
    async def test_no_session_returns_empty(self):
        """session_id=None → empty proposals, no reader call."""
        reader = FakeRedisReader()
        source = RedisHotStateCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        assert result.proposals == []
        assert result.error is None
        assert len(reader.calls) == 0  # Reader not even called

    @pytest.mark.asyncio
    async def test_with_session_returns_items(self):
        """session_id set → reader called with that session."""
        session_id = uuid4()
        good_id = uuid4()
        session = FakeHotSessionRead(
            items=[FakeHotItemRead(snapshot={"id": str(good_id)})],
        )
        reader = FakeRedisReader(session_read=session)
        source = RedisHotStateCandidateSource(reader)
        result = await source.propose_candidates(
            _make_query(session_id=session_id),
        )

        assert len(result.proposals) == 1
        assert result.proposals[0].memory_id == good_id
        assert reader.calls[0]["session_id"] == session_id


# ---------------------------------------------------------------------------
# Tests: Redis bounded ordering
# ---------------------------------------------------------------------------

class TestRedisBoundedOrdering:
    """Redis results are bounded and in hot-state order."""

    @pytest.mark.asyncio
    async def test_bounded_by_max_items(self):
        """limit passed to reader is query.max_items."""
        session_id = uuid4()
        reader = FakeRedisReader()
        source = RedisHotStateCandidateSource(reader)
        await source.propose_candidates(
            _make_query(session_id=session_id, max_items=7),
        )

        assert reader.calls[0]["limit"] == 7

    @pytest.mark.asyncio
    async def test_score_decreases_with_position(self):
        """Hot-state order: first item has highest score."""
        session_id = uuid4()
        ids = [uuid4() for _ in range(3)]
        session = FakeHotSessionRead(
            items=[
                FakeHotItemRead(snapshot={"id": str(mid)})
                for mid in ids
            ],
        )
        reader = FakeRedisReader(session_read=session)
        source = RedisHotStateCandidateSource(reader)
        result = await source.propose_candidates(
            _make_query(session_id=session_id),
        )

        assert len(result.proposals) == 3
        # Scores: 1/(1+0)=1.0, 1/(1+1)=0.5, 1/(1+2)=0.333
        assert result.proposals[0].score == pytest.approx(1.0)
        assert result.proposals[1].score == pytest.approx(0.5)
        assert result.proposals[2].score == pytest.approx(1.0 / 3)
        # Order matches session order (hot-state desc).
        assert result.proposals[0].memory_id == ids[0]
        assert result.proposals[1].memory_id == ids[1]
        assert result.proposals[2].memory_id == ids[2]

    @pytest.mark.asyncio
    async def test_empty_session_returns_empty(self):
        """Session with no items → empty proposals."""
        session_id = uuid4()
        session = FakeHotSessionRead(items=[])
        reader = FakeRedisReader(session_read=session)
        source = RedisHotStateCandidateSource(reader)
        result = await source.propose_candidates(
            _make_query(session_id=session_id),
        )

        assert result.proposals == []
        assert result.error is None


# ---------------------------------------------------------------------------
# Tests: Constructor validation
# ---------------------------------------------------------------------------

class TestConstructorValidation:
    """Adapters reject invalid readers at construction time."""

    def test_es_rejects_non_protocol_reader(self):
        """Non-conforming reader raises TypeError."""
        with pytest.raises(TypeError):
            ElasticsearchCandidateSource("not a reader")

    def test_es_rejects_bad_embedding_provider(self):
        """Non-conforming embedding provider raises TypeError."""
        reader = FakeESReader()
        with pytest.raises(TypeError):
            ElasticsearchCandidateSource(
                reader, embedding_provider="not an embedder",
            )

    def test_redis_rejects_non_protocol_reader(self):
        """Non-conforming reader raises TypeError."""
        with pytest.raises(TypeError):
            RedisHotStateCandidateSource("not a reader")

    def test_es_kind_is_hybrid_search(self):
        """ES adapter always reports HYBRID_SEARCH kind."""
        reader = FakeESReader()
        source = ElasticsearchCandidateSource(reader)
        assert source.kind == CandidateSourceKind.HYBRID_SEARCH

    def test_redis_kind_is_hot_state(self):
        """Redis adapter always reports HOT_STATE kind."""
        reader = FakeRedisReader()
        source = RedisHotStateCandidateSource(reader)
        assert source.kind == CandidateSourceKind.HOT_STATE


# ---------------------------------------------------------------------------
# Tests: No authoritative content
# ---------------------------------------------------------------------------

class TestNoAuthoritativeContent:
    """Adapters propose IDs only — never return content fields."""

    @pytest.mark.asyncio
    async def test_es_proposals_have_no_content(self):
        """ES proposals contain only memory_id + score."""
        uid = uuid4()
        reader = FakeESReader(
            bm25_results=[
                FakeSearchResult(
                    memory_id=str(uid),
                    bm25_rank=1,
                    content="secret content that should NOT leak",
                ),
            ],
        )
        source = ElasticsearchCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        proposal = result.proposals[0]
        # CandidateProposal has only memory_id and score fields.
        assert not hasattr(proposal, "content")
        assert proposal.memory_id == uid
        assert isinstance(proposal.score, float)

    @pytest.mark.asyncio
    async def test_redis_proposals_have_no_content(self):
        """Redis proposals contain only memory_id + score."""
        uid = uuid4()
        session = FakeHotSessionRead(
            items=[FakeHotItemRead(snapshot={
                "id": str(uid),
                "content": "secret content",
            })],
        )
        reader = FakeRedisReader(session_read=session)
        source = RedisHotStateCandidateSource(reader)
        result = await source.propose_candidates(
            _make_query(session_id=uuid4()),
        )

        proposal = result.proposals[0]
        assert not hasattr(proposal, "content")
        assert proposal.memory_id == uid


# ---------------------------------------------------------------------------
# Fakes: Graph reader (P8-B)
# ---------------------------------------------------------------------------

@dataclass
class FakeGraphResult:
    """Mimics a graph reader result row for adapter tests."""
    memory_id: str
    score: float | None = None


class FakeGraphReader:
    """Fake graph reader that records calls and returns configured results."""

    def __init__(
        self,
        results: Sequence[FakeGraphResult] | None = None,
    ) -> None:
        self._results = list(results) if results else []
        self.calls: list[dict[str, Any]] = []

    async def search_candidates(
        self,
        *,
        query: str,
        max_items: int,
        project_id: UUID | None = None,
        session_id: UUID | None = None,
        agent_id: UUID | None = None,
        knowledge_base_id: UUID | None = None,
        status: str = "active",
    ) -> list[FakeGraphResult]:
        self.calls.append({
            "query": query,
            "max_items": max_items,
            "project_id": project_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "knowledge_base_id": knowledge_base_id,
            "status": status,
        })
        return list(self._results)


# ---------------------------------------------------------------------------
# Tests: Graph adapter — kind and constructor
# ---------------------------------------------------------------------------

class TestGraphAdapterKindAndConstruction:
    """Graph adapter reports GRAPH kind and validates its reader."""

    def test_kind_is_graph(self):
        """Graph adapter always reports GRAPH kind."""
        reader = FakeGraphReader()
        source = GraphCandidateSource(reader)
        assert source.kind == CandidateSourceKind.GRAPH

    def test_rejects_non_protocol_reader(self):
        """Non-conforming reader raises TypeError."""
        with pytest.raises(TypeError):
            GraphCandidateSource("not a reader")  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------------------
# Tests: Graph adapter — bounded and dimension-filtered
# ---------------------------------------------------------------------------

class TestGraphAdapterBoundedAndFiltered:
    """Graph adapter passes dimension filters and is bounded."""

    @pytest.mark.asyncio
    async def test_bounded_by_max_items(self):
        """max_items is forwarded to the graph reader."""
        reader = FakeGraphReader()
        source = GraphCandidateSource(reader)
        await source.propose_candidates(_make_query(max_items=5))

        assert reader.calls[0]["max_items"] == 5

    @pytest.mark.asyncio
    async def test_max_items_minimum_one(self):
        """max_items=0 is clamped to 1."""
        reader = FakeGraphReader()
        source = GraphCandidateSource(reader)
        await source.propose_candidates(_make_query(max_items=0))

        assert reader.calls[0]["max_items"] == 1

    @pytest.mark.asyncio
    async def test_query_text_propagated(self):
        """Query text is forwarded to the graph reader."""
        reader = FakeGraphReader()
        source = GraphCandidateSource(reader)
        await source.propose_candidates(_make_query("hello graph"))

        assert reader.calls[0]["query"] == "hello graph"

    @pytest.mark.asyncio
    async def test_all_dimensions_propagated(self):
        """All four data dimensions propagate to the reader."""
        pid, sid, aid, kbid = uuid4(), uuid4(), uuid4(), uuid4()
        reader = FakeGraphReader()
        source = GraphCandidateSource(reader)
        await source.propose_candidates(
            _make_query(
                project_id=pid,
                session_id=sid,
                agent_id=aid,
                knowledge_base_id=kbid,
            ),
        )

        call = reader.calls[0]
        assert call["project_id"] == pid
        assert call["session_id"] == sid
        assert call["agent_id"] == aid
        assert call["knowledge_base_id"] == kbid

    @pytest.mark.asyncio
    async def test_active_status_always_passed(self):
        """The adapter always passes status='active' to the reader."""
        reader = FakeGraphReader()
        source = GraphCandidateSource(reader)
        await source.propose_candidates(_make_query())

        assert reader.calls[0]["status"] == "active"

    @pytest.mark.asyncio
    async def test_no_dimensions_no_filters(self):
        """No dimensions → all filter fields are None."""
        reader = FakeGraphReader()
        source = GraphCandidateSource(reader)
        await source.propose_candidates(_make_query())

        call = reader.calls[0]
        assert call["project_id"] is None
        assert call["session_id"] is None
        assert call["agent_id"] is None
        assert call["knowledge_base_id"] is None


# ---------------------------------------------------------------------------
# Tests: Graph adapter — result mapping and score
# ---------------------------------------------------------------------------

class TestGraphAdapterResultMapping:
    """Graph results are mapped to proposals correctly."""

    @pytest.mark.asyncio
    async def test_valid_results_mapped(self):
        """Valid graph results → proposals with correct memory_id."""
        uid1, uid2 = uuid4(), uuid4()
        reader = FakeGraphReader(results=[
            FakeGraphResult(memory_id=str(uid1), score=0.9),
            FakeGraphResult(memory_id=str(uid2), score=0.5),
        ])
        source = GraphCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        assert result.kind == CandidateSourceKind.GRAPH
        assert len(result.proposals) == 2
        assert result.proposals[0].memory_id == uid1
        assert result.proposals[1].memory_id == uid2

    @pytest.mark.asyncio
    async def test_score_propagated(self):
        """Graph score is forwarded as the opaque proposal score."""
        uid = uuid4()
        reader = FakeGraphReader(results=[
            FakeGraphResult(memory_id=str(uid), score=0.42),
        ])
        source = GraphCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        assert result.proposals[0].score == pytest.approx(0.42)

    @pytest.mark.asyncio
    async def test_none_score_defaults_to_zero(self):
        """Missing score → defaults to 0.0."""
        uid = uuid4()
        reader = FakeGraphReader(results=[
            FakeGraphResult(memory_id=str(uid), score=None),
        ])
        source = GraphCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        assert result.proposals[0].score == 0.0

    @pytest.mark.asyncio
    async def test_empty_results(self):
        """Empty graph results → empty proposals (not an error)."""
        reader = FakeGraphReader(results=[])
        source = GraphCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        assert result.proposals == []
        assert result.error is None


# ---------------------------------------------------------------------------
# Tests: Graph adapter — malformed UUID handling
# ---------------------------------------------------------------------------

class TestGraphAdapterMalformedUUIDs:
    """Malformed graph IDs are silently dropped."""

    @pytest.mark.asyncio
    async def test_malformed_uuid_dropped(self):
        """Invalid memory_id string → proposal skipped."""
        good_id = uuid4()
        reader = FakeGraphReader(results=[
            FakeGraphResult(memory_id="not-a-uuid", score=0.9),
            FakeGraphResult(memory_id=str(good_id), score=0.5),
        ])
        source = GraphCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        assert len(result.proposals) == 1
        assert result.proposals[0].memory_id == good_id

    @pytest.mark.asyncio
    async def test_empty_memory_id_dropped(self):
        """Empty memory_id → proposal skipped."""
        reader = FakeGraphReader(results=[
            FakeGraphResult(memory_id="", score=0.9),
        ])
        source = GraphCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        assert result.proposals == []

    @pytest.mark.asyncio
    async def test_all_malformed_returns_empty(self):
        """All malformed IDs → empty proposals, no error."""
        reader = FakeGraphReader(results=[
            FakeGraphResult(memory_id="garbage-1"),
            FakeGraphResult(memory_id="garbage-2"),
        ])
        source = GraphCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        assert result.proposals == []
        assert result.error is None


# ---------------------------------------------------------------------------
# Tests: Graph adapter — no authoritative content
# ---------------------------------------------------------------------------

class TestGraphAdapterNoAuthoritativeContent:
    """Graph adapter proposes IDs only — never returns content fields."""

    @pytest.mark.asyncio
    async def test_proposals_have_no_content(self):
        """Graph proposals contain only memory_id + score."""
        uid = uuid4()
        reader = FakeGraphReader(results=[
            FakeGraphResult(memory_id=str(uid), score=0.8),
        ])
        source = GraphCandidateSource(reader)
        result = await source.propose_candidates(_make_query())

        proposal = result.proposals[0]
        assert not hasattr(proposal, "content")
        assert not hasattr(proposal, "evidence")
        assert proposal.memory_id == uid
        assert isinstance(proposal.score, float)
