"""Runtime composition seam for optional P2 candidate sources.

Creates the ``AsyncElasticsearch`` projection reader and
``redis.asyncio.Redis`` hot-state reader, wraps them with the P2
candidate adapters, and owns their async close lifecycle.

The composition is opt-in via
``settings.MEMORY_CANDIDATE_SOURCES_ENABLED``.  When disabled, no
clients are created and the PostgreSQL-only path remains the sole
retrieval backend.

Design invariants
-----------------
1. Reuses the existing versioned ES projection index naming
   (:func:`build_versioned_index_name`) and the configured
   ``EmbeddingService`` — no second projection implementation.
2. On any construction failure the seam returns ``([], [])`` so the
   caller can fall back to PostgreSQL-only without breaking.
3. Close callables are idempotent-safe: each may be awaited at most
   once; calling close on an already-closed client is a no-op at the
   driver level.
"""

from __future__ import annotations

import inspect
import logging
from typing import (
    Awaitable,
    Callable,
    Protocol,
    Sequence,
    Tuple,
    Union,
    cast,
    runtime_checkable,
)

from app.memory_core.candidate_source import CandidateSource

logger = logging.getLogger(__name__)

CloseCallable = Callable[[], Union[None, Awaitable[None]]]
CompositionResult = Tuple[Sequence[CandidateSource], Sequence[CloseCallable]]
OptionalFactory = Callable[
    [],
    Union[CompositionResult, Awaitable[CompositionResult]],
]


@runtime_checkable
class SettingsLike(Protocol):
    """Typed boundary for the settings attributes consumed by this seam.

    Only the attributes actually read by :func:`_build_sources` are
    declared.  The real ``app.core.config.settings`` object satisfies
    this protocol structurally — no inheritance required.
    """

    ES_HOST: str
    ES_USER: str | None
    ES_PASSWORD: str | None
    REDIS_URL: str
    EMBEDDING_DIMENSION: int


#: Exception types that may be raised during optional-source composition.
#:
#: Covers:
#: - Import failures (``ImportError``) from missing optional dependencies.
#: - Configuration / constructor errors (``TypeError``, ``ValueError``)
#:   from invalid client constructor arguments.
#: - OS-level resource errors (``OSError``) from network unreachability.
#: - Connection/timeout errors (``ConnectionError``, ``TimeoutError``)
#:   from unreachable services or connection-pool exhaustion.
#:
#: Programming errors (``AttributeError``, ``NotImplementedError``,
#: ``AssertionError``) deliberately do **not** appear here — they
#: propagate to surface bugs rather than silently degrading to
#: PostgreSQL-only.
CONSTRUCTION_ERRORS: tuple[type[Exception], ...] = (
    ImportError,
    TypeError,
    ValueError,
    OSError,
    ConnectionError,
    TimeoutError,
)


async def _invoke_closer(closer: CloseCallable) -> None:
    """Invoke a closer that may be sync or async.

    Swallows *all* exceptions — the close path must be defensive so
    that one failing closer does not prevent remaining closers from
    running.  This is distinct from the construction path, where only
    :data:`CONSTRUCTION_ERRORS` are caught.
    """
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.warning(
            "candidate_sources_runtime: failed to close client",
            exc_info=True,
        )


async def close_optional_sources(
    closers: Sequence[CloseCallable],
) -> None:
    """Close all owned client connections.

    Idempotent: safe to call multiple times.  Each closer is invoked
    exactly once per call; errors in one closer do not prevent the
    remaining closers from running.
    """
    for closer in closers:
        await _invoke_closer(closer)


async def _invoke_optional_factory(
    factory: OptionalFactory,
) -> CompositionResult:
    """Invoke an optional-source factory that may be sync or async.

    The factory may return ``(sources, closers)`` directly or as an
    awaitable.  The result is normalised to lists.
    """
    result = factory()
    if inspect.isawaitable(result):
        resolved: CompositionResult = await cast(
            Awaitable[CompositionResult], result
        )
    else:
        resolved = cast(CompositionResult, result)
    sources, closers = resolved
    return list(sources), list(closers)


