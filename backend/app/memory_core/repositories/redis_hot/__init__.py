"""Redis 7 hot-state projection package (P1-05).

Sub-modules:
- ``keys``: deterministic key construction, payload models, TTL helpers
- ``script``: Lua scripts for atomic projection and failure injection
- ``writer``: atomic Lua-scripted writer
- ``reader``: typed read adapter
- ``rebuild``: deterministic PostgreSQL-to-Redis rebuild service
"""

from app.memory_core.repositories.redis_hot.keys import (
    DEFAULT_HOT_TTL_SECONDS,
    DEFAULT_PREFIX,
    HotItemRead,
    HotSessionRead,
    LUA_SAFE_MAX_EXPIRY_MS,
    MAX_SESSION_ITEMS,
    READ_PAGE_WINDOW,
    HotStatePayload,
    PermanentPayloadError,
    build_keys,
    canonical_json,
    effective_expiry_ms,
    item_key,
    session_key,
)
from app.memory_core.repositories.redis_hot.reader import RedisHotStateReader
from app.memory_core.repositories.redis_hot.rebuild import (
    RebuildCounters,
    RebuildInfrastructureError,
    rebuild_from_postgres,
)
from app.memory_core.repositories.redis_hot.script import (
    ATOMIC_PROJECTION_SCRIPT,
    PRE_WRITE_FAILURE_INJECTION_SCRIPT,
    WRITE_THEN_ERROR_SEMANTICS_SCRIPT,
    LUA_RC_CONFLICT,
    LUA_RC_IDEMPOTENT,
    LUA_RC_OK,
    get_atomic_script,
    get_pre_write_failure_script,
    get_write_then_error_script,
)
from app.memory_core.repositories.redis_hot.writer import RedisHotStateWriter

__all__ = [
    "ATOMIC_PROJECTION_SCRIPT",
    "PRE_WRITE_FAILURE_INJECTION_SCRIPT",
    "WRITE_THEN_ERROR_SEMANTICS_SCRIPT",
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
    "PermanentPayloadError",
    "RebuildCounters",
    "RebuildInfrastructureError",
    "RedisHotStateReader",
    "RedisHotStateWriter",
    "build_keys",
    "canonical_json",
    "effective_expiry_ms",
    "get_atomic_script",
    "get_pre_write_failure_script",
    "get_write_then_error_script",
    "item_key",
    "rebuild_from_postgres",
    "session_key",
]
