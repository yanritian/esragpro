"""Key construction, strict payload models, and TTL helpers (P1-05).

Strict Pydantic validation rejects coercions such as schema_version="1".
Lua scripts live in ``script.py``; this module re-exports return codes
for backward compatibility.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PREFIX = "esragpro:memory:hot:v1"
DEFAULT_HOT_TTL_SECONDS = 3600
MAX_SESSION_ITEMS = 100
_SESSION_DIGEST_HEX_LEN = 16
READ_PAGE_WINDOW = 50
# Redis absolute expiry (PEXPIREAT) limit: signed 64-bit millisecond maximum.
# Retained as a defensive invariant — the stricter Lua-safe bound below is
# what actually governs pre-Lua rejection.
REDIS_MAX_EXPIRY_MS = (2**63) - 1
# Lua 5.1 (Redis's embedded interpreter) represents numbers as IEEE-754
# doubles. Integers above 2**53 cannot be represented exactly; tonumber()
# on such values silently loses precision. The production Lua script uses
# ``tonumber(ARGV[5])`` before ``PEXPIREAT``. The Python side must never
# pass an absolute-expiry integer above this bound — it would reach Lua
# as a lossy double, corrupting the TTL. This bound is stricter than
# REDIS_MAX_EXPIRY_MS; the signed-64 check is retained as defense-in-depth.
LUA_SAFE_MAX_EXPIRY_MS = (2**53) - 1


def _validate_hot_ttl_seconds(value, *, label: str = "hot_ttl_seconds") -> int:
    """Reject boolean, non-integer, non-finite, and non-positive TTL values.

    Returns the validated integer value. Raises PermanentPayloadError.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise PermanentPayloadError(f"{label}: must be an integer")
    if value <= 0:
        raise PermanentPayloadError(f"{label}: must be positive")
    return value


def _normalize_uuid(value) -> str:
    return UUID(str(value)).hex


def _validate_uuid_field(value: Optional[str], label: str) -> Optional[UUID]:
    """Parse a UUID string or raise PermanentPayloadError."""
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        raise PermanentPayloadError(f"{label}: invalid UUID")


def item_key(prefix: str, memory_id) -> str:
    return f"{prefix}:item:{_normalize_uuid(memory_id)}"


def session_key(prefix: str, session_id) -> str:
    digest = hashlib.sha256(_normalize_uuid(session_id).encode("ascii")).hexdigest()
    return f"{prefix}:sess:{digest[:_SESSION_DIGEST_HEX_LEN]}"


def build_keys(prefix: str, memory_id, session_id) -> tuple[str, Optional[str]]:
    ik = item_key(prefix, memory_id)
    sk = session_key(prefix, session_id) if session_id is not None else None
    return ik, sk


class _SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str
    content: str
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    expires_at: Optional[str] = None
    project_id: Optional[str] = None
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    knowledge_base_id: Optional[str] = None
    provenance: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)


class HotStatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: str
    projection_target: str
    occurred_at: str
    memory_snapshot: _SnapshotModel


class PermanentPayloadError(ValueError):
    """Malformed/contradictory timestamps or TTL contracts."""


def _parse_utc_ms(value, label: str):
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PermanentPayloadError(f"{label}: malformed ISO-8601") from exc
    if dt.tzinfo is None:
        raise PermanentPayloadError(f"{label}: naive datetime")
    dt_utc = dt.astimezone(timezone.utc)
    try:
        epoch = dt_utc.timestamp()
    except (OverflowError, OSError, ValueError) as exc:
        raise PermanentPayloadError(f"{label}: overflow") from exc
    if not math.isfinite(epoch):
        raise PermanentPayloadError(f"{label}: non-finite")
    try:
        return int(epoch * 1000)
    except (OverflowError, ValueError) as exc:
        raise PermanentPayloadError(f"{label}: ms overflow") from exc


def effective_expiry_ms(
    *,
    snapshot_expires_at_ms,
    occurred_at_ms: int,
    hot_ttl_seconds: int,
) -> int:
    _validate_hot_ttl_seconds(hot_ttl_seconds)
    if not isinstance(occurred_at_ms, int) or isinstance(occurred_at_ms, bool):
        raise PermanentPayloadError("occurred_at_ms: must be an integer")
    ttl_boundary = occurred_at_ms + hot_ttl_seconds * 1000
    if snapshot_expires_at_ms is None:
        abs_exp = ttl_boundary
    else:
        abs_exp = min(snapshot_expires_at_ms, ttl_boundary)
    # Lua-safe exact-integer bound: Lua 5.1 uses IEEE-754 doubles, so
    # integers above 2**53-1 lose precision under tonumber(). The
    # production script calls ``tonumber(ARGV[5])`` before PEXPIREAT.
    # Reject before Lua execution — a local, deterministic, permanent
    # payload error, not a Lua runtime error.
    if abs_exp > LUA_SAFE_MAX_EXPIRY_MS:
        raise PermanentPayloadError(
            "computed expiry exceeds Lua-safe exact-integer ms range "
            f"(limit={LUA_SAFE_MAX_EXPIRY_MS})",
        )
    # Defensive invariant: the signed-64-bit Redis limit. The Lua-safe
    # bound is stricter, so this branch is unreachable in practice; it
    # remains as defense-in-depth should LUA_SAFE_MAX_EXPIRY_MS ever
    # be loosened.
    if abs_exp > REDIS_MAX_EXPIRY_MS:
        raise PermanentPayloadError(
            "computed expiry exceeds Redis signed-64-bit ms range",
        )
    return abs_exp


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class HotItemRead:
    """Typed freshness result for a single hot-state item."""

    snapshot: dict[str, Any]
    pttl_ms: Optional[int]

    @property
    def is_stale(self) -> bool:
        return self.pttl_ms is None or self.pttl_ms <= 0


@dataclass(frozen=True)
class HotSessionRead:
    """Typed freshness result for a session listing."""

    items: list[HotItemRead]
    session_pttl_ms: Optional[int]


# Re-export Lua return codes from script module for backward compat.
from app.memory_core.repositories.redis_hot.script import (  # noqa: E402
    LUA_RC_CONFLICT,
    LUA_RC_IDEMPOTENT,
    LUA_RC_OK,
    get_atomic_script,
)


__all__ = [
    "DEFAULT_HOT_TTL_SECONDS",
    "DEFAULT_PREFIX",
    "HotItemRead",
    "HotSessionRead",
    "HotStatePayload",
    "LUA_RC_CONFLICT",
    "LUA_RC_IDEMPOTENT",
    "LUA_RC_OK",
    "LUA_SAFE_MAX_EXPIRY_MS",
    "MAX_SESSION_ITEMS",
    "READ_PAGE_WINDOW",
    "REDIS_MAX_EXPIRY_MS",
    "PermanentPayloadError",
    "build_keys",
    "canonical_json",
    "effective_expiry_ms",
    "get_atomic_script",
    "item_key",
    "session_key",
]
