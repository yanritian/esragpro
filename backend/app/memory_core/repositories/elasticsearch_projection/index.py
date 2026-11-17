"""Async Elasticsearch index operations (P1-06).

Preflight, ensure-index, upsert, get, delete, refresh, BM25 search,
kNN search, close. Uses the existing ``elasticsearch`` async client.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import (
    BadRequestError,
    ConnectionError as ESConnectionError,
    NotFoundError,
    TransportError,
)

from app.memory_core.repositories.elasticsearch_projection.mapping import (
    DEFAULT_DIMS,
    MAPPING_VERSION,
    assert_mapping_compatible,
    build_index_body,
    build_versioned_index_name,
)
from app.memory_core.repositories.elasticsearch_projection.models import (
    ElasticsearchSearchResult,
    PermanentElasticsearchPayloadError,
    SearchFilters,
    validate_embedding,
)
from app.memory_core.repositories.elasticsearch_projection.rrf import (
    DEFAULT_RRF_K,
    rrf_fuse,
)

logger = logging.getLogger(__name__)


class EmbeddingProvider(Protocol):
    """Narrow async embedding-provider protocol.

    Injected into the handler and reader. Implementations produce
    dense vectors for a given text.
    """

    async def embed(self, text: str) -> list[float]:
        """Return a dense vector for the given text."""
        ...


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hit_to_result(
    hit: dict[str, Any],
    *,
    index_name: str,
    bm25_rank: Optional[int] = None,
    vector_rank: Optional[int] = None,
    rrf_score: Optional[float] = None,
) -> ElasticsearchSearchResult:
    """Convert an ES hit dict to a typed search result."""
    src = hit.get("_source", {})
    return ElasticsearchSearchResult(
        memory_id=src.get("memory_id", ""),
        content=src.get("content", ""),
        status=src.get("status", ""),
        created_at=src.get("created_at"),
        updated_at=src.get("updated_at"),
        expires_at=src.get("expires_at"),
        project_id=src.get("project_id"),
        session_id=src.get("session_id"),
        agent_id=src.get("agent_id"),
        knowledge_base_id=src.get("knowledge_base_id"),
        metadata=src.get("metadata", {}),
        provenance=src.get("provenance"),
        evidence_refs=src.get("evidence_refs", []),
        bm25_rank=bm25_rank,
        vector_rank=vector_rank,
        rrf_score=rrf_score,
        es_index=index_name,
        es_id=hit.get("_id"),
    )


_IDEMPOTENCY_CONFLICT_MARKER = "idempotency_key conflict"


def _is_idempotency_conflict(exc: BadRequestError) -> bool:
    """Check whether a ``BadRequestError`` signals an idempotency-key conflict.

    Robust against elasticsearch-py versions where ``str(exc)`` may raise
    (e.g. when ``exc.meta`` is missing or malformed).  The structured
    ``exc.body`` is inspected first; only then do we fall back to
    stringifying the exception, guarded by a try/except.
    """
    marker = _IDEMPOTENCY_CONFLICT_MARKER
    body = getattr(exc, "body", None)
    if body is not None:
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, str) and marker in error:
                return True
            if isinstance(error, dict):
                for rc in error.get("root_cause", []) or []:
                    if marker in str(rc.get("reason", "")):
                        return True
                caused_by = error.get("caused_by") or {}
                if marker in str(caused_by.get("reason", "")):
                    return True
                if marker in str(error):
                    return True
        elif isinstance(body, str) and marker in body:
            return True
    try:
        return marker in str(exc)
    except Exception:
        return False


class ElasticsearchProjectionIndex:
    """Async ES index operations for memory projection (P1-06).

    Client ownership: by default the class does NOT own the client.
    Set ``owns_client=True`` if the caller transfers ownership.
    """

    def __init__(
        self,
        client: AsyncElasticsearch,
        *,
        index_name: str = "",
        dims: int = DEFAULT_DIMS,
        mapping_version: int = MAPPING_VERSION,
        owns_client: bool = False,
    ) -> None:
        if not isinstance(client, AsyncElasticsearch):
            raise TypeError("client must be an AsyncElasticsearch instance")
        if not isinstance(dims, int) or isinstance(dims, bool) or dims < 2:
            raise ValueError("dims must be a positive integer >= 2")
        self._client = client
        self._dims = dims
        self._mapping_version = mapping_version
        self._owns_client = owns_client
        if index_name:
            self._index_name = index_name
        else:
            self._index_name = build_versioned_index_name(
                version=mapping_version,
            )

    @property
    def index_name(self) -> str:
        return self._index_name

    @property
    def dims(self) -> int:
        return self._dims

    @property
    def owns_client(self) -> bool:
        return self._owns_client

    async def preflight_ping(self, *, timeout_s: float = 5.0) -> dict[str, Any]:
        """Ping ES and return version info. Raises on failure."""
        import asyncio

        info = await asyncio.wait_for(
            self._client.info(),
            timeout=timeout_s,
        )
        return dict(info.body)

    async def ensure_index(self) -> str:
        """Create the index if missing, or verify mapping compatibility.

        Returns the index name.
        Raises ValueError on mapping version mismatch.
        """
        exists = await self._client.indices.exists(index=self._index_name)
        if not exists:
            body = build_index_body(
                dims=self._dims,
                version=self._mapping_version,
            )
            await self._client.indices.create(
                index=self._index_name,
                body=body,
            )
            logger.info("created index %s", self._index_name)
        else:
            mapping_resp = await self._client.indices.get_mapping(
                index=self._index_name,
            )
            idx_data = mapping_resp.get(self._index_name, {})
            mappings = idx_data.get("mappings", {})
            meta = mappings.get("_meta")
            assert_mapping_compatible(
                actual_meta=meta,
                expected_version=self._mapping_version,
            )
        return self._index_name

    async def upsert(
        self,
        *,
        memory_id: str,
        content: str,
        embedding: list[float],
        status: str = "active",
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        expires_at: Optional[str] = None,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        knowledge_base_id: Optional[str] = None,
        event_type: str = "memory.remembered",
        schema_version: int = 1,
        idempotency_key: str = "",
        metadata: Optional[dict[str, Any]] = None,
        provenance: Optional[dict[str, Any]] = None,
        evidence_refs: Optional[list[dict[str, Any]]] = None,
    ) -> str:
        """Idempotent upsert of a memory projection document.

        The document ``_id`` is exactly the ``memory_id``.
        Returns the outcome: ``"created"`` or ``"updated"``.

        Idempotency-key conflict semantics: if a document already exists
        at ``memory_id`` with a DIFFERENT ``idempotency_key``, a
        ``PermanentElasticsearchPayloadError`` is raised. Same-key replay
        is accepted as an idempotent update.
        """
        vec = validate_embedding(embedding, expected_dims=self._dims)

        # Atomic idempotency-key conflict protection:
        #
        # Uses ``update(upsert=...)`` with an inline Painless script so that
        # the existence check and the write happen in a single ES operation.
        # This eliminates the check-then-index race that could allow two
        # concurrent writers with different idempotency keys to overwrite
        # each other.
        #
        # Semantics:
        # - Doc does NOT exist → upsert inserts the full document.
        # - Doc exists, same key → script updates all fields (idempotent).
        # - Doc exists, different non-empty key → script throws → we
        #   convert to PermanentElasticsearchPayloadError.
        # - Incoming key is empty → script always allows the update.
        script_body = (
            "if (params.idem_key != '' && "
            "ctx._source.idempotency_key != '' && "
            "ctx._source.idempotency_key != params.idem_key) {"
            "  throw new RuntimeException('idempotency_key conflict');"
            "}"
        )
        for field_name in (
            "content",
            "content_embedding",
            "status",
            "created_at",
            "updated_at",
            "expires_at",
            "project_id",
            "session_id",
            "agent_id",
            "knowledge_base_id",
            "event_type",
            "schema_version",
            "idempotency_key",
            "metadata",
            "provenance",
            "evidence_refs",
        ):
            script_body += f"ctx._source.{field_name} = params.{field_name};"

        doc: dict[str, Any] = {
            "content": content,
            "content_embedding": vec,
            "memory_id": memory_id,
            "status": status,
            "created_at": created_at or _now_utc_iso(),
            "updated_at": updated_at or _now_utc_iso(),
            "expires_at": expires_at,
            "project_id": project_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "knowledge_base_id": knowledge_base_id,
            "event_type": event_type,
            "schema_version": schema_version,
            "idempotency_key": idempotency_key,
            "metadata": metadata or {},
            "provenance": provenance,
            "evidence_refs": evidence_refs or [],
        }
        try:
            resp = await self._client.update(
                index=self._index_name,
                id=memory_id,
                body={
                    "script": {
                        "source": script_body,
                        "lang": "painless",
                        "params": {"idem_key": idempotency_key, **doc},
                    },
                    "upsert": doc,
                },
            )
        except BadRequestError as exc:
            if _is_idempotency_conflict(exc):
                raise PermanentElasticsearchPayloadError(
                    f"idempotency_key conflict for memory_id={memory_id}: "
                    f"incoming={idempotency_key!r}",
                ) from exc
            raise
        result = resp.get("result", "unknown")
        return result

    async def get(self, memory_id: str) -> Optional[dict[str, Any]]:
        """Get a document by memory_id. Returns None if not found."""
        try:
            resp = await self._client.get(
                index=self._index_name,
                id=memory_id,
            )
            return resp.get("_source")
        except NotFoundError:
            return None

    async def delete(self, memory_id: str) -> bool:
        """Delete a document. Returns True if deleted, False if not found."""
        try:
            await self._client.delete(
                index=self._index_name,
                id=memory_id,
            )
            return True
        except NotFoundError:
            return False

    async def refresh(self) -> None:
        """Refresh the index for immediate search visibility."""
        await self._client.indices.refresh(index=self._index_name)

    async def search_bm25(
        self,
        *,
        query: str,
        size: int = 20,
        filters: Optional[SearchFilters] = None,
    ) -> list[tuple[ElasticsearchSearchResult, int]]:
        """BM25 text search. Returns list of (result, rank) tuples."""
        if not query or not query.strip():
            raise ValueError("query must be non-empty")
        if size < 1:
            raise ValueError("size must be >= 1")

        es_filters = (filters or SearchFilters()).to_es_filter()
        bm25_query: dict[str, Any] = {"match": {"content": query}}
        if es_filters:
            bm25_query = {
                "bool": {
                    "must": [bm25_query],
                    "filter": es_filters,
                },
            }

        resp = await self._client.search(
            index=self._index_name,
            query=bm25_query,
            size=size,
        )
        hits = resp.get("hits", {}).get("hits", [])
        results: list[tuple[ElasticsearchSearchResult, int]] = []
        for rank, hit in enumerate(hits, 1):
            sr = _hit_to_result(
                hit,
                index_name=self._index_name,
                bm25_rank=rank,
            )
            results.append((sr, rank))
        return results

    async def search_knn(
        self,
        *,
        query_vector: list[float],
        size: int = 20,
        filters: Optional[SearchFilters] = None,
    ) -> list[tuple[ElasticsearchSearchResult, int]]:
        """kNN dense-vector search. Returns list of (result, rank)."""
        if size < 1:
            raise ValueError("size must be >= 1")
        vec = validate_embedding(query_vector, expected_dims=self._dims)

        es_filters = (filters or SearchFilters()).to_es_filter()
        knn_query: dict[str, Any] = {
            "field": "content_embedding",
            "query_vector": vec,
            "k": size,
            "num_candidates": max(size * 4, 50),
        }
        if es_filters:
            knn_query["filter"] = {"bool": {"filter": es_filters}}

        resp = await self._client.search(
            index=self._index_name,
            knn=knn_query,
            size=size,
        )
        hits = resp.get("hits", {}).get("hits", [])
        results: list[tuple[ElasticsearchSearchResult, int]] = []
        for rank, hit in enumerate(hits, 1):
            sr = _hit_to_result(
                hit,
                index_name=self._index_name,
                vector_rank=rank,
            )
            results.append((sr, rank))
        return results

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
        """Hybrid BM25 + kNN search with deterministic project-level RRF.

        Does NOT call the license-gated native ``rank.rrf`` API.
        """
        if size < 1:
            raise ValueError("size must be >= 1")
        if candidate_window < size:
            candidate_window = size

        bm25_results = await self.search_bm25(
            query=query,
            size=candidate_window,
            filters=filters,
        )
        knn_results = await self.search_knn(
            query_vector=query_vector,
            size=candidate_window,
            filters=filters,
        )

        bm25_hits = [{"memory_id": r.memory_id} for r, _ in bm25_results]
        vector_hits = [{"memory_id": r.memory_id} for r, _ in knn_results]

        fused = rrf_fuse(bm25_hits, vector_hits, rrf_k=rrf_k, top_k=size)

        bm25_map = {r.memory_id: r for r, _ in bm25_results}
        knn_map = {r.memory_id: r for r, _ in knn_results}

        final: list[ElasticsearchSearchResult] = []
        for fh in fused:
            base = bm25_map.get(fh.memory_id) or knn_map.get(fh.memory_id)
            if base is None:
                continue
            final.append(
                ElasticsearchSearchResult(
                    memory_id=fh.memory_id,
                    content=base.content,
                    status=base.status,
                    created_at=base.created_at,
                    updated_at=base.updated_at,
                    expires_at=base.expires_at,
                    project_id=base.project_id,
                    session_id=base.session_id,
                    agent_id=base.agent_id,
                    knowledge_base_id=base.knowledge_base_id,
                    metadata=base.metadata,
                    provenance=base.provenance,
                    evidence_refs=base.evidence_refs,
                    bm25_rank=fh.bm25_rank,
                    vector_rank=fh.vector_rank,
                    rrf_score=fh.rrf_score,
                    es_index=base.es_index,
                    es_id=base.es_id,
                )
            )
        return final

    async def close(self) -> None:
        """Close the owned client if ownership was transferred."""
        if self._owns_client:
            await self._client.close()


def is_retryable_es_error(exc: BaseException) -> bool:
    """Return True if the exception is a retryable ES infrastructure error."""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if isinstance(exc, (ESConnectionError, TransportError)):
        return True
    mod = type(exc).__module__ or ""
    name = type(exc).__name__
    if "elastic" in mod.lower() or "elastic" in name.lower():
        if any(kw in name.lower() for kw in ("connection", "timeout", "transport", "sniff")):
            return True
    return False


__all__ = [
    "ElasticsearchProjectionIndex",
    "EmbeddingProvider",
    "is_retryable_es_error",
]
