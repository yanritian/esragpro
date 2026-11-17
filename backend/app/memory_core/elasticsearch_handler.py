"""Projection handler for ``target='elasticsearch'`` (P1-06).

Implements ``ProjectionHandler`` protocol. Delegates the actual
Elasticsearch work to ``ElasticsearchProjectionIndex``.

Never mutates PostgreSQL/outbox state. Never logs credentials
or raw payloads.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from elasticsearch import AsyncElasticsearch

from app.memory_core.projection_worker.contracts import (
    PermanentProjectionError,
    ProjectionEvent,
)
from app.memory_core.repositories.elasticsearch_projection.index import (
    ElasticsearchProjectionIndex,
    EmbeddingProvider,
    is_retryable_es_error,
)
from app.memory_core.repositories.elasticsearch_projection.mapping import (
    DEFAULT_DIMS,
    DEFAULT_INDEX_NAME,
    MAPPING_VERSION,
    build_versioned_index_name,
)
from app.memory_core.repositories.elasticsearch_projection.models import (
    PermanentElasticsearchPayloadError,
    validate_payload_cross_check,
)

logger = logging.getLogger(__name__)


class ElasticsearchProjectionHandler:
    """ProjectionHandler for target 'elasticsearch'.

    Client ownership:
    - ``create()`` builds an index that owns the provided ES client
      (``close()`` will close it).
    - Direct construction does NOT close the client unless the index
      was built with ``owns_client=True``.
    """

    def __init__(
        self,
        index: ElasticsearchProjectionIndex,
        *,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        if not isinstance(index, ElasticsearchProjectionIndex):
            raise TypeError("index must be an ElasticsearchProjectionIndex")
        if embedding_provider is None:
            raise TypeError("embedding_provider must not be None")
        self._index = index
        self._embedding_provider = embedding_provider

    @classmethod
    async def create(
        cls,
        es_client: AsyncElasticsearch,
        *,
        embedding_provider: EmbeddingProvider,
        index_name: str = "",
        dims: int = DEFAULT_DIMS,
        mapping_version: int = MAPPING_VERSION,
        preflight_timeout_s: float = 5.0,
    ) -> "ElasticsearchProjectionHandler":
        """Build a handler with an owned ES client.

        Performs preflight ping and ensures the index exists.
        """
        idx = ElasticsearchProjectionIndex(
            es_client,
            index_name=index_name
            or build_versioned_index_name(
                base_name=DEFAULT_INDEX_NAME,
                version=mapping_version,
            ),
            dims=dims,
            mapping_version=mapping_version,
            owns_client=True,
        )
        await idx.preflight_ping(timeout_s=preflight_timeout_s)
        await idx.ensure_index()
        return cls(idx, embedding_provider=embedding_provider)

    @property
    def index(self) -> ElasticsearchProjectionIndex:
        return self._index

    async def preflight_ping(self, *, timeout_s: float = 5.0) -> dict[str, Any]:
        return await self._index.preflight_ping(timeout_s=timeout_s)

    async def ensure_index(self) -> str:
        return await self._index.ensure_index()

    async def close(self) -> None:
        await self._index.close()

    async def __call__(self, event: ProjectionEvent) -> None:
        payload = event.get_payload_copy()
        parsed = validate_payload_cross_check(
            payload,
            envelope_idempotency_key=event.idempotency_key,
            envelope_aggregate_id=str(event.aggregate_id),
            envelope_event_type=event.event_type,
            envelope_schema_version=event.schema_version,
            envelope_target=event.projection_target,
        )

        snap = parsed.memory_snapshot
        embedding: Optional[list[float]] = None
        try:
            embedding = await self._embedding_provider.embed(snap.content)
        except PermanentProjectionError:
            # Already a permanent error — propagate as-is.
            raise
        except PermanentElasticsearchPayloadError as exc:
            # Data/vector-shape errors are permanent — no retry can fix them.
            raise PermanentProjectionError(
                f"elasticsearch handler: embedding data error: {type(exc).__name__}: {exc}",
            ) from exc
        except (ValueError, TypeError) as exc:
            # Content/validation failures from the embedding provider are
            # permanent — retrying the same bad input cannot succeed.
            raise PermanentProjectionError(
                f"elasticsearch handler: embedding content error: {type(exc).__name__}: {exc}",
            ) from exc
        except Exception as exc:
            # Unknown provider/runtime errors remain retryable by the worker.
            # Do NOT silently swallow — the event must be retried or
            # dead-lettered after exhaustion.
            logger.warning(
                "elasticsearch handler: embedding failed (retryable): %s: %s",
                type(exc).__name__,
                exc,
            )
            raise

        try:
            await self._index.upsert(
                memory_id=snap.id,
                content=snap.content,
                embedding=embedding,
                status=snap.status,
                created_at=snap.created_at,
                updated_at=snap.updated_at,
                expires_at=snap.expires_at,
                project_id=snap.project_id,
                session_id=snap.session_id,
                agent_id=snap.agent_id,
                knowledge_base_id=snap.knowledge_base_id,
                event_type=parsed.event_type,
                schema_version=parsed.schema_version,
                idempotency_key=event.idempotency_key,
                metadata=snap.metadata,
                provenance=snap.provenance,
                evidence_refs=snap.evidence_refs,
            )
        except Exception as exc:
            if is_retryable_es_error(exc):
                raise
            raise PermanentProjectionError(
                f"elasticsearch projection: upsert failed: {type(exc).__name__}",
            ) from exc

        logger.debug(
            "elasticsearch handler: event=%s memory_id=%s",
            event.outbox_id,
            snap.id,
        )


__all__ = ["ElasticsearchProjectionHandler"]