async def compose_optional_sources() -> CompositionResult:
    """Create ES and Redis candidate sources with their close lifecycle.

    Returns ``(sources, closers)``.  On any failure during construction,
    partially-created clients are cleaned up and ``([], [])`` is
    returned so the caller can fall back to PostgreSQL-only.

    Only :data:`CONSTRUCTION_ERRORS` are caught — programming errors
    (``AttributeError``, ``NotImplementedError``, ``AssertionError``)
    propagate to surface bugs.

    The embedding service (``app.services.embedding.embedding_service``)
    is imported lazily — it pulls in heavy dependencies (torch,
    sentence-transformers) that should not be loaded when the feature
    is disabled.
    """
    from app.core.config import settings

    try:
        return await _build_sources(settings)
    except CONSTRUCTION_ERRORS:
        logger.warning(
            "candidate_sources_runtime: composition failed, "
            "falling back to PostgreSQL-only",
            exc_info=True,
        )
        # Partial cleanup is handled inside _build_sources; any
        # fully-created client has already been closed before the
        # exception propagated.
        return [], []


async def _build_sources(settings: SettingsLike) -> CompositionResult:
    """Construct ES and Redis clients, readers, and adapters.

    Async so that partially-created clients can be closed on failure.
    If the ES client is created but the Redis client fails to construct,
    the ES client is closed immediately to prevent a resource leak, then
    the exception is re-raised for the caller to handle.

    Network I/O does not happen here — client construction is deferred
    to first use by both drivers.
    """
    # ---- Elasticsearch source ----
    from elasticsearch import AsyncElasticsearch

    from app.memory_core.candidate_adapters import (
        ElasticsearchCandidateSource,
    )
    from app.memory_core.repositories.elasticsearch_projection.index import (
        ElasticsearchProjectionIndex,
    )
    from app.memory_core.repositories.elasticsearch_projection.mapping import (
        DEFAULT_INDEX_NAME,
        MAPPING_VERSION,
        build_versioned_index_name,
    )
    from app.memory_core.repositories.elasticsearch_projection.reader import (
        ElasticsearchProjectionReader,
    )
    from app.services.embedding import embedding_service

    es_auth = (
        (settings.ES_USER, settings.ES_PASSWORD)
        if settings.ES_USER and settings.ES_PASSWORD
        else None
    )
    es_client = AsyncElasticsearch(
        settings.ES_HOST,
        basic_auth=es_auth,
    )

    try:
        # ---- Redis source ----
        from redis.asyncio import Redis

        from app.memory_core.candidate_adapters import (
            RedisHotStateCandidateSource,
        )
        from app.memory_core.repositories.redis_hot.reader import (
            RedisHotStateReader,
        )

        redis_client = Redis.from_url(
            settings.REDIS_URL,
            decode_responses=False,
        )
    except CONSTRUCTION_ERRORS:
        # Redis construction failed — close the partially-created ES
        # client before propagating so it does not leak.
        await _invoke_closer(es_client.close)
        raise

    index_name = build_versioned_index_name(
        base_name=DEFAULT_INDEX_NAME,
        version=MAPPING_VERSION,
    )
    es_index = ElasticsearchProjectionIndex(
        es_client,
        index_name=index_name,
        dims=settings.EMBEDDING_DIMENSION,
    )
    es_reader = ElasticsearchProjectionReader(es_index)
    es_source = ElasticsearchCandidateSource(
        es_reader,
        embedding_provider=embedding_service,
    )

    redis_reader = RedisHotStateReader(redis_client)
    redis_source = RedisHotStateCandidateSource(redis_reader)

    sources: list[CandidateSource] = [es_source, redis_source]
    closers: list[CloseCallable] = [es_client.close, redis_client.aclose]

    return sources, closers


__all__ = [
    "compose_optional_sources",
    "close_optional_sources",
    "CONSTRUCTION_ERRORS",
]
