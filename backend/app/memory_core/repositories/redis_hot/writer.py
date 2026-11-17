"""Atomic Redis hot-state writer (P1-05).

Uses a single Lua script for HASH + optional ZSET write with absolute
TTL via ``PEXPIREAT``. Idempotent delivery and conflict detection.

The session ZSET TTL is monotonically non-decreasing: a new item may
extend the session-key expiry but never shorten it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from redis.asyncio import Redis

from app.memory_core.repositories.postgres_outbox import (
    AGGREGATE_TYPE,
    EVENT_TYPE_REMEMBERED,
    SCHEMA_VERSION,
    build_idempotency_key,
)
from app.memory_core.repositories.redis_hot.keys import (
    DEFAULT_HOT_TTL_SECONDS,
    DEFAULT_PREFIX,
    HotStatePayload,
    PermanentPayloadError,
    _parse_utc_ms,
    _validate_hot_ttl_seconds,
    _validate_uuid_field,
    build_keys,
    canonical_json,
    effective_expiry_ms,
)
from app.memory_core.repositories.redis_hot.script import (
    LUA_RC_CONFLICT,
    LUA_RC_IDEMPOTENT,
    LUA_RC_OK,
    get_atomic_script,
)

logger = logging.getLogger(__name__)


def _get_permanent_error_class():
    """Lazy import to avoid circular dependency."""
    from app.memory_core.projection_worker.contracts import (
        PermanentProjectionError,
    )

    return PermanentProjectionError


_ppe = None


def _PPE_cls():
    global _ppe
    if _ppe is None:
        _ppe = _get_permanent_error_class()
    return _ppe


class RedisHotStateWriter:
    """Atomic writer for Redis hot-state projection (P1-05).

    Client ownership: by default the writer does NOT own the client.
    Set ``owns_client=True`` if the caller transfers ownership.
    """

    def __init__(
        self,
        redis_client,
        *,
        prefix: str = DEFAULT_PREFIX,
        hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
        owns_client: bool = False,
    ) -> None:
        if not isinstance(redis_client, Redis):
            raise TypeError("redis_client must be a redis.asyncio.Redis instance")
        if not prefix or not prefix.strip():
            raise _PPE_cls()("prefix must be non-empty")
        # Validate TTL before any Redis interaction (reject bool/non-int/non-positive).
        try:
            _validate_hot_ttl_seconds(hot_ttl_seconds)
        except PermanentPayloadError as exc:
            raise _PPE_cls()(str(exc)) from exc
        self._client = redis_client
        self._prefix = prefix
        self._hot_ttl_seconds = hot_ttl_seconds
        self._owns_client = owns_client
        self._script = self._client.register_script(get_atomic_script())

    @property
    def prefix(self) -> str:
        return self._prefix

    @property
    def hot_ttl_seconds(self) -> int:
        return self._hot_ttl_seconds

    @property
    def owns_client(self) -> bool:
        return self._owns_client

    async def preflight_ping(self) -> None:
        await self._client.ping()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _validate_payload(
        self,
        payload: Mapping[str, Any],
        *,
        envelope_idempotency_key: str,
        envelope_aggregate_id: str,
        envelope_event_type: str,
        envelope_schema_version: int,
        envelope_target: str,
    ) -> tuple[HotStatePayload, UUID, int, int]:
        """Validate payload and cross-check envelope. Returns parsed data.

        Raises PermanentProjectionError for all data errors.
        Never logs payload content or raw exception messages.
        """
        try:
            parsed = HotStatePayload.model_validate(payload)
        except Exception:
            raise _PPE_cls()(
                "redis projection: malformed payload",
            )

        # Cross-check envelope vs payload constants
        if parsed.event_type != envelope_event_type:
            raise _PPE_cls()(
                "redis projection: event_type mismatch envelope vs payload",
            )
        if parsed.schema_version != envelope_schema_version:
            raise _PPE_cls()(
                "redis projection: schema_version mismatch envelope vs payload",
            )
        if parsed.projection_target != envelope_target:
            raise _PPE_cls()(
                "redis projection: projection_target mismatch envelope vs payload",
            )

        # Validate semantic constraints
        if parsed.event_type != EVENT_TYPE_REMEMBERED:
            raise _PPE_cls()(
                "redis projection: unsupported event_type",
            )
        if parsed.schema_version != SCHEMA_VERSION:
            raise _PPE_cls()(
                "redis projection: unsupported schema_version",
            )
        if parsed.aggregate_type != AGGREGATE_TYPE:
            raise _PPE_cls()(
                "redis projection: unsupported aggregate_type",
            )
        if parsed.projection_target != "redis":
            raise _PPE_cls()(
                "redis projection: unsupported target",
            )

        # Cross-check aggregate ID
        if parsed.aggregate_id != envelope_aggregate_id:
            raise _PPE_cls()(
                "redis projection: aggregate_id mismatch envelope vs payload",
            )

        # Validate UUID fields
        try:
            agg_uuid = UUID(parsed.aggregate_id)
        except (TypeError, ValueError):
            raise _PPE_cls()(
                "redis projection: aggregate_id is not a valid UUID",
            )

        expected_idem = build_idempotency_key(agg_uuid, "redis")
        if expected_idem != envelope_idempotency_key:
            raise _PPE_cls()(
                "redis projection: idempotency_key mismatch",
            )

        # Cross-check snapshot.id vs aggregate_id
        if parsed.memory_snapshot.id != parsed.aggregate_id:
            raise _PPE_cls()(
                "redis projection: snapshot.id does not match aggregate_id",
            )

        # Validate snapshot UUID dimension fields
        snap = parsed.memory_snapshot
        try:
            for label, val in [
                ("snapshot.project_id", snap.project_id),
                ("snapshot.session_id", snap.session_id),
                ("snapshot.agent_id", snap.agent_id),
                ("snapshot.knowledge_base_id", snap.knowledge_base_id),
            ]:
                _validate_uuid_field(val, label)
        except PermanentPayloadError as exc:
            raise _PPE_cls()(str(exc)) from exc

        # Parse timestamps
        try:
            occurred_at_ms = _parse_utc_ms(parsed.occurred_at, "occurred_at")
            if occurred_at_ms is None:
                raise _PPE_cls()(
                    "redis projection: occurred_at is required",
                )
            snapshot_expires_ms = _parse_utc_ms(
                snap.expires_at,
                "memory_snapshot.expires_at",
            )
        except PermanentPayloadError as exc:
            raise _PPE_cls()(str(exc)) from exc

        try:
            abs_exp = effective_expiry_ms(
                snapshot_expires_at_ms=snapshot_expires_ms,
                occurred_at_ms=occurred_at_ms,
                hot_ttl_seconds=self._hot_ttl_seconds,
            )
        except PermanentPayloadError as exc:
            raise _PPE_cls()(str(exc)) from exc

        return parsed, agg_uuid, occurred_at_ms, abs_exp

    async def project(
        self,
        payload: Mapping[str, Any],
        *,
        envelope_idempotency_key: str,
        envelope_aggregate_id: str,
        envelope_event_type: str,
        envelope_schema_version: int,
        envelope_target: str,
    ) -> str:
        parsed, agg_uuid, occurred_at_ms, abs_exp = self._validate_payload(
            payload,
            envelope_idempotency_key=envelope_idempotency_key,
            envelope_aggregate_id=envelope_aggregate_id,
            envelope_event_type=envelope_event_type,
            envelope_schema_version=envelope_schema_version,
            envelope_target=envelope_target,
        )

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if abs_exp <= now_ms:
            logger.info("redis projection: event already expired, skipping")
            return "expired"

        snapshot_dict = parsed.memory_snapshot.model_dump()
        canonical = canonical_json(snapshot_dict)
        sess_uuid = _validate_uuid_field(
            parsed.memory_snapshot.session_id,
            "snapshot.session_id",
        )
        ik, sk = build_keys(self._prefix, agg_uuid, sess_uuid)
        has_session = 1 if sk else 0

        rc = await self._script(
            keys=[ik, sk or ""],
            args=[
                envelope_idempotency_key,
                canonical,
                occurred_at_ms,
                parsed.aggregate_id,
                abs_exp,
                has_session,
            ],
        )

        rc_int = int(rc)
        if rc_int == LUA_RC_OK:
            return "written"
        if rc_int == LUA_RC_IDEMPOTENT:
            return "idempotent"
        if rc_int == LUA_RC_CONFLICT:
            raise _PPE_cls()(
                "redis projection: idempotency key conflict on existing item",
            )
        raise _PPE_cls()(
            "redis projection: unexpected Lua return code",
        )


__all__ = ["RedisHotStateWriter"]
