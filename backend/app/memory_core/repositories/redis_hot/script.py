"""Lua scripts for atomic Redis hot-state projection (P1-05).

Kept in a dedicated module so ``keys.py`` stays focused on key
construction, payload models, and TTL helpers.

The production script performs all deterministic validation (idempotency
and conflict checks) BEFORE the first write, then executes a fixed
post-validation write phase (HASH + optional ZSET + TTL) over validated
arguments. This matters because Redis Lua runtime errors do NOT roll
back writes already executed: any failure after a write would leave
partial state.

Return codes (production script):
  0 = written (new item)
  1 = idempotent (same idempotency key, already written)
  2 = conflict  (different idempotency key on existing item)

Test-only scripts:
- ``PRE_WRITE_FAILURE_INJECTION_SCRIPT``: raises before any write.
  Used to prove the post-validation contract: zero state visible.
- ``WRITE_THEN_ERROR_SEMANTICS_SCRIPT``: writes first, then errors.
  Used as a SEMANTICS regression proving Redis Lua does NOT roll back:
  the earlier write IS visible. This prevents future maintainers from
  relying on nonexistent rollback.
"""

from __future__ import annotations

LUA_RC_OK = 0
LUA_RC_IDEMPOTENT = 1
LUA_RC_CONFLICT = 2


# Production script: all validation before first write, then a fixed
# post-validation write phase.
ATOMIC_PROJECTION_SCRIPT = (
    "local item_key = KEYS[1]\n"
    "local sess_key = KEYS[2]\n"
    "local idem = ARGV[1]\n"
    "local snap = ARGV[2]\n"
    "local score = tonumber(ARGV[3])\n"
    "local member = ARGV[4]\n"
    "local abs_exp = tonumber(ARGV[5])\n"
    "local has_session = tonumber(ARGV[6])\n"
    "\n"
    "-- Phase 1: deterministic validation (no writes yet).\n"
    "local existing = redis.call('HGET', item_key, '_idempotency_key')\n"
    "local is_idempotent = 0\n"
    "if existing then\n"
    "    if existing == idem then\n"
    "        is_idempotent = 1\n"
    "    else\n"
    "        return 2\n"
    "    end\n"
    "end\n"
    "\n"
    "-- Phase 2: fixed post-validation write phase.\n"
    "if is_idempotent == 0 then\n"
    "    redis.call('HSET', item_key,\n"
    "        '_idempotency_key', idem,\n"
    "        '_snapshot', snap)\n"
    "    redis.call('PEXPIREAT', item_key, abs_exp)\n"
    "end\n"
    "\n"
    "if has_session == 1 and sess_key ~= '' then\n"
    "    redis.call('ZADD', sess_key, score, member)\n"
    "    local cur_pttl = redis.call('PTTL', sess_key)\n"
    "    if cur_pttl == -2 then\n"
    "        -- Key does not exist (ZADD just created it without TTL).\n"
    "        redis.call('PEXPIREAT', sess_key, abs_exp)\n"
    "    elseif cur_pttl == -1 then\n"
    "        -- Key exists without TTL: must receive absolute expiry.\n"
    "        redis.call('PEXPIREAT', sess_key, abs_exp)\n"
    "    else\n"
    "        -- Key exists with TTL: only extend, never shorten.\n"
    "        local time_arr = redis.call('TIME')\n"
    "        local now_ms = tonumber(time_arr[1]) * 1000"
    " + math.floor(tonumber(time_arr[2]) / 1000)\n"
    "        local sess_abs = now_ms + cur_pttl\n"
    "        if abs_exp > sess_abs then\n"
    "            redis.call('PEXPIREAT', sess_key, abs_exp)\n"
    "        end\n"
    "    end\n"
    "end\n"
    "\n"
    "if is_idempotent == 1 then return 1 end\n"
    "return 0\n"
)


# Pre-write failure injection: raises BEFORE any write command.
# Proves the post-validation contract: when a failure occurs before the
# write phase, neither HASH nor ZSET is visible.
PRE_WRITE_FAILURE_INJECTION_SCRIPT = "return redis.error_reply('INJECTED_PRE_WRITE_FAILURE')\n"


# Write-then-error semantics regression: performs a write, then raises.
# Proves Redis Lua does NOT roll back writes on runtime error — the
# earlier HSET IS visible after the error. This is documentation for
# future maintainers so they never rely on nonexistent rollback.
WRITE_THEN_ERROR_SEMANTICS_SCRIPT = (
    "local item_key = KEYS[1]\n"
    "redis.call('HSET', item_key, '_marker', 'visible')\n"
    "return redis.error_reply('INJECTED_POST_WRITE_FAILURE')\n"
)


def get_atomic_script() -> str:
    """Return the production atomic projection Lua script."""
    return ATOMIC_PROJECTION_SCRIPT


def get_pre_write_failure_script() -> str:
    """Return the pre-write failure injection script for atomicity tests."""
    return PRE_WRITE_FAILURE_INJECTION_SCRIPT


def get_write_then_error_script() -> str:
    """Return the write-then-error semantics regression script."""
    return WRITE_THEN_ERROR_SEMANTICS_SCRIPT


__all__ = [
    "ATOMIC_PROJECTION_SCRIPT",
    "PRE_WRITE_FAILURE_INJECTION_SCRIPT",
    "WRITE_THEN_ERROR_SEMANTICS_SCRIPT",
    "LUA_RC_CONFLICT",
    "LUA_RC_IDEMPOTENT",
    "LUA_RC_OK",
    "get_atomic_script",
    "get_pre_write_failure_script",
    "get_write_then_error_script",
]
