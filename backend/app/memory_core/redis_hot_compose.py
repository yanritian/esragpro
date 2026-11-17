"""Production composition root for P1-05 Redis hot-state worker.

Creates the Redis client, performs Redis preflight BEFORE any outbox
claim, constructs exactly one Redis handler, registers it in the
``ProjectionHandlerRegistry``, constructs the PostgreSQL outbox store
and ``ProjectionWorker``, and runs the polling lifecycle.

Ownership:
- The composition root creates and owns the Redis client, handler,
  registry, and store/worker it builds.
- On normal exit, cancellation, or startup failure, the root closes
  ONLY the resources it created.
- A caller-supplied ``async_sessionmaker`` is NOT owned; the root
  never disposes it.

Configuration uses the repository's existing ``Settings`` for
``REDIS_URL`` and ``DATABASE_URL``; worker identity, batch size,
timeouts, and hot-state TTL are explicit parameters.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
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
from app.memory_core.redis_hot_handler import RedisHotStateHandler
from app.memory_core.repositories.redis_hot import (
    DEFAULT_HOT_TTL_SECONDS,
    DEFAULT_PREFIX,
    RedisHotStateWriter,
)

logger = logging.getLogger(__name__)


async def _ping_with_timeout(client: Redis, timeout_s: float) -> None:
    """Ping Redis with a bounded timeout; raises on failure."""
    await asyncio.wait_for(client.ping(), timeout=timeout_s)


async def compose_and_run_redis_hot_worker_from_settings(
    *,
    database_url: str,
    redis_url: str,
    worker_identity: str,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    prefix: str = DEFAULT_PREFIX,
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
    """Fully self-contained entrypoint.

    Creates the SQLAlchemy engine and ``async_sessionmaker`` from
    ``database_url``, then delegates to
    :func:`compose_and_run_redis_hot_worker`.

    The engine is created and disposed by this function. On normal
    exit, cancellation, or startup failure, the engine is disposed.
    """
    if not database_url or not database_url.strip():
        raise ValueError("database_url must be non-empty")

    engine = create_async_engine(database_url)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    try:
        await compose_and_run_redis_hot_worker(
            session_factory=factory,
            redis_url=redis_url,
            worker_identity=worker_identity,
            hot_ttl_seconds=hot_ttl_seconds,
            prefix=prefix,
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


async def compose_and_run_redis_hot_worker(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    redis_url: str,
    worker_identity: str,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    prefix: str = DEFAULT_PREFIX,
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
    """Compose and run a production Redis hot-state projection worker.

    Steps:
    1. Create a Redis client (owned by this function).
    2. Redis preflight ping BEFORE any outbox claim.
    3. Construct exactly one ``RedisHotStateHandler``.
    4. Register it in a ``ProjectionHandlerRegistry`` for target
       ``"redis"``.
    5. Construct ``ProjectionOutboxStore`` + ``ProjectionWorker``.
    6. Run the polling loop until ``stop_event`` is set or cancelled.
    7. On exit (normal, cancel, startup failure), close the Redis client.

    Raises:
        ConnectionError / redis.exceptions.AuthenticationError: on
            preflight failure, BEFORE any outbox claim.
        ValueError: on invalid parameters.
    """
    if not redis_url or not redis_url.strip():
        raise ValueError("redis_url must be non-empty")
    if not worker_identity or not worker_identity.strip():
        raise ValueError("worker_identity must be non-empty")

    # Step 1: create Redis client (owned).
    redis_client = Redis.from_url(redis_url, decode_responses=False)
    handler: Optional[RedisHotStateHandler] = None
    owns_redis = True

    try:
        # Step 2: Redis preflight BEFORE any outbox claim.
        await _ping_with_timeout(redis_client, preflight_timeout_s)

        # Step 3: exactly one Redis handler (owns the client).
        writer = RedisHotStateWriter(
            redis_client,
            prefix=prefix,
            hot_ttl_seconds=hot_ttl_seconds,
            owns_client=True,
        )
        handler = RedisHotStateHandler(writer)

        # Step 4: register in a registry; duplicate target is impossible
        # because we construct it from a single-key dict.
        registry = ProjectionHandlerRegistry({"redis": handler})

        # Step 5: store + worker.
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
            target_filter={"redis"},
        )

        # Step 6: polling loop.
        effective_stop = stop_event if stop_event is not None else asyncio.Event()
        logger.info(
            "redis hot worker starting: identity=%s prefix=%s",
            worker_identity,
            prefix,
        )
        await run_polling_loop(
            worker,
            effective_stop,
            idle_wait=timedelta(seconds=idle_wait_s),
        )

    except asyncio.CancelledError:
        logger.info("redis hot worker cancelled during startup or run")
        raise
    except Exception as exc:
        # Startup failure: log a safe fixed event with sanitized exception
        # class name only (no raw exception message, no credentials/endpoint
        # text). Preserve the original exception by re-raising it.
        logger.error(
            "redis hot worker startup/run failed: %s",
            type(exc).__name__,
        )
        raise
    finally:
        # Step 7: close only resources we created.
        if owns_redis:
            try:
                await redis_client.aclose()
            except Exception as close_exc:
                logger.warning(
                    "redis hot worker: failed to close redis client: %s",
                    type(close_exc).__name__,
                )


__all__ = [
    "compose_and_run_redis_hot_worker",
    "compose_and_run_redis_hot_worker_from_settings",
]
