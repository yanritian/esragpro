"""Hot-path typed read adapter for Redis hot-state (P1-05).

Returns parsed snapshots with freshness metadata. Never fabricates
data. Lazily removes stale ZSET members on read. All reads are
bounded — no ``ZREVRANGE 0 -1``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from redis.asyncio import Redis

from app.memory_core.repositories.redis_hot.keys import (
    DEFAULT_PREFIX,
    MAX_SESSION_ITEMS,
    HotItemRead,
    HotSessionRead,
    READ_PAGE_WINDOW,
    item_key,
    session_key,
)

logger = logging.getLogger(__name__)

_BOUNDED_SCAN_BUDGET = MAX_SESSION_ITEMS * 2


def _decode(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _validate_positive_limit(limit: int) -> int:
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    return min(limit, MAX_SESSION_ITEMS)


def _is_valid_uuid_str(s: str) -> bool:
    """Check if s is a parseable UUID string (with or without dashes)."""
    try:
        from uuid import UUID

        UUID(s)
        return True
    except (TypeError, ValueError):
        return False


class RedisHotStateReader:
    """Typed read adapter for the hot-state projection."""

    def __init__(self, redis_client, *, prefix: str = DEFAULT_PREFIX) -> None:
        if not isinstance(redis_client, Redis):
            raise TypeError("redis_client must be a redis.asyncio.Redis instance")
        if not prefix or not prefix.strip():
            raise ValueError("prefix must be non-empty")
        self._client = redis_client
        self._prefix = prefix

    async def get_item(self, memory_id) -> Optional[dict[str, Any]]:
        result = await self.get_item_typed(memory_id)
        return result.snapshot if result is not None else None

    async def get_item_typed(self, memory_id) -> Optional[HotItemRead]:
        ik = item_key(self._prefix, memory_id)
        pipe = self._client.pipeline(transaction=False)
        pipe.hget(ik, "_snapshot")
        pipe.pttl(ik)
        snap_raw, pttl = await pipe.execute()
        if snap_raw is None:
            return None
        # Treat missing or non-positive PTTL as absent state even when
        # the snapshot was fetched (race between TTL expiry and read).
        if pttl is None or pttl <= 0:
            return None
        try:
            snap = json.loads(_decode(snap_raw))
        except (TypeError, ValueError):
            logger.warning("redis reader: corrupt snapshot for item")
            return None
        return HotItemRead(snapshot=snap, pttl_ms=int(pttl))

    async def pttl_ms(self, memory_id) -> Optional[int]:
        ik = item_key(self._prefix, memory_id)
        pttl = await self._client.pttl(ik)
        if pttl is None or pttl < 0:
            return None
        return int(pttl)

    async def list_session_items(
        self,
        session_id,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        result = await self.list_session_items_typed(
            session_id,
            limit=limit,
        )
        return [r.snapshot for r in result.items]

    async def list_session_items_typed(
        self,
        session_id,
        *,
        limit: int = 20,
    ) -> HotSessionRead:
        limit = _validate_positive_limit(limit)
        sk = session_key(self._prefix, session_id)

        results: list[HotItemRead] = []
        stale: list[str] = []
        offset = 0
        scanned = 0

        while len(results) < limit and scanned < _BOUNDED_SCAN_BUDGET:
            window = min(READ_PAGE_WINDOW, _BOUNDED_SCAN_BUDGET - scanned)
            page_members = await self._client.zrange(
                sk,
                offset,
                offset + window - 1,
                desc=True,
            )
            if not page_members:
                break
            scanned += len(page_members)
            offset += window

            member_ids = [_decode(m) for m in page_members]

            valid_ids = [m for m in member_ids if _is_valid_uuid_str(m)]
            invalid_ids = [m for m in member_ids if not _is_valid_uuid_str(m)]
            if invalid_ids:
                stale.extend(invalid_ids)

            if not valid_ids:
                continue

            pipe = self._client.pipeline(transaction=False)
            for mid in valid_ids:
                ik = item_key(self._prefix, mid)
                pipe.hget(ik, "_snapshot")
                pipe.pttl(ik)
            pipe_results = await pipe.execute()

            for i, mid in enumerate(valid_ids):
                snap_raw = pipe_results[i * 2]
                pttl_raw = pipe_results[i * 2 + 1]
                if snap_raw is None:
                    stale.append(mid)
                    continue
                # Treat missing or non-positive PTTL as absent/stale
                # even when the snapshot was fetched (race between TTL
                # expiry and read). Enqueue for lazy ZSET removal.
                if pttl_raw is None or pttl_raw <= 0:
                    stale.append(mid)
                    continue
                try:
                    snap = json.loads(_decode(snap_raw))
                except (TypeError, ValueError):
                    stale.append(mid)
                    continue
                results.append(HotItemRead(snapshot=snap, pttl_ms=int(pttl_raw)))
                if len(results) >= limit:
                    break

        if stale:
            try:
                await self._client.zrem(sk, *stale)
            except Exception:
                logger.warning("redis reader: failed to remove stale members")

        sess_pttl = await self._client.pttl(sk)
        sess_pttl_int = int(sess_pttl) if sess_pttl is not None and sess_pttl >= 0 else None
        return HotSessionRead(items=results, session_pttl_ms=sess_pttl_int)


__all__ = ["RedisHotStateReader"]
