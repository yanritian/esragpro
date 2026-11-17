"""Production composition root for P1-08 MinIO projection worker.

Creates the MinIO client, performs MinIO preflight BEFORE any
outbox claim, constructs exactly one MinIO handler, registers it in
the ProjectionHandlerRegistry, constructs the PostgreSQL outbox store
and ProjectionWorker, and runs the polling lifecycle.

Ownership:
- The composition root creates and owns the MinIO client, handler,
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

from app.memory_core.minio_handler import MinioProjectionHandler
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


async def compose_and_run_minio_worker_from_settings(
    *,
    database_url: str,
    minio_endpoint: str,
    minio_access_key: str,
    minio_secret_key: str,
    minio_bucket_name: str = "memory-evidence",
    minio_secure: bool = False,
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
    :func:`compose_and_run_minio_worker`.
    """
    if not database_url or not database_url.strip():
        raise ValueError("database_url must be non-empty")

    engine = create_async_engine(database_url)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    try:
        await compose_and_run_minio_worker(
            session_factory=factory,
            minio_endpoint=minio_endpoint,
            minio_access_key=minio_access_key,
            minio_secret_key=minio_secret_key,
            minio_bucket_name=minio_bucket_name,
            minio_secure=minio_secure,
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


async def compose_and_run_minio_worker(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    minio_endpoint: str,
    minio_access_key: str,
    minio_secret_key: str,
    minio_bucket_name: str = "memory-evidence",
    minio_secure: bool = False,
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
    """Compose and run a production MinIO projection worker.

    Steps:
    1. Create a MinioProjectionHandler (owned by this function).
       The handler's ``create()`` performs MinIO preflight BEFORE
       any outbox claim, and ensures the bucket exists.
    2. Register it for target ``"minio"``.
    3. Construct ``ProjectionOutboxStore`` + ``ProjectionWorker``.
    4. Run the polling loop until ``stop_event`` or cancelled.
    5. On exit, close the handler (and the client it owns).
    """
    if not minio_endpoint or not minio_endpoint.strip():
        raise ValueError("minio_endpoint must be non-empty")
    if not minio_access_key or not minio_access_key.strip():
        raise ValueError("minio_access_key must be non-empty")
    if not minio_secret_key or not minio_secret_key.strip():
        raise ValueError("minio_secret_key must be non-empty")
    if not worker_identity or not worker_identity.strip():
        raise ValueError("worker_identity must be non-empty")

    handler: Optional[MinioProjectionHandler] = None

    try:
        # Step 1: Create handler with owned client.
        # create() performs preflight and bucket setup.
        handler = await MinioProjectionHandler.create(
            endpoint=minio_endpoint,
            access_key=minio_access_key,
            secret_key=minio_secret_key,
            bucket_name=minio_bucket_name,
            secure=minio_secure,
            preflight_timeout_s=preflight_timeout_s,
        )

        # Step 2: Register for target "minio".
        registry = ProjectionHandlerRegistry({"minio": handler})

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
            target_filter={"minio"},
        )

        # Step 4: Polling loop.
        effective_stop = stop_event or asyncio.Event()
        logger.info(
            "minio worker starting: identity=%s endpoint=%s",
            worker_identity,
            minio_endpoint,
        )
        await run_polling_loop(
            worker,
            effective_stop,
            idle_wait=timedelta(seconds=idle_wait_s),
        )

    except asyncio.CancelledError:
        logger.info("minio worker cancelled during startup or run")
        raise
    except Exception as exc:
        logger.error(
            "minio worker startup/run failed: %s",
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
                    "minio worker: failed to close handler: %s",
                    type(close_exc).__name__,
                )


__all__ = [
    "compose_and_run_minio_worker",
    "compose_and_run_minio_worker_from_settings",
]
