"""Production composition root for P1-07 Neo4j projection worker.

Creates the async Neo4j driver, performs Neo4j preflight BEFORE any
outbox claim, constructs exactly one Neo4j handler, registers it in
the ProjectionHandlerRegistry, constructs the PostgreSQL outbox store
and ProjectionWorker, and runs the polling lifecycle.

Ownership:
- The composition root creates and owns the Neo4j driver, handler,
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
from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.memory_core.neo4j_handler import Neo4jProjectionHandler
from app.memory_core.projection_worker.contracts import (
    ProjectionHandlerRegistry,
)
from app.memory_core.projection_worker.polling import run_polling_loop
from app.memory_core.projection_worker.postgres_store import (
    ProjectionOutboxStore,
)
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.projection_worker.worker import ProjectionWorker

logger = logging.getLogger(__name__)


async def compose_and_run_neo4j_worker_from_settings(
    *,
    database_url: str,
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
    worker_identity: str,
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
    :func:`compose_and_run_neo4j_worker`.
    """
    if not database_url or not database_url.strip():
        raise ValueError("database_url must be non-empty")

    engine = create_async_engine(database_url)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    try:
        await compose_and_run_neo4j_worker(
            session_factory=factory,
            neo4j_uri=neo4j_uri,
            neo4j_user=neo4j_user,
            neo4j_password=neo4j_password,
            worker_identity=worker_identity,
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


async def compose_and_run_neo4j_worker(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
    worker_identity: str,
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
    """Compose and run a production Neo4j projection worker.

    Steps:
    1. Create a Neo4jProjectionHandler (owned by this function).
       The handler's ``create()`` performs Neo4j preflight BEFORE
       any outbox claim, and creates graph constraints.
    2. Register it for target ``"neo4j"``.
    3. Construct ``ProjectionOutboxStore`` + ``ProjectionWorker``.
    4. Run the polling loop until ``stop_event`` or cancelled.
    5. On exit, close the handler (and the driver it owns).
    """
    if not neo4j_uri or not neo4j_uri.strip():
        raise ValueError("neo4j_uri must be non-empty")
    if not neo4j_user or not neo4j_user.strip():
        raise ValueError("neo4j_user must be non-empty")
    if not worker_identity or not worker_identity.strip():
        raise ValueError("worker_identity must be non-empty")

    handler: Optional[Neo4jProjectionHandler] = None

    try:
        # Step 1: Create handler with owned driver.
        # create() performs preflight and constraint setup.
        handler = await Neo4jProjectionHandler.create(
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
            preflight_timeout_s=preflight_timeout_s,
        )

        # Step 2: Register for target "neo4j".
        registry = ProjectionHandlerRegistry({"neo4j": handler})

        # Step 3: Store + worker.
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
            target_filter={"neo4j"},
        )

        # Step 4: Polling loop.
        effective_stop = stop_event or asyncio.Event()
        logger.info(
            "neo4j worker starting: identity=%s uri=%s",
            worker_identity,
            neo4j_uri,
        )
        await run_polling_loop(
            worker,
            effective_stop,
            idle_wait=timedelta(seconds=idle_wait_s),
        )

    except asyncio.CancelledError:
        logger.info("neo4j worker cancelled during startup or run")
        raise
    except Exception as exc:
        logger.error(
            "neo4j worker startup/run failed: %s",
            type(exc).__name__,
        )
        raise
    finally:
        # Step 5: Close only resources we created.
        if handler is not None:
            try:
                await handler.close()
            except Exception as close_exc:
                logger.warning(
                    "neo4j worker: failed to close handler: %s",
                    type(close_exc).__name__,
                )


__all__ = [
    "compose_and_run_neo4j_worker",
    "compose_and_run_neo4j_worker_from_settings",
]
