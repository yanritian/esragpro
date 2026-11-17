"""Typed BM25/vector/hybrid query surface (P1-06).

Wraps ElasticsearchProjectionIndex with a reader-friendly interface.
The reader does not perform writes; it delegates search to the index.
"""

from __future__ import annotations

from typing import Optional

from app.memory_core.repositories.elasticsearch_projection.index import (
    ElasticsearchProjectionIndex,
)
from app.memory_core.repositories.elasticsearch_projection.models import (
    ElasticsearchSearchResult,
    SearchFilters,
)
from app.memory_core.repositories.elasticsearch_projection.rrf import (
    DEFAULT_RRF_K,
)


class ElasticsearchProjectionReader:
    """Typed read-only query surface for memory projection (P1-06).

    The reader wraps an ``ElasticsearchProjectionIndex`` and exposes
    clean search methods that return typed results.
    """

    def __init__(self, index: ElasticsearchProjectionIndex) -> None:
        if not isinstance(index, ElasticsearchProjectionIndex):
            raise TypeError("index must be an ElasticsearchProjectionIndex")
        self._index = index

    @property
    def index_name(self) -> str:
        return self._index.index_name

    async def search_bm25(
        self,
        *,
        query: str,
        size: int = 10,
        filters: Optional[SearchFilters] = None,
    ) -> list[ElasticsearchSearchResult]:
        """BM25 text search. Returns typed results with bm25_rank."""
        results = await self._index.search_bm25(
            query=query,
            size=size,
            filters=filters,
        )
        return [r for r, _rank in results]

    async def search_vector(
        self,
        *,
        query_vector: list[float],
        size: int = 10,
        filters: Optional[SearchFilters] = None,
    ) -> list[ElasticsearchSearchResult]:
        """kNN vector search. Returns typed results with vector_rank."""
        results = await self._index.search_knn(
            query_vector=query_vector,
            size=size,
            filters=filters,
        )
        return [r for r, _rank in results]

    async def search_hybrid(
        self,
        *,
        query: str,
        query_vector: list[float],
        size: int = 10,
        candidate_window: int = 20,
        rrf_k: int = DEFAULT_RRF_K,
        filters: Optional[SearchFilters] = None,
    ) -> list[ElasticsearchSearchResult]:
        """Hybrid search with project-level deterministic RRF.

        Exposes both source ranks (bm25_rank, vector_rank) and the
        fused rrf_score for auditability.
        """
        return await self._index.search_hybrid(
            query=query,
            query_vector=query_vector,
            size=size,
            candidate_window=candidate_window,
            rrf_k=rrf_k,
            filters=filters,
        )

    async def get_by_id(
        self,
        memory_id: str,
    ) -> Optional[ElasticsearchSearchResult]:
        """Get a single document by memory_id. Returns None if not found."""
        doc = await self._index.get(memory_id)
        if doc is None:
            return None
        return ElasticsearchSearchResult(
            memory_id=doc.get("memory_id", memory_id),
            content=doc.get("content", ""),
            status=doc.get("status", ""),
            created_at=doc.get("created_at"),
            updated_at=doc.get("updated_at"),
            expires_at=doc.get("expires_at"),
            project_id=doc.get("project_id"),
            session_id=doc.get("session_id"),
            agent_id=doc.get("agent_id"),
            knowledge_base_id=doc.get("knowledge_base_id"),
            metadata=doc.get("metadata", {}),
            provenance=doc.get("provenance"),
            evidence_refs=doc.get("evidence_refs", []),
            es_index=self._index.index_name,
            es_id=memory_id,
        )


__all__ = ["ElasticsearchProjectionReader"]
