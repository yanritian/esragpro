"""Production composition root for P1-06 Elasticsearch projection worker.

Creates the async ES client and injected embedding provider, performs
ES preflight BEFORE any outbox claim, constructs exactly one ES handler,
registers it in the ProjectionHandlerRegistry, constructs the PostgreSQL
outbox store and ProjectionWorker, and runs the polling lifecycle.

Ownership:
- The composition root creates and owns the ES client, handler,
  registry, and store/worker it builds.
- On normal exit, cancellation, or startup failure, the root closes
  ONLY the resources it created.
- A caller-supplied ``async_sessionmaker`` is NOT owned; the root
  never disposes it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Optional

from elasticsearch import AsyncElasticsearch
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.memory_core.elasticsearch_handler import (
    ElasticsearchProjectionHandler,
)
from app.memory_core.projection_worker.contracts import (
    ProjectionHandlerRegistry,
)
from app.memory_core.projection_worker.polling import run_polling_loop
from app.memory_core.projection_worker.postgres_store import (
    ProjectionOutboxStore,
)
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.projection_worker.worker import ProjectionWorker
from app.memory_core.repositories.elasticsearch_projection.index import (
    EmbeddingProvider,
)
from app.memory_core.repositories.elasticsearch_projection.mapping import (
    DEFAULT_DIMS,
    DEFAULT_INDEX_NAME,
    MAPPING_VERSION,
    build_versioned_index_name,
)

logger = logging.getLogger(__name__)


async def _ping_with_timeout(
    client: AsyncElasticsearch,
    timeout_s: float,
) -> dict[str, Any]:
    """Ping ES with a bounded timeout; raises on failure."""
    info = await asyncio.wait_for(client.info(), timeout=timeout_s)
    return dict(info.body)


async def compose_and_run_elasticsearch_worker_from_settings(
    *,
    database_url: str,
    elasticsearch_url: str,
    embedding_provider: EmbeddingProvider,
    worker_identity: str,
    dims: int = DEFAULT_DIMS,
    index_name: str = "",
    batch_size: int = 10,
    max_concurrency: int = 10,
    lease_duration_s: float = 30.0,
    delivery_timeout_s: float = 10.0,
    finalization_margin_s: float = 5.0,
    idle_wait_s: float = 1.0,
    preflight_timeout_s: float = 5.0,
    retry_base_delay_s: float = 1.0,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Fully self-contained entrypoint from raw URLs.

    Creates the SQLAlchemy engine and ``async_sessionmaker`` from
    ``database_url``, then delegates to
    :func:`compose_and_run_elasticsearch_worker`.
    """
    if not database_url or not database_url.strip():
        raise ValueError("database_url must be non-empty")

    engine = create_async_engine(database_url)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    try:
        await compose_and_run_elasticsearch_worker(
            session_factory=factory,
            elasticsearch_url=elasticsearch_url,
            embedding_provider=embedding_provider,
            worker_identity=worker_identity,
            dims=dims,
            index_name=index_name,
            batch_size=batch_size,
            max_concurrency=max_concurrency,
            lease_duration_s=lease_duration_s,
            delivery_timeout_s=delivery_timeout_s,
            finalization_margin_s=finalization_margin_s,
            idle_wait_s=idle_wait_s,
            preflight_timeout_s=preflight_timeout_s,
            retry_base_delay_s=retry_base_delay_s,
            stop_event=stop_event,
        )
    finally:
        await engine.dispose()


async def compose_and_run_elasticsearch_worker(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    elasticsearch_url: str,
    embedding_provider: EmbeddingProvider,
    worker_identity: str,
    dims: int = DEFAULT_DIMS,
    index_name: str = "",
    batch_size: int = 10,
    max_concurrency: int = 10,
    lease_duration_s: float = 30.0,
    delivery_timeout_s: float = 10.0,
    finalization_margin_s: float = 5.0,
    idle_wait_s: float = 1.0,
    preflight_timeout_s: float = 5.0,
    retry_base_delay_s: float = 1.0,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Compose and run a production Elasticsearch projection worker.

    Steps:
    1. Create an AsyncElasticsearch client (owned by this function).
    2. ES preflight ping BEFORE any outbox claim.
    3. Ensure the compatible index exists.
    4. Construct exactly one ``ElasticsearchProjectionHandler``.
    5. Register it for target ``"elasticsearch"``.
    6. Construct ``ProjectionOutboxStore`` + ``ProjectionWorker``.
    7. Run the polling loop until ``stop_event`` or cancelled.
    8. On exit, close the ES client.
    """
    if not elasticsearch_url or not elasticsearch_url.strip():
        raise ValueError("elasticsearch_url must be non-empty")
    if not worker_identity or not worker_identity.strip():
        raise ValueError("worker_identity must be non-empty")
    if embedding_provider is None:
        raise ValueError("embedding_provider must not be None")

    resolved_index = index_name or build_versioned_index_name(
        base_name=DEFAULT_INDEX_NAME,
        version=MAPPING_VERSION,
    )

    es_client = AsyncElasticsearch(elasticsearch_url)
    handler: Optional[ElasticsearchProjectionHandler] = None

    try:
        # Step 2: ES preflight BEFORE any outbox claim.
        await _ping_with_timeout(es_client, preflight_timeout_s)

        # Step 3-4: construct handler with owned client.
        handler = await ElasticsearchProjectionHandler.create(
            es_client,
            embedding_provider=embedding_provider,
            index_name=resolved_index,
            dims=dims,
            preflight_timeout_s=preflight_timeout_s,
        )

        # Step 5: register for target "elasticsearch".
        registry = ProjectionHandlerRegistry({"elasticsearch": handler})

        # Step 6: store + worker.
        store = ProjectionOutboxStore(
            session_factory,
            worker_identity=worker_identity,
        )
        worker = ProjectionWorker(
            store=store,
            registry=registry,
            worker_identity=worker_identity,
            batch_size=batch_size,
            max_concurrency=max_concurrency,
            lease_duration=timedelta(seconds=lease_duration_s),
            delivery_timeout=timedelta(seconds=delivery_timeout_s),
            finalization_margin=timedelta(seconds=finalization_margin_s),
            retry_policy=ExponentialBackoffPolicy(
                base_delay_seconds=retry_base_delay_s,
            ),
            target_filter={"elasticsearch"},
        )

        # Step 7: polling loop.
        effective_stop = stop_event or asyncio.Event()
        logger.info(
            "elasticsearch worker starting: identity=%s index=%s",
            worker_identity,
            resolved_index,
        )
        await run_polling_loop(
            worker,
            effective_stop,
            idle_wait=timedelta(seconds=idle_wait_s),
        )

    except asyncio.CancelledError:
        logger.info("elasticsearch worker cancelled during startup or run")
        raise
    except Exception as exc:
        logger.error(
            "elasticsearch worker startup/run failed: %s",
            type(exc).__name__,
        )
        raise
    finally:
        # Step 8: close only resources we created.
        if handler is not None:
            try:
                await handler.close()
            except Exception as close_exc:
                logger.warning(
                    "elasticsearch worker: failed to close: %s",
                    type(close_exc).__name__,
                )
        else:
            try:
                await es_client.close()
            except Exception as close_exc:
                logger.warning(
                    "elasticsearch worker: failed to close raw client: %s",
                    type(close_exc).__name__,
                )


__all__ = [
    "compose_and_run_elasticsearch_worker",
    "compose_and_run_elasticsearch_worker_from_settings",
]
