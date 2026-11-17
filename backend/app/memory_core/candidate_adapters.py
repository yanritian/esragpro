"""Concrete candidate-source adapters (P2, P8-B).

Adapts the Elasticsearch projection reader, Redis hot-state reader, and
Neo4j graph reader to the :class:`CandidateSource` protocol so the query
coordinator can use them as optional retrieval backends.

Design invariants
-----------------
1. Adapters propose **IDs only**. They never return authoritative
   content. The coordinator resolves IDs against PostgreSQL.
2. Each adapter depends on a *narrow reader protocol* — not the full
   reader class — so tests can supply fakes without standing up
   live ES/Redis/Neo4j.
3. Malformed projection data (e.g. unparseable UUIDs) is silently
   skipped. It must never become authoritative.
4. Results are bounded by ``query.max_items``.
5. No live service, permission/ACL, or authoritative-content path is
   introduced here.
6. The graph adapter (P8-B) injects a reader seam only — no Neo4j
   driver, schema, or runtime wiring is included in this slice.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol, Sequence, runtime_checkable
from uuid import UUID

from anyio.to_thread import run_sync as _run_sync_in_thread

from app.memory_core.candidate_source import (
    CandidateProposal,
    CandidateSourceKind,
    CandidateSourceResult,
)
from app.memory_core.repositories.elasticsearch_projection.models import SearchFilters
from app.memory_core.unpermissioned_contracts import UnpermissionedMemoryQuery

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Narrow reader protocols (for testability)
# ---------------------------------------------------------------------------

@runtime_checkable
class _EmbeddingProvider(Protocol):
    """Synchronous embedding provider (single-query path).

    The production ``EmbeddingService.embed_query`` satisfies this
    protocol. Tests can substitute a trivial fake.
    """

    def embed_query(self, query: str) -> list[float]:
        """Return the embedding vector for a single query string."""
        ...


@runtime_checkable
class _ElasticsearchReaderProtocol(Protocol):
    """Narrow read surface the ES adapter needs.

    ``ElasticsearchProjectionReader`` satisfies this protocol. Tests
    can supply a fake with the same method signatures.
    """

    async def search_bm25(
        self,
        *,
        query: str,
        size: int = 10,
        filters: Optional[SearchFilters] = None,
    ) -> Sequence[object]:
        ...

    async def search_hybrid(
        self,
        *,
        query: str,
        query_vector: list[float],
        size: int = 10,
        candidate_window: int = 20,
        rrf_k: int = 60,
        filters: Optional[SearchFilters] = None,
    ) -> Sequence[object]:
        ...


@runtime_checkable
class _RedisHotReaderProtocol(Protocol):
    """Narrow read surface the Redis adapter needs.

    ``RedisHotStateReader`` satisfies this protocol. Tests can supply
    a fake with the same method signatures.
    """

    async def list_session_items_typed(
        self,
        session_id: object,
        *,
        limit: int = 20,
    ) -> object:
        ...


@runtime_checkable
class _GraphReaderProtocol(Protocol):
    """Narrow read surface the graph adapter needs (P8-B).

    A production Neo4j reader may satisfy this protocol in a later
    phase. Tests supply a fake with the same method signatures.

    The method receives the four data-dimension filters plus an active
    status marker so the reader can scope its traversal without the
    adapter leaking any authoritative content or graph-side facts.

    Each returned object must expose a ``memory_id`` attribute (str or
    UUID) and may expose an optional ``score`` attribute (float).
    Malformed IDs are dropped by the adapter.
    """

    async def search_candidates(
        self,
        *,
        query: str,
        max_items: int,
        project_id: Optional[UUID] = None,
        session_id: Optional[UUID] = None,
        agent_id: Optional[UUID] = None,
        knowledge_base_id: Optional[UUID] = None,
        status: str = "active",
    ) -> Sequence[object]:
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_parse_uuid(value: object) -> Optional[UUID]:
    """Parse a UUID string, returning ``None`` on failure.

    Malformed IDs in projection data must NOT propagate — they are
    silently dropped so they cannot become authoritative.
    """
    if not isinstance(value, (str, UUID)):
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _build_es_filters(query: UnpermissionedMemoryQuery):
    """Build ``SearchFilters`` from query dimensions.

    Imported lazily to keep the module importable in test environments
    that don't have the full ES projection stack.
    """
    from app.memory_core.repositories.elasticsearch_projection.models import (
        SearchFilters,
    )

    return SearchFilters(
        project_id=str(query.project_id) if query.project_id else None,
        session_id=str(query.session_id) if query.session_id else None,
        agent_id=str(query.agent_id) if query.agent_id else None,
        knowledge_base_id=(
            str(query.knowledge_base_id) if query.knowledge_base_id else None
        ),
        status="active",
    )


def _score_from_bm25_rank(rank: Optional[int]) -> float:
    """Convert BM25 rank (1-based) to an opaque score (higher = better)."""
    if rank is None or rank <= 0:
        return 0.0
    return 1.0 / rank


def _score_from_rrf(rrf_score: Optional[float]) -> float:
    """Use the RRF fused score directly; fall back to 0.0."""
    if rrf_score is None:
        return 0.0
    return float(rrf_score)


# ---------------------------------------------------------------------------
# Elasticsearch candidate source
# ---------------------------------------------------------------------------

class ElasticsearchCandidateSource:
    """Candidate source backed by Elasticsearch projection (BM25 or hybrid).

    When an ``embedding_provider`` is supplied, the adapter uses hybrid
    search (BM25 + kNN with RRF fusion). Without it, it falls back to
    BM25-only search. Both paths return bounded candidate proposals.

    Parameters
    ----------
    reader:
        An object satisfying :class:`_ElasticsearchReaderProtocol`.
        The production ``ElasticsearchProjectionReader`` qualifies.
    embedding_provider:
        Optional object with a synchronous ``embed_query(str) -> list[float]``
        method. When provided, hybrid search is used. When ``None``,
        BM25-only search is used.
    """

    def __init__(
        self,
        reader: _ElasticsearchReaderProtocol,
        *,
        embedding_provider: Optional[_EmbeddingProvider] = None,
    ) -> None:
        if not isinstance(reader, _ElasticsearchReaderProtocol):
            raise TypeError(
                "reader must satisfy _ElasticsearchReaderProtocol",
            )
        if embedding_provider is not None and not isinstance(
            embedding_provider, _EmbeddingProvider,
        ):
            raise TypeError(
                "embedding_provider must satisfy _EmbeddingProvider",
            )
        self._reader = reader
        self._embedding_provider = embedding_provider

    @property
    def kind(self) -> CandidateSourceKind:
        return CandidateSourceKind.HYBRID_SEARCH

    async def propose_candidates(
        self,
        query: UnpermissionedMemoryQuery,
    ) -> CandidateSourceResult:
        """Run BM25 or hybrid search and map results to proposals.

        The adapter is bounded by ``query.max_items``. Malformed UUIDs
        in projection data are silently dropped.
        """
        max_items = max(1, query.max_items)
        filters = _build_es_filters(query)

        if self._embedding_provider is not None:
            proposals = await self._propose_hybrid(
                query, max_items, filters,
            )
        else:
            proposals = await self._propose_bm25(
                query, max_items, filters,
            )

        return CandidateSourceResult(
            kind=self.kind,
            proposals=proposals,
        )

    async def _propose_bm25(
        self,
        query: UnpermissionedMemoryQuery,
        max_items: int,
        filters,
    ) -> list[CandidateProposal]:
        """BM25-only fallback path."""
        results = await self._reader.search_bm25(
            query=query.query,
            size=max_items,
            filters=filters,
        )
        return self._map_results_bm25(results)

    async def _propose_hybrid(
        self,
        query: UnpermissionedMemoryQuery,
        max_items: int,
        filters,
    ) -> list[CandidateProposal]:
        """Hybrid (BM25 + kNN) path with RRF fusion."""
        provider = self._embedding_provider
        assert provider is not None  # guarded by caller
        query_vector = await _run_sync_in_thread(
            provider.embed_query,
            query.query,
        )
        results = await self._reader.search_hybrid(
            query=query.query,
            query_vector=query_vector,
            size=max_items,
            filters=filters,
        )
        return self._map_results_hybrid(results)

    @staticmethod
    def _map_results_bm25(results: Sequence[object]) -> list[CandidateProposal]:
        """Map BM25 search results to candidate proposals."""
        proposals: list[CandidateProposal] = []
        for r in results:
            mid = _safe_parse_uuid(getattr(r, "memory_id", ""))
            if mid is None:
                logger.debug(
                    "ES candidate source: skipping malformed memory_id",
                )
                continue
            score = _score_from_bm25_rank(getattr(r, "bm25_rank", None))
            proposals.append(CandidateProposal(memory_id=mid, score=score))
        return proposals

    @staticmethod
    def _map_results_hybrid(results: Sequence[object]) -> list[CandidateProposal]:
        """Map hybrid search results to candidate proposals."""
        proposals: list[CandidateProposal] = []
        for r in results:
            mid = _safe_parse_uuid(getattr(r, "memory_id", ""))
            if mid is None:
                logger.debug(
                    "ES candidate source: skipping malformed memory_id",
                )
                continue
            score = _score_from_rrf(getattr(r, "rrf_score", None))
            proposals.append(CandidateProposal(memory_id=mid, score=score))
        return proposals


# ---------------------------------------------------------------------------
# Redis hot-state candidate source
# ---------------------------------------------------------------------------

class RedisHotStateCandidateSource:
    """Candidate source backed by Redis hot-state session cache.

    Returns bounded session candidates in hot-state order (most-recent
    first, per the ZSET score). When the query has no ``session_id``,
    the adapter returns an empty result — it cannot propose candidates
    without a session scope.

    Parameters
    ----------
    reader:
        An object satisfying :class:`_RedisHotReaderProtocol`.
        The production ``RedisHotStateReader`` qualifies.
    """

    def __init__(
        self,
        reader: _RedisHotReaderProtocol,
    ) -> None:
        if not isinstance(reader, _RedisHotReaderProtocol):
            raise TypeError(
                "reader must satisfy _RedisHotReaderProtocol",
            )
        self._reader = reader

    @property
    def kind(self) -> CandidateSourceKind:
        return CandidateSourceKind.HOT_STATE

    async def propose_candidates(
        self,
        query: UnpermissionedMemoryQuery,
    ) -> CandidateSourceResult:
        """Return session candidates in hot-state order.

        - No ``session_id`` → empty result (not an error).
        - Malformed UUIDs in snapshot data → silently dropped.
        - Bounded by ``query.max_items``.
        """
        if query.session_id is None:
            return CandidateSourceResult(
                kind=self.kind,
                proposals=[],
            )

        max_items = max(1, query.max_items)
        session = await self._reader.list_session_items_typed(
            query.session_id,
            limit=max_items,
        )

        return CandidateSourceResult(
            kind=self.kind,
            proposals=self._map_session_items(session),
        )

    @staticmethod
    def _map_session_items(session: object) -> list[CandidateProposal]:
        """Map hot-state session items to candidate proposals.

        Items arrive in hot-state order (most-recent first from ZSET
        desc). Score decreases with position: ``1 / (1 + pos)``.
        """
        proposals: list[CandidateProposal] = []
        items = getattr(session, "items", [])
        for pos, item in enumerate(items):
            snapshot = getattr(item, "snapshot", {})
            raw_id = snapshot.get("id", "")
            mid = _safe_parse_uuid(raw_id)
            if mid is None:
                logger.debug(
                    "Redis candidate source: skipping malformed memory_id",
                )
                continue
            score = 1.0 / (1 + pos)
            proposals.append(CandidateProposal(memory_id=mid, score=score))
        return proposals


# ---------------------------------------------------------------------------
# Graph candidate source (P8-B)
# ---------------------------------------------------------------------------

class GraphCandidateSource:
    """Candidate source backed by a graph reader (P8-B seam).

    This adapter injects a narrow reader protocol so a future production
    Neo4j implementation can plug in without changing the coordinator.
    The current slice does **not** include a Neo4j driver, schema,
    migration, index, or runtime wiring — those belong to a later
    projection/runtime phase.

    The adapter proposes memory IDs only. It never returns authoritative
    content or graph-side facts. The coordinator resolves all proposed
    IDs against PostgreSQL, applies evidence/validity/status filters,
    and enforces scope.

    Parameters
    ----------
    reader:
        An object satisfying :class:`_GraphReaderProtocol`.
    """

    def __init__(
        self,
        reader: _GraphReaderProtocol,
    ) -> None:
        if not isinstance(reader, _GraphReaderProtocol):
            raise TypeError(
                "reader must satisfy _GraphReaderProtocol",
            )
        self._reader = reader

    @property
    def kind(self) -> CandidateSourceKind:
        return CandidateSourceKind.GRAPH

    async def propose_candidates(
        self,
        query: UnpermissionedMemoryQuery,
    ) -> CandidateSourceResult:
        """Search the graph for candidate memory IDs.

        - Bounded by ``query.max_items``.
        - Passes the four data-dimension filters plus active status to
          the reader so the graph traversal can be scoped.
        - Malformed UUIDs in reader results → silently dropped.
        """
        max_items = max(1, query.max_items)

        results = await self._reader.search_candidates(
            query=query.query,
            max_items=max_items,
            project_id=query.project_id,
            session_id=query.session_id,
            agent_id=query.agent_id,
            knowledge_base_id=query.knowledge_base_id,
            status="active",
        )

        return CandidateSourceResult(
            kind=self.kind,
            proposals=self._map_results(results),
        )

    @staticmethod
    def _map_results(results: Sequence[object]) -> list[CandidateProposal]:
        """Map graph search results to candidate proposals.

        Each result must expose a ``memory_id`` attribute. An optional
        ``score`` attribute is used as the opaque proposal score;
        absent/None scores default to 0.0.
        """
        proposals: list[CandidateProposal] = []
        for r in results:
            mid = _safe_parse_uuid(getattr(r, "memory_id", ""))
            if mid is None:
                logger.debug(
                    "Graph candidate source: skipping malformed memory_id",
                )
                continue
            raw_score = getattr(r, "score", None)
            score = float(raw_score) if raw_score is not None else 0.0
            proposals.append(CandidateProposal(memory_id=mid, score=score))
        return proposals


__all__ = [
    "ElasticsearchCandidateSource",
    "GraphCandidateSource",
    "RedisHotStateCandidateSource",
]
