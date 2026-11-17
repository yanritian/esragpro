"""Projection handler for ``target='redis'`` (P1-05).

Implements ``ProjectionHandler`` protocol. Delegates the actual
Redis work to :class:`RedisHotStateWriter`.

Never mutates PostgreSQL/outbox state. Never logs credentials
or raw payloads.
"""

from __future__ import annotations

import logging

from redis.asyncio import Redis

from app.memory_core.projection_worker.contracts import (
    ProjectionEvent,
)
from app.memory_core.repositories.redis_hot.writer import RedisHotStateWriter

logger = logging.getLogger(__name__)


class RedisHotStateHandler:
    """ProjectionHandler for target 'redis'.

    Client ownership:
    - ``create()`` builds a writer that owns the provided Redis client
      (``aclose()`` will close it).
    - Direct construction ``RedisHotStateHandler(writer)`` does NOT close
      the writer's client on ``aclose()`` unless the writer was built
      with ``owns_client=True``.
    """

    def __init__(self, writer: RedisHotStateWriter) -> None:
        if not isinstance(writer, RedisHotStateWriter):
            raise TypeError("writer must be a RedisHotStateWriter")
        self._writer = writer

    @classmethod
    async def create(
        cls,
        redis_client: Redis,
        *,
        prefix: str = "esragpro:memory:hot:v1",
        hot_ttl_seconds: int = 3600,
    ) -> "RedisHotStateHandler":
        writer = RedisHotStateWriter(
            redis_client,
            prefix=prefix,
            hot_ttl_seconds=hot_ttl_seconds,
            owns_client=True,
        )
        await writer.preflight_ping()
        return cls(writer)

    @property
    def writer(self) -> RedisHotStateWriter:
        return self._writer

    async def preflight_ping(self) -> None:
        await self._writer.preflight_ping()

    async def aclose(self) -> None:
        await self._writer.aclose()

    async def __call__(self, event: ProjectionEvent) -> None:
        payload = event.get_payload_copy()
        outcome = await self._writer.project(
            payload,
            envelope_idempotency_key=event.idempotency_key,
            envelope_aggregate_id=str(event.aggregate_id),
            envelope_event_type=event.event_type,
            envelope_schema_version=event.schema_version,
            envelope_target=event.projection_target,
        )
        logger.debug(
            "redis handler: event=%s outcome=%s",
            event.outbox_id,
            outcome,
        )


__all__ = ["RedisHotStateHandler"]
