"""Unit tests for Redis hot-state projection (P1-05).

Covers key construction, payload validation, TTL math, canonical JSON,
envelope cross-checks, typed freshness results, rebuild counters,
Lua return-code classification, and credential log-leak regression.
Uses fakes for Redis and PostgreSQL.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.memory_core.projection_worker.contracts import PermanentProjectionError
from app.memory_core.repositories.redis_hot import (
    DEFAULT_PREFIX,
    HotItemRead,
    HotSessionRead,
    HotStatePayload,
    PermanentPayloadError,
    canonical_json,
    effective_expiry_ms,
    item_key,
    session_key,
)
from app.memory_core.repositories.redis_hot.keys import (
    READ_PAGE_WINDOW,
    _validate_uuid_field,
    build_keys,
)
from app.memory_core.repositories.redis_hot.rebuild import RebuildCounters


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _make_payload(
    *,
    memory_id=None,
    session_id=None,
    expires_at=None,
    occurred_at=None,
    extra_snapshot_fields=None,
    extra_envelope_fields=None,
):
    memory_id = memory_id or uuid4()
    now = occurred_at or datetime.now(timezone.utc)
    snapshot = {
        "id": str(memory_id),
        "content": "hello world",
        "status": "active",
        "created_at": _iso_utc(now),
        "updated_at": _iso_utc(now),
        "expires_at": _iso_utc(expires_at) if expires_at else None,
        "project_id": None,
        "session_id": str(session_id) if session_id else None,
        "agent_id": None,
        "knowledge_base_id": None,
        "provenance": None,
        "metadata": {},
        "evidence_refs": [],
    }
    if extra_snapshot_fields:
        snapshot.update(extra_snapshot_fields)
    payload = {
        "event_type": "memory.remembered",
        "schema_version": 1,
        "aggregate_type": "memory_item",
        "aggregate_id": str(memory_id),
        "projection_target": "redis",
        "occurred_at": _iso_utc(now),
        "memory_snapshot": snapshot,
    }
    if extra_envelope_fields:
        payload.update(extra_envelope_fields)
    return payload


# ---------------------------------------------------------------------------
# Key construction
# ---------------------------------------------------------------------------


class TestKeyConstruction:
    def test_item_key_is_deterministic_and_bounded(self) -> None:
        mid = uuid4()
        k1 = item_key(DEFAULT_PREFIX, mid)
        k2 = item_key(DEFAULT_PREFIX, str(mid))
        assert k1 == k2
        assert k1.startswith("esragpro:memory:hot:v1:item:")
        assert len(k1) == len("esragpro:memory:hot:v1:item:") + 32

    def test_session_key_uses_sha256_digest_not_raw(self) -> None:
        sid = uuid4()
        k = session_key(DEFAULT_PREFIX, sid)
        digest = hashlib.sha256(sid.hex.encode("ascii")).hexdigest()[:16]
        assert k == f"esragpro:memory:hot:v1:sess:{digest}"
        assert str(sid) not in k

    def test_build_keys_no_session(self) -> None:
        ik, sk = build_keys(DEFAULT_PREFIX, uuid4(), None)
        assert ik.startswith("esragpro:memory:hot:v1:item:")
        assert sk is None

    def test_build_keys_with_session(self) -> None:
        ik, sk = build_keys(DEFAULT_PREFIX, uuid4(), uuid4())
        assert sk is not None
        assert sk.startswith("esragpro:memory:hot:v1:sess:")


# ---------------------------------------------------------------------------
# Strict payload validation
# ---------------------------------------------------------------------------


class TestPayloadValidation:
    def test_accepts_valid_payload(self) -> None:
        payload = _make_payload()
        parsed = HotStatePayload.model_validate(payload)
        assert parsed.event_type == "memory.remembered"

    def test_rejects_extra_envelope_field(self) -> None:
        payload = _make_payload(extra_envelope_fields={"unknown": "x"})
        with pytest.raises(Exception):
            HotStatePayload.model_validate(payload)

    def test_rejects_extra_snapshot_field(self) -> None:
        payload = _make_payload(extra_snapshot_fields={"bogus": "y"})
        with pytest.raises(Exception):
            HotStatePayload.model_validate(payload)

    def test_rejects_schema_version_string_coercion(self) -> None:
        """schema_version must be int; strict mode rejects string '1'."""
        payload = _make_payload()
        payload["schema_version"] = "1"
        with pytest.raises(Exception):
            HotStatePayload.model_validate(payload)

    def test_rejects_schema_version_float(self) -> None:
        payload = _make_payload()
        payload["schema_version"] = 1.0
        with pytest.raises(Exception):
            HotStatePayload.model_validate(payload)

    def test_rejects_event_type_int(self) -> None:
        payload = _make_payload()
        payload["event_type"] = 42
        with pytest.raises(Exception):
            HotStatePayload.model_validate(payload)


# ---------------------------------------------------------------------------
# UUID validation
# ---------------------------------------------------------------------------


class TestUuidValidation:
    def test_valid_uuid(self) -> None:
        u = uuid4()
        assert _validate_uuid_field(str(u), "test") == u

    def test_none_returns_none(self) -> None:
        assert _validate_uuid_field(None, "test") is None

    def test_invalid_uuid_raises(self) -> None:
        with pytest.raises(PermanentPayloadError, match="invalid UUID"):
            _validate_uuid_field("not-a-uuid", "test")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(PermanentPayloadError, match="invalid UUID"):
            _validate_uuid_field("", "test")


# ---------------------------------------------------------------------------
# Envelope / payload / snapshot cross-checks
# ---------------------------------------------------------------------------


class TestEnvelopeCrossCheck:
    """Writer raises PermanentProjectionError for envelope mismatches."""

    @pytest.fixture
    def writer(self):
        from unittest.mock import AsyncMock, MagicMock
        from redis.asyncio import Redis
        from app.memory_core.repositories.redis_hot.writer import (
            RedisHotStateWriter,
        )

        client = MagicMock(spec=Redis)
        client.register_script = MagicMock(return_value=AsyncMock())
        return RedisHotStateWriter(client, hot_ttl_seconds=3600)

    @pytest.mark.asyncio
    async def test_event_type_mismatch_envelope_vs_payload(self, writer) -> None:
        payload = _make_payload()
        with pytest.raises(PermanentProjectionError, match="event_type"):
            await writer.project(
                payload,
                envelope_idempotency_key="memory:x:remembered:v1:redis",
                envelope_aggregate_id=str(uuid4()),
                envelope_event_type="memory.forgotten",
                envelope_schema_version=1,
                envelope_target="redis",
            )

    @pytest.mark.asyncio
    async def test_schema_version_mismatch(self, writer) -> None:
        payload = _make_payload()
        with pytest.raises(PermanentProjectionError, match="schema_version"):
            await writer.project(
                payload,
                envelope_idempotency_key="memory:x:remembered:v1:redis",
                envelope_aggregate_id=str(uuid4()),
                envelope_event_type="memory.remembered",
                envelope_schema_version=99,
                envelope_target="redis",
            )

    @pytest.mark.asyncio
    async def test_aggregate_id_mismatch(self, writer) -> None:
        mid = uuid4()
        payload = _make_payload(memory_id=mid)
        from app.memory_core.repositories.postgres_outbox import (
            build_idempotency_key,
        )

        idem = build_idempotency_key(mid, "redis")
        other = uuid4()
        with pytest.raises(PermanentProjectionError, match="aggregate_id"):
            await writer.project(
                payload,
                envelope_idempotency_key=idem,
                envelope_aggregate_id=str(other),
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="redis",
            )

    @pytest.mark.asyncio
    async def test_snapshot_id_mismatch_aggregate_id(self, writer) -> None:
        mid = uuid4()
        other = uuid4()
        payload = _make_payload(memory_id=mid)
        payload["memory_snapshot"] = dict(payload["memory_snapshot"])
        payload["memory_snapshot"]["id"] = str(other)
        from app.memory_core.repositories.postgres_outbox import (
            build_idempotency_key,
        )

        idem = build_idempotency_key(mid, "redis")
        with pytest.raises(PermanentProjectionError, match="snapshot.id"):
            await writer.project(
                payload,
                envelope_idempotency_key=idem,
                envelope_aggregate_id=str(mid),
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="redis",
            )

    @pytest.mark.asyncio
    async def test_invalid_aggregate_id_uuid(self, writer) -> None:
        payload = _make_payload()
        payload["aggregate_id"] = "not-a-uuid"
        payload["memory_snapshot"] = dict(payload["memory_snapshot"])
        payload["memory_snapshot"]["id"] = "not-a-uuid"
        with pytest.raises(PermanentProjectionError, match="UUID"):
            await writer.project(
                payload,
                envelope_idempotency_key="memory:x:remembered:v1:redis",
                envelope_aggregate_id="not-a-uuid",
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="redis",
            )

    @pytest.mark.asyncio
    async def test_invalid_session_id_uuid(self, writer) -> None:
        mid = uuid4()
        payload = _make_payload(memory_id=mid)
        payload["memory_snapshot"] = dict(payload["memory_snapshot"])
        payload["memory_snapshot"]["session_id"] = "not-a-uuid"
        from app.memory_core.repositories.postgres_outbox import (
            build_idempotency_key,
        )

        idem = build_idempotency_key(mid, "redis")
        with pytest.raises(PermanentProjectionError, match="UUID"):
            await writer.project(
                payload,
                envelope_idempotency_key=idem,
                envelope_aggregate_id=str(mid),
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="redis",
            )

    @pytest.mark.asyncio
    async def test_unsupported_event_type(self, writer) -> None:
        mid = uuid4()
        payload = _make_payload(memory_id=mid)
        payload["event_type"] = "memory.forgotten"
        from app.memory_core.repositories.postgres_outbox import (
            build_idempotency_key,
        )

        idem = build_idempotency_key(mid, "redis")
        with pytest.raises(PermanentProjectionError, match="unsupported event_type"):
            await writer.project(
                payload,
                envelope_idempotency_key=idem,
                envelope_aggregate_id=str(mid),
                envelope_event_type="memory.forgotten",
                envelope_schema_version=1,
                envelope_target="redis",
            )

    @pytest.mark.asyncio
    async def test_malformed_payload(self, writer) -> None:
        with pytest.raises(PermanentProjectionError, match="malformed"):
            await writer.project(
                {"bogus": True},
                envelope_idempotency_key="x",
                envelope_aggregate_id=str(uuid4()),
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="redis",
            )

    @pytest.mark.asyncio
    async def test_projection_target_mismatch(self, writer) -> None:
        mid = uuid4()
        payload = _make_payload(memory_id=mid)
        from app.memory_core.repositories.postgres_outbox import (
            build_idempotency_key,
        )

        idem = build_idempotency_key(mid, "redis")
        with pytest.raises(PermanentProjectionError, match="target"):
            await writer.project(
                payload,
                envelope_idempotency_key=idem,
                envelope_aggregate_id=str(mid),
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="elasticsearch",
            )


# ---------------------------------------------------------------------------
# Canonical JSON
# ---------------------------------------------------------------------------


class TestCanonicalJson:
    def test_key_ordering(self) -> None:
        data = {"z": 1, "a": 2, "m": 3}
        assert canonical_json(data) == '{"a":2,"m":3,"z":1}'

    def test_no_whitespace(self) -> None:
        data = {"a": [1, 2, 3]}
        assert " " not in canonical_json(data)

    def test_stable_across_calls(self) -> None:
        data = {"id": "abc", "content": "hello", "metadata": {}}
        assert canonical_json(data) == canonical_json(data)


# ---------------------------------------------------------------------------
# Effective TTL
# ---------------------------------------------------------------------------


class TestEffectiveTtl:
    def test_only_hot_ttl(self) -> None:
        occurred = 1_000_000
        result = effective_expiry_ms(
            snapshot_expires_at_ms=None,
            occurred_at_ms=occurred,
            hot_ttl_seconds=60,
        )
        assert result == occurred + 60_000

    def test_snapshot_expires_before_hot_ttl(self) -> None:
        occurred = 1_000_000
        snap_exp = occurred + 30_000
        result = effective_expiry_ms(
            snapshot_expires_at_ms=snap_exp,
            occurred_at_ms=occurred,
            hot_ttl_seconds=60,
        )
        assert result == snap_exp

    def test_snapshot_expires_after_hot_ttl(self) -> None:
        occurred = 1_000_000
        snap_exp = occurred + 90_000
        result = effective_expiry_ms(
            snapshot_expires_at_ms=snap_exp,
            occurred_at_ms=occurred,
            hot_ttl_seconds=60,
        )
        assert result == occurred + 60_000

    def test_rejects_zero_ttl(self) -> None:
        with pytest.raises(PermanentPayloadError):
            effective_expiry_ms(
                snapshot_expires_at_ms=None,
                occurred_at_ms=1,
                hot_ttl_seconds=0,
            )


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------


class TestTimestampParsing:
    def test_rejects_naive_datetime(self) -> None:
        from app.memory_core.repositories.redis_hot.keys import _parse_utc_ms

        with pytest.raises(PermanentPayloadError, match="naive"):
            _parse_utc_ms("2024-01-01T00:00:00", "test")

    def test_rejects_malformed(self) -> None:
        from app.memory_core.repositories.redis_hot.keys import _parse_utc_ms

        with pytest.raises(PermanentPayloadError, match="malformed"):
            _parse_utc_ms("not-a-date", "test")

    def test_accepts_utc(self) -> None:
        from app.memory_core.repositories.redis_hot.keys import _parse_utc_ms

        result = _parse_utc_ms("2024-01-01T00:00:00+00:00", "test")
        assert isinstance(result, int)
        assert result > 0

    def test_none_returns_none(self) -> None:
        from app.memory_core.repositories.redis_hot.keys import _parse_utc_ms

        assert _parse_utc_ms(None, "test") is None


# ---------------------------------------------------------------------------
# Lua return-code classification
# ---------------------------------------------------------------------------


class TestLuaReturnCodeClassification:
    @pytest.fixture
    def writer(self):
        from unittest.mock import AsyncMock, MagicMock
        from redis.asyncio import Redis
        from app.memory_core.repositories.redis_hot.writer import (
            RedisHotStateWriter,
        )

        client = MagicMock(spec=Redis)
        mock_script = AsyncMock()
        client.register_script = MagicMock(return_value=mock_script)
        return RedisHotStateWriter(client, hot_ttl_seconds=3600), mock_script

    @pytest.mark.asyncio
    async def test_rc_ok_returns_written(self, writer) -> None:
        w, mock_script = writer
        mock_script.return_value = 0
        mid = uuid4()
        payload = _make_payload(memory_id=mid)
        from app.memory_core.repositories.postgres_outbox import (
            build_idempotency_key,
        )

        idem = build_idempotency_key(mid, "redis")
        result = await w.project(
            payload,
            envelope_idempotency_key=idem,
            envelope_aggregate_id=str(mid),
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="redis",
        )
        assert result == "written"

    @pytest.mark.asyncio
    async def test_rc_idempotent(self, writer) -> None:
        w, mock_script = writer
        mock_script.return_value = 1
        mid = uuid4()
        payload = _make_payload(memory_id=mid)
        from app.memory_core.repositories.postgres_outbox import (
            build_idempotency_key,
        )

        idem = build_idempotency_key(mid, "redis")
        result = await w.project(
            payload,
            envelope_idempotency_key=idem,
            envelope_aggregate_id=str(mid),
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="redis",
        )
        assert result == "idempotent"

    @pytest.mark.asyncio
    async def test_rc_conflict_raises_permanent(self, writer) -> None:
        w, mock_script = writer
        mock_script.return_value = 2
        mid = uuid4()
        payload = _make_payload(memory_id=mid)
        from app.memory_core.repositories.postgres_outbox import (
            build_idempotency_key,
        )

        idem = build_idempotency_key(mid, "redis")
        with pytest.raises(PermanentProjectionError, match="conflict"):
            await w.project(
                payload,
                envelope_idempotency_key=idem,
                envelope_aggregate_id=str(mid),
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="redis",
            )

    @pytest.mark.asyncio
    async def test_rc_unknown_raises_permanent(self, writer) -> None:
        w, mock_script = writer
        mock_script.return_value = 99
        mid = uuid4()
        payload = _make_payload(memory_id=mid)
        from app.memory_core.repositories.postgres_outbox import (
            build_idempotency_key,
        )

        idem = build_idempotency_key(mid, "redis")
        with pytest.raises(PermanentProjectionError, match="unexpected"):
            await w.project(
                payload,
                envelope_idempotency_key=idem,
                envelope_aggregate_id=str(mid),
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="redis",
            )


# ---------------------------------------------------------------------------
# Typed freshness results
# ---------------------------------------------------------------------------


class TestTypedFreshness:
    def test_hot_item_read_is_stale(self) -> None:
        r = HotItemRead(snapshot={"id": "x"}, pttl_ms=None)
        assert r.is_stale is True

    def test_hot_item_read_fresh(self) -> None:
        r = HotItemRead(snapshot={"id": "x"}, pttl_ms=5000)
        assert r.is_stale is False

    def test_hot_session_read(self) -> None:
        items = [HotItemRead(snapshot={"id": "a"}, pttl_ms=1000)]
        sr = HotSessionRead(items=items, session_pttl_ms=2000)
        assert len(sr.items) == 1
        assert sr.session_pttl_ms == 2000


# ---------------------------------------------------------------------------
# Bounded read constants
# ---------------------------------------------------------------------------


class TestBoundedReadConstants:
    def test_read_page_window_positive(self) -> None:
        assert READ_PAGE_WINDOW > 0
        assert READ_PAGE_WINDOW <= 200

    def test_list_session_rejects_zero_limit(self) -> None:
        from app.memory_core.repositories.redis_hot.reader import (
            _validate_positive_limit,
        )

        with pytest.raises(ValueError, match="positive"):
            _validate_positive_limit(0)

    def test_list_session_rejects_negative_limit(self) -> None:
        from app.memory_core.repositories.redis_hot.reader import (
            _validate_positive_limit,
        )

        with pytest.raises(ValueError, match="positive"):
            _validate_positive_limit(-1)


# ---------------------------------------------------------------------------
# Rebuild counters
# ---------------------------------------------------------------------------


class TestRebuildCounters:
    def test_defaults_zero(self) -> None:
        c = RebuildCounters()
        assert c.scanned == 0
        assert c.projected == 0
        assert c.skipped_expired == 0
        assert c.failed == 0

    def test_record_error_limits_list(self) -> None:
        c = RebuildCounters()
        for i in range(30):
            c.record_error(uuid4(), f"err{i}")
        assert c.failed == 30
        assert len(c.errors) == 20


# ---------------------------------------------------------------------------
# Client ownership
# ---------------------------------------------------------------------------


class TestClientOwnership:
    def test_writer_default_does_not_own_client(self) -> None:
        from unittest.mock import MagicMock
        from redis.asyncio import Redis
        from app.memory_core.repositories.redis_hot.writer import (
            RedisHotStateWriter,
        )

        client = MagicMock(spec=Redis)
        client.register_script = MagicMock()
        w = RedisHotStateWriter(client)
        assert w.owns_client is False

    def test_writer_explicit_owns_client(self) -> None:
        from unittest.mock import MagicMock
        from redis.asyncio import Redis
        from app.memory_core.repositories.redis_hot.writer import (
            RedisHotStateWriter,
        )

        client = MagicMock(spec=Redis)
        client.register_script = MagicMock()
        w = RedisHotStateWriter(client, owns_client=True)
        assert w.owns_client is True


# ---------------------------------------------------------------------------
# Credential / payload log-leak regression
# ---------------------------------------------------------------------------


class TestLogLeakRegression:
    def test_no_secrets_leaked_on_validation_failure(self, caplog) -> None:
        """Verify no payload content, Redis URL, or raw exception leaks."""
        from unittest.mock import AsyncMock, MagicMock
        from redis.asyncio import Redis
        from app.memory_core.repositories.redis_hot.writer import (
            RedisHotStateWriter,
        )

        client = MagicMock(spec=Redis)
        client.register_script = MagicMock(return_value=AsyncMock())
        writer = RedisHotStateWriter(client, hot_ttl_seconds=3600)

        with caplog.at_level(logging.DEBUG):
            try:
                import asyncio

                loop = asyncio.new_event_loop()
                loop.run_until_complete(
                    writer.project(
                        {"totally": "wrong"},
                        envelope_idempotency_key="redis://secret@host/0",
                        envelope_aggregate_id=str(uuid4()),
                        envelope_event_type="memory.remembered",
                        envelope_schema_version=1,
                        envelope_target="redis",
                    )
                )
            except PermanentProjectionError:
                pass
            finally:
                loop.close()

        for rec in caplog.records:
            msg = rec.getMessage()
            assert "redis://" not in msg
            assert "secret" not in msg
            assert "wrong" not in msg
            assert "Traceback" not in msg

    def test_no_secrets_leaked_on_expired_skip(self, caplog) -> None:
        """Expired skip must not log payload content."""
        from unittest.mock import AsyncMock, MagicMock
        from redis.asyncio import Redis
        from app.memory_core.repositories.redis_hot.writer import (
            RedisHotStateWriter,
        )

        client = MagicMock(spec=Redis)
        client.register_script = MagicMock(return_value=AsyncMock())
        writer = RedisHotStateWriter(client, hot_ttl_seconds=3600)

        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        mid = uuid4()
        payload = _make_payload(memory_id=mid, occurred_at=past)

        from app.memory_core.repositories.postgres_outbox import (
            build_idempotency_key,
        )

        idem = build_idempotency_key(mid, "redis")

        with caplog.at_level(logging.DEBUG):
            import asyncio

            loop = asyncio.new_event_loop()
            loop.run_until_complete(
                writer.project(
                    payload,
                    envelope_idempotency_key=idem,
                    envelope_aggregate_id=str(mid),
                    envelope_event_type="memory.remembered",
                    envelope_schema_version=1,
                    envelope_target="redis",
                )
            )
            loop.close()

        for rec in caplog.records:
            msg = rec.getMessage()
            assert "hello world" not in msg
            assert "redis://" not in msg


# ---------------------------------------------------------------------------
# TTL / expiry rejection — booleans, non-integers, non-positive, and
# signed-64-bit range enforcement (P1-05 review fix 1)
# ---------------------------------------------------------------------------


class TestTtlRejection:
    """Reject boolean, float, non-integer, and non-positive TTL values
    before any Lua write. Enforce Redis signed-64-bit ms range on the
    computed absolute expiry.
    """

    def test_effective_expiry_rejects_bool_true(self) -> None:
        with pytest.raises(PermanentPayloadError, match="integer"):
            effective_expiry_ms(
                snapshot_expires_at_ms=None,
                occurred_at_ms=1_000_000,
                hot_ttl_seconds=True,
            )

    def test_effective_expiry_rejects_bool_false(self) -> None:
        with pytest.raises(PermanentPayloadError, match="integer"):
            effective_expiry_ms(
                snapshot_expires_at_ms=None,
                occurred_at_ms=1_000_000,
                hot_ttl_seconds=False,
            )

    def test_effective_expiry_rejects_float_ttl(self) -> None:
        with pytest.raises(PermanentPayloadError, match="integer"):
            effective_expiry_ms(
                snapshot_expires_at_ms=None,
                occurred_at_ms=1_000_000,
                hot_ttl_seconds=3.14,
            )

    def test_effective_expiry_rejects_string_ttl(self) -> None:
        with pytest.raises(PermanentPayloadError, match="integer"):
            effective_expiry_ms(
                snapshot_expires_at_ms=None,
                occurred_at_ms=1_000_000,
                hot_ttl_seconds="3600",
            )

    def test_effective_expiry_rejects_negative_ttl(self) -> None:
        with pytest.raises(PermanentPayloadError, match="positive"):
            effective_expiry_ms(
                snapshot_expires_at_ms=None,
                occurred_at_ms=1_000_000,
                hot_ttl_seconds=-10,
            )

    def test_effective_expiry_rejects_bool_occurred_at_ms(self) -> None:
        with pytest.raises(PermanentPayloadError, match="integer"):
            effective_expiry_ms(
                snapshot_expires_at_ms=None,
                occurred_at_ms=True,
                hot_ttl_seconds=60,
            )

    def test_effective_expiry_rejects_overflow_signed64(self) -> None:
        """Computed expiry exceeding the Lua-safe exact-integer ms bound
        must raise. The Lua 5.1 interpreter uses IEEE-754 doubles; integers
        above 2**53-1 lose precision under tonumber(). The production Lua
        script uses ``tonumber(ARGV[5])`` before PEXPIREAT, so the Python
        side must reject any value that would be misinterpreted by Lua.

        effective_expiry_ms() raises PermanentPayloadError (not the
        worker-facing PermanentProjectionError); the writer converts.
        """
        from app.memory_core.repositories.redis_hot.keys import (
            LUA_SAFE_MAX_EXPIRY_MS,
        )

        # occurred_at_ms + ttl * 1000 > LUA_SAFE_MAX_EXPIRY_MS
        huge_occurred = LUA_SAFE_MAX_EXPIRY_MS  # already at the limit
        with pytest.raises(PermanentPayloadError, match="Lua-safe"):
            effective_expiry_ms(
                snapshot_expires_at_ms=None,
                occurred_at_ms=huge_occurred,
                hot_ttl_seconds=1,
            )

    def test_effective_expiry_rejects_overflow_via_snapshot(self) -> None:
        """When BOTH candidates exceed LUA_SAFE_MAX_EXPIRY_MS, overflow is
        rejected. A huge snapshot alone is safe when min() picks an
        in-range ttl_boundary — so occurred_at + ttl must also overflow.
        """
        from app.memory_core.repositories.redis_hot.keys import (
            LUA_SAFE_MAX_EXPIRY_MS,
        )

        huge_snap = LUA_SAFE_MAX_EXPIRY_MS + 10_000
        # Pick occurred_at close enough to the limit that even a 1-second
        # TTL pushes ttl_boundary past LUA_SAFE_MAX_EXPIRY_MS.
        huge_occurred = LUA_SAFE_MAX_EXPIRY_MS - 100  # +1s >> remaining room
        with pytest.raises(PermanentPayloadError, match="Lua-safe"):
            effective_expiry_ms(
                snapshot_expires_at_ms=huge_snap,
                occurred_at_ms=huge_occurred,
                hot_ttl_seconds=1,
            )

    def test_effective_expiry_accepts_large_valid_value(self) -> None:
        """A large but in-range expiry must succeed."""
        from app.memory_core.repositories.redis_hot.keys import (
            LUA_SAFE_MAX_EXPIRY_MS,
        )

        result = effective_expiry_ms(
            snapshot_expires_at_ms=None,
            occurred_at_ms=LUA_SAFE_MAX_EXPIRY_MS - 100_000,
            hot_ttl_seconds=60,
        )
        assert result == LUA_SAFE_MAX_EXPIRY_MS - 100_000 + 60_000

    def test_effective_expiry_accepts_exact_lua_safe_boundary(self) -> None:
        """The exact Lua-safe boundary (2**53-1) is accepted when
        otherwise valid — this is the maximum exactly representable
        millisecond integer in Lua 5.1."""
        from app.memory_core.repositories.redis_hot.keys import (
            LUA_SAFE_MAX_EXPIRY_MS,
        )

        # Set occurred_at and hot_ttl so ttl_boundary == LUA_SAFE_MAX_EXPIRY_MS
        occurred = LUA_SAFE_MAX_EXPIRY_MS - 60_000
        result = effective_expiry_ms(
            snapshot_expires_at_ms=None,
            occurred_at_ms=occurred,
            hot_ttl_seconds=60,
        )
        assert result == LUA_SAFE_MAX_EXPIRY_MS

    def test_effective_expiry_rejects_one_over_lua_safe_boundary(self) -> None:
        """The integer one above the Lua-safe boundary (2**53) is rejected
        locally, before any Redis/Lua interaction."""
        from app.memory_core.repositories.redis_hot.keys import (
            LUA_SAFE_MAX_EXPIRY_MS,
        )

        # Set occurred_at and hot_ttl so ttl_boundary == LUA_SAFE_MAX_EXPIRY_MS + 1
        occurred = LUA_SAFE_MAX_EXPIRY_MS - 60_000 + 1
        with pytest.raises(PermanentPayloadError, match="Lua-safe"):
            effective_expiry_ms(
                snapshot_expires_at_ms=None,
                occurred_at_ms=occurred,
                hot_ttl_seconds=60,
            )

    def test_effective_expiry_rejects_lua_unsafe_snapshot(self) -> None:
        """A snapshot expires_at above the Lua-safe boundary is rejected
        when it would be the effective expiry (min picks it)."""
        from app.memory_core.repositories.redis_hot.keys import (
            LUA_SAFE_MAX_EXPIRY_MS,
        )

        huge_snap = LUA_SAFE_MAX_EXPIRY_MS + 1
        # Set occurred_at and TTL so ttl_boundary > huge_snap (min picks snap)
        occurred = 1_000_000
        with pytest.raises(PermanentPayloadError, match="Lua-safe"):
            effective_expiry_ms(
                snapshot_expires_at_ms=huge_snap,
                occurred_at_ms=occurred,
                hot_ttl_seconds=(LUA_SAFE_MAX_EXPIRY_MS // 1000) + 10_000,
            )


class TestWriterTtlRejection:
    """Writer rejects boolean/float/non-positive TTL before registering
    the Lua script or contacting Redis.
    """

    def _make_client(self):
        from unittest.mock import MagicMock
        from redis.asyncio import Redis

        client = MagicMock(spec=Redis)
        client.register_script = MagicMock()
        return client

    def test_writer_rejects_bool_true_ttl(self) -> None:
        from app.memory_core.repositories.redis_hot.writer import (
            RedisHotStateWriter,
        )

        client = self._make_client()
        with pytest.raises(PermanentProjectionError, match="integer"):
            RedisHotStateWriter(client, hot_ttl_seconds=True)
        # Script must NOT have been registered (no Redis interaction).
        client.register_script.assert_not_called()

    def test_writer_rejects_bool_false_ttl(self) -> None:
        from app.memory_core.repositories.redis_hot.writer import (
            RedisHotStateWriter,
        )

        client = self._make_client()
        with pytest.raises(PermanentProjectionError, match="integer"):
            RedisHotStateWriter(client, hot_ttl_seconds=False)
        client.register_script.assert_not_called()

    def test_writer_rejects_float_ttl(self) -> None:
        from app.memory_core.repositories.redis_hot.writer import (
            RedisHotStateWriter,
        )

        client = self._make_client()
        with pytest.raises(PermanentProjectionError, match="integer"):
            RedisHotStateWriter(client, hot_ttl_seconds=3.14)
        client.register_script.assert_not_called()

    def test_writer_rejects_negative_ttl(self) -> None:
        from app.memory_core.repositories.redis_hot.writer import (
            RedisHotStateWriter,
        )

        client = self._make_client()
        with pytest.raises(PermanentProjectionError, match="positive"):
            RedisHotStateWriter(client, hot_ttl_seconds=-10)
        client.register_script.assert_not_called()

    def test_writer_rejects_zero_ttl(self) -> None:
        from app.memory_core.repositories.redis_hot.writer import (
            RedisHotStateWriter,
        )

        client = self._make_client()
        with pytest.raises(PermanentProjectionError, match="positive"):
            RedisHotStateWriter(client, hot_ttl_seconds=0)
        client.register_script.assert_not_called()


class TestWriterExpiryRejectionBeforeLua:
    """Writer project() rejects overflow expiry before invoking Lua."""

    @pytest.fixture
    def writer_and_script(self):
        from unittest.mock import AsyncMock, MagicMock
        from redis.asyncio import Redis
        from app.memory_core.repositories.redis_hot.writer import (
            RedisHotStateWriter,
        )

        client = MagicMock(spec=Redis)
        mock_script = AsyncMock()
        client.register_script = MagicMock(return_value=mock_script)
        w = RedisHotStateWriter(client, hot_ttl_seconds=3600)
        return w, mock_script

    @pytest.mark.asyncio
    async def test_project_rejects_overflow_expiry_before_lua(self, writer_and_script) -> None:
        w, mock_script = writer_and_script
        # Replace the writer's TTL with a deliberately oversized value so
        # occurred_at_ms + hot_ttl_seconds * 1000 overflows the Lua-safe
        # exact-integer bound (2**53-1).
        w._hot_ttl_seconds = 10**16  # 10^16 s * 1000 = 10^19 ms >> limit
        mid = uuid4()
        payload = _make_payload(memory_id=mid)
        from app.memory_core.repositories.postgres_outbox import (
            build_idempotency_key,
        )

        idem = build_idempotency_key(mid, "redis")
        # effective_expiry_ms() raises PermanentPayloadError locally; the
        # writer's _validate_payload converts it to the worker-facing
        # PermanentProjectionError so the delivery is classified as
        # non-retryable (permanent failure).
        with pytest.raises(PermanentProjectionError, match="Lua-safe"):
            await w.project(
                payload,
                envelope_idempotency_key=idem,
                envelope_aggregate_id=str(mid),
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="redis",
            )
        # Lua script must NOT have been called (error happens before Lua).
        mock_script.assert_not_called()

    @pytest.mark.asyncio
    async def test_project_converts_lua_safe_boundary_to_permanent_projection_error(
        self, writer_and_script
    ) -> None:
        """Writer converts the local Lua-safe boundary error to
        PermanentProjectionError BEFORE Lua execution — proves the
        error is worker-visible as non-retryable and no Redis
        interaction occurs."""
        from app.memory_core.repositories.redis_hot.keys import (
            LUA_SAFE_MAX_EXPIRY_MS,
        )

        w, mock_script = writer_and_script
        # Set TTL so ttl_boundary == LUA_SAFE_MAX_EXPIRY_MS + 1 (one over)
        # occurred_at ~ 1.7e12 (year ~2024 in ms), we want:
        #   occurred_at + ttl*1000 = LUA_SAFE_MAX_EXPIRY_MS + 1
        # Pick occurred_at = 1_000_000, so ttl*1000 = LUA_SAFE_MAX_EXPIRY_MS + 1 - 1_000_000
        target_boundary_excess = LUA_SAFE_MAX_EXPIRY_MS + 1 - 1_000_000
        w._hot_ttl_seconds = (target_boundary_excess // 1000) + 1
        mid = uuid4()
        payload = _make_payload(
            memory_id=mid, occurred_at=datetime(1970, 1, 1, tzinfo=timezone.utc)
        )
        # occurred_at_ms for 1970-01-01 = 0; so ttl_boundary = 0 + ttl*1000
        # Reset to a clean occurred_at: use 1_000_000 ms via a specific datetime
        payload["occurred_at"] = "1970-01-01T00:16:40+00:00"  # 1_000_000 ms
        from app.memory_core.repositories.postgres_outbox import (
            build_idempotency_key,
        )

        idem = build_idempotency_key(mid, "redis")
        with pytest.raises(PermanentProjectionError):
            await w.project(
                payload,
                envelope_idempotency_key=idem,
                envelope_aggregate_id=str(mid),
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="redis",
            )
        # Lua script must NOT have been called.
        mock_script.assert_not_called()


# ---------------------------------------------------------------------------
# Rebuild — Redis ResponseError classified as infrastructure failure
# (P1-05 review fix 1)
# ---------------------------------------------------------------------------


class TestRebuildInfraErrorClassification:
    """Redis ResponseError variants cause RebuildInfrastructureError,
    not per-item counter increments.

    Uses real redis-py exception subclasses to prove that the
    isinstance(exc, ResponseError) check in rebuild._is_infra_error
    correctly classifies every server-side error variant — including
    those not enumerated by name in earlier implementations.
    """

    def test_is_infra_error_classifies_response_error(self) -> None:
        from redis.exceptions import ResponseError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        exc = ResponseError("generic server error")
        assert _is_infra_error(exc) is True

    def test_is_infra_error_classifies_noauth_error(self) -> None:
        from redis.exceptions import AuthenticationError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        assert _is_infra_error(AuthenticationError("NOAUTH")) is True

    def test_is_infra_error_classifies_oom_error(self) -> None:
        from redis.exceptions import OutOfMemoryError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        assert _is_infra_error(OutOfMemoryError("OOM")) is True

    def test_is_infra_error_classifies_readonly_error(self) -> None:
        from redis.exceptions import ReadOnlyError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        assert _is_infra_error(ReadOnlyError("READONLY")) is True

    def test_is_infra_error_classifies_misconf_error(self) -> None:
        from redis.exceptions import NoPermissionError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        assert _is_infra_error(NoPermissionError("NOPERM")) is True

    def test_is_infra_error_classifies_noscript_error(self) -> None:
        """NoScriptError (NOSCRIPT) is a ResponseError subclass and must
        be classified as infrastructure — the script may need reloading,
        it is not a permanent payload error."""
        from redis.exceptions import NoScriptError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        exc = NoScriptError("NOSCRIPT No matching script")
        assert _is_infra_error(exc) is True

    def test_is_infra_error_classifies_execabort_error(self) -> None:
        """ExecAbortError (EXECABORT) is a ResponseError subclass and
        must be classified as infrastructure — transaction aborted by
        server, not a payload problem."""
        from redis.exceptions import ExecAbortError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        exc = ExecAbortError("EXECABORT Transaction discarded")
        assert _is_infra_error(exc) is True

    def test_is_infra_error_classifies_clusterdown_error(self) -> None:
        """ClusterDownError (CLUSTERDOWN) is a ResponseError subclass and
        must be classified as infrastructure — cluster unavailable,
        transient."""
        from redis.exceptions import ClusterDownError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        exc = ClusterDownError("CLUSTERDOWN The cluster is down")
        assert _is_infra_error(exc) is True

    def test_is_infra_error_classifies_tryagain_error(self) -> None:
        """TryAgainError (TRYAGAIN) is a ResponseError subclass and must
        be classified as infrastructure — transient, client should retry."""
        from redis.exceptions import TryAgainError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        exc = TryAgainError("TRYAGAIN Multiple keys request during rehashing")
        assert _is_infra_error(exc) is True

    def test_is_infra_error_classifies_busyloading_error(self) -> None:
        from redis.exceptions import BusyLoadingError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        assert _is_infra_error(BusyLoadingError("LOADING")) is True

    def test_is_infra_error_classifies_masterdown_error(self) -> None:
        from redis.exceptions import MasterDownError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        assert _is_infra_error(MasterDownError("MASTERDOWN")) is True

    def test_is_infra_error_does_not_classify_permanent_projection_error(
        self,
    ) -> None:
        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        exc = PermanentProjectionError("permanent failure")
        assert _is_infra_error(exc) is False

    def test_is_infra_error_does_not_classify_value_error(self) -> None:
        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        exc = ValueError("some value error")
        assert _is_infra_error(exc) is False

    def test_is_infra_error_classifies_connection_error(self) -> None:
        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        # Python builtins
        assert _is_infra_error(ConnectionError("refused")) is True
        assert _is_infra_error(TimeoutError("timed out")) is True
        assert _is_infra_error(OSError("network")) is True

    def test_is_infra_error_classifies_redis_connection_error(self) -> None:
        """redis.exceptions.ConnectionError shadows the builtin. It
        inherits from RedisError (not from the builtin ConnectionError).
        It must still be classified as infrastructure.
        """
        from redis.exceptions import ConnectionError as RedisConnectionError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        assert _is_infra_error(RedisConnectionError("connection refused")) is True

    def test_is_infra_error_classifies_redis_timeout_error(self) -> None:
        """redis.exceptions.TimeoutError shadows the builtin. It
        inherits from RedisError (not from the builtin TimeoutError).
        It must still be classified as infrastructure.
        """
        from redis.exceptions import TimeoutError as RedisTimeoutError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        assert _is_infra_error(RedisTimeoutError("timeout")) is True

    def test_is_infra_error_classifies_redis_authentication_error(self) -> None:
        """AuthenticationError inherits from redis's ConnectionError,
        which inherits from RedisError (not builtin ConnectionError).
        It must still be classified as infrastructure.
        """
        from redis.exceptions import AuthenticationError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        assert _is_infra_error(AuthenticationError("auth failed")) is True

    def test_is_infra_error_classifies_max_connections_error(self) -> None:
        """MaxConnectionsError inherits from redis's ConnectionError.
        Must be classified as infrastructure — pool exhaustion.
        """
        from redis.exceptions import MaxConnectionsError

        from app.memory_core.repositories.redis_hot.rebuild import (
            _is_infra_error,
        )

        assert _is_infra_error(MaxConnectionsError("pool exhausted")) is True

    @pytest.mark.asyncio
    async def test_rebuild_raises_infrastructure_error_on_response_error(
        self,
    ) -> None:
        """End-to-end: a real redis-py ResponseError during rebuild causes
        RebuildInfrastructureError, not a per-item failure increment.
        """
        from unittest.mock import AsyncMock, MagicMock
        from redis.exceptions import ResponseError

        from app.memory_core.repositories.redis_hot.rebuild import (
            RebuildInfrastructureError,
            rebuild_from_postgres,
        )

        fake_writer = MagicMock()
        fake_writer.project = AsyncMock(
            side_effect=ResponseError("OOM command not allowed"),
        )

        # Build a fake PG session that returns one row.
        fake_row = MagicMock()
        fake_row.id = uuid4()
        fake_row.status = "active"
        fake_row.content = "test"
        fake_row.created_at = datetime.now(timezone.utc)
        fake_row.updated_at = datetime.now(timezone.utc)
        fake_row.expires_at = None
        fake_row.session_id = None
        fake_row.agent_id = None
        fake_row.knowledge_base_id = None
        fake_row.project_id = None
        fake_row.provenance = None
        fake_row.metadata_ = {}

        fake_result = MagicMock()
        fake_result.scalars.return_value.all.return_value = [fake_row]

        fake_session = AsyncMock()
        fake_session.execute = AsyncMock(return_value=fake_result)

        # Patch _hydrate_evidence_batch to return empty evidence.
        import app.memory_core.repositories.redis_hot.rebuild as rebuild_mod

        original_hydrate = rebuild_mod._hydrate_evidence_batch
        rebuild_mod._hydrate_evidence_batch = AsyncMock(return_value={})
        try:
            with pytest.raises(RebuildInfrastructureError):
                await rebuild_from_postgres(
                    session=fake_session,
                    writer=fake_writer,
                )
        finally:
            rebuild_mod._hydrate_evidence_batch = original_hydrate

    @pytest.mark.asyncio
    async def test_rebuild_raises_infrastructure_error_on_noscript_error(
        self,
    ) -> None:
        """End-to-end: a real NoScriptError during rebuild causes
        RebuildInfrastructureError — the script needs reloading, this
        is not a permanent payload error.
        """
        from unittest.mock import AsyncMock, MagicMock
        from redis.exceptions import NoScriptError

        from app.memory_core.repositories.redis_hot.rebuild import (
            RebuildInfrastructureError,
            rebuild_from_postgres,
        )

        fake_writer = MagicMock()
        fake_writer.project = AsyncMock(
            side_effect=NoScriptError("NOSCRIPT No matching script"),
        )

        fake_row = MagicMock()
        fake_row.id = uuid4()
        fake_row.status = "active"
        fake_row.content = "test"
        fake_row.created_at = datetime.now(timezone.utc)
        fake_row.updated_at = datetime.now(timezone.utc)
        fake_row.expires_at = None
        fake_row.session_id = None
        fake_row.agent_id = None
        fake_row.knowledge_base_id = None
        fake_row.project_id = None
        fake_row.provenance = None
        fake_row.metadata_ = {}

        fake_result = MagicMock()
        fake_result.scalars.return_value.all.return_value = [fake_row]

        fake_session = AsyncMock()
        fake_session.execute = AsyncMock(return_value=fake_result)

        import app.memory_core.repositories.redis_hot.rebuild as rebuild_mod

        original_hydrate = rebuild_mod._hydrate_evidence_batch
        rebuild_mod._hydrate_evidence_batch = AsyncMock(return_value={})
        try:
            with pytest.raises(RebuildInfrastructureError):
                await rebuild_from_postgres(
                    session=fake_session,
                    writer=fake_writer,
                )
        finally:
            rebuild_mod._hydrate_evidence_batch = original_hydrate

    @pytest.mark.asyncio
    async def test_rebuild_raises_infrastructure_error_on_clusterdown_error(
        self,
    ) -> None:
        """End-to-end: ClusterDownError during rebuild causes
        RebuildInfrastructureError."""
        from unittest.mock import AsyncMock, MagicMock
        from redis.exceptions import ClusterDownError

        from app.memory_core.repositories.redis_hot.rebuild import (
            RebuildInfrastructureError,
            rebuild_from_postgres,
        )

        fake_writer = MagicMock()
        fake_writer.project = AsyncMock(
            side_effect=ClusterDownError("CLUSTERDOWN The cluster is down"),
        )

        fake_row = MagicMock()
        fake_row.id = uuid4()
        fake_row.status = "active"
        fake_row.content = "test"
        fake_row.created_at = datetime.now(timezone.utc)
        fake_row.updated_at = datetime.now(timezone.utc)
        fake_row.expires_at = None
        fake_row.session_id = None
        fake_row.agent_id = None
        fake_row.knowledge_base_id = None
        fake_row.project_id = None
        fake_row.provenance = None
        fake_row.metadata_ = {}

        fake_result = MagicMock()
        fake_result.scalars.return_value.all.return_value = [fake_row]

        fake_session = AsyncMock()
        fake_session.execute = AsyncMock(return_value=fake_result)

        import app.memory_core.repositories.redis_hot.rebuild as rebuild_mod

        original_hydrate = rebuild_mod._hydrate_evidence_batch
        rebuild_mod._hydrate_evidence_batch = AsyncMock(return_value={})
        try:
            with pytest.raises(RebuildInfrastructureError):
                await rebuild_from_postgres(
                    session=fake_session,
                    writer=fake_writer,
                )
        finally:
            rebuild_mod._hydrate_evidence_batch = original_hydrate

    @pytest.mark.asyncio
    async def test_rebuild_raises_infrastructure_error_on_tryagain_error(
        self,
    ) -> None:
        """End-to-end: TryAgainError during rebuild causes
        RebuildInfrastructureError — transient, not a payload error."""
        from unittest.mock import AsyncMock, MagicMock
        from redis.exceptions import TryAgainError

        from app.memory_core.repositories.redis_hot.rebuild import (
            RebuildInfrastructureError,
            rebuild_from_postgres,
        )

        fake_writer = MagicMock()
        fake_writer.project = AsyncMock(
            side_effect=TryAgainError("TRYAGAIN Multiple keys during rehash"),
        )

        fake_row = MagicMock()
        fake_row.id = uuid4()
        fake_row.status = "active"
        fake_row.content = "test"
        fake_row.created_at = datetime.now(timezone.utc)
        fake_row.updated_at = datetime.now(timezone.utc)
        fake_row.expires_at = None
        fake_row.session_id = None
        fake_row.agent_id = None
        fake_row.knowledge_base_id = None
        fake_row.project_id = None
        fake_row.provenance = None
        fake_row.metadata_ = {}

        fake_result = MagicMock()
        fake_result.scalars.return_value.all.return_value = [fake_row]

        fake_session = AsyncMock()
        fake_session.execute = AsyncMock(return_value=fake_result)

        import app.memory_core.repositories.redis_hot.rebuild as rebuild_mod

        original_hydrate = rebuild_mod._hydrate_evidence_batch
        rebuild_mod._hydrate_evidence_batch = AsyncMock(return_value={})
        try:
            with pytest.raises(RebuildInfrastructureError):
                await rebuild_from_postgres(
                    session=fake_session,
                    writer=fake_writer,
                )
        finally:
            rebuild_mod._hydrate_evidence_batch = original_hydrate

    @pytest.mark.asyncio
    async def test_rebuild_raises_infrastructure_error_on_execabort_error(
        self,
    ) -> None:
        """End-to-end: ExecAbortError during rebuild causes
        RebuildInfrastructureError."""
        from unittest.mock import AsyncMock, MagicMock
        from redis.exceptions import ExecAbortError

        from app.memory_core.repositories.redis_hot.rebuild import (
            RebuildInfrastructureError,
            rebuild_from_postgres,
        )

        fake_writer = MagicMock()
        fake_writer.project = AsyncMock(
            side_effect=ExecAbortError("EXECABORT Transaction discarded"),
        )

        fake_row = MagicMock()
        fake_row.id = uuid4()
        fake_row.status = "active"
        fake_row.content = "test"
        fake_row.created_at = datetime.now(timezone.utc)
        fake_row.updated_at = datetime.now(timezone.utc)
        fake_row.expires_at = None
        fake_row.session_id = None
        fake_row.agent_id = None
        fake_row.knowledge_base_id = None
        fake_row.project_id = None
        fake_row.provenance = None
        fake_row.metadata_ = {}

        fake_result = MagicMock()
        fake_result.scalars.return_value.all.return_value = [fake_row]

        fake_session = AsyncMock()
        fake_session.execute = AsyncMock(return_value=fake_result)

        import app.memory_core.repositories.redis_hot.rebuild as rebuild_mod

        original_hydrate = rebuild_mod._hydrate_evidence_batch
        rebuild_mod._hydrate_evidence_batch = AsyncMock(return_value={})
        try:
            with pytest.raises(RebuildInfrastructureError):
                await rebuild_from_postgres(
                    session=fake_session,
                    writer=fake_writer,
                )
        finally:
            rebuild_mod._hydrate_evidence_batch = original_hydrate

    @pytest.mark.asyncio
    async def test_rebuild_does_not_accumulate_partial_result_on_infra_error(
        self,
    ) -> None:
        """When an infrastructure error occurs mid-batch, rebuild raises
        immediately — no successful partial result is accumulated, no
        subsequent items are processed."""
        from unittest.mock import AsyncMock, MagicMock
        from redis.exceptions import ClusterDownError

        from app.memory_core.repositories.redis_hot.rebuild import (
            RebuildInfrastructureError,
            rebuild_from_postgres,
        )

        # Writer succeeds for the first item, then raises ClusterDownError.
        call_count = 0

        async def project_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "written"
            raise ClusterDownError("CLUSTERDOWN The cluster is down")

        fake_writer = MagicMock()
        fake_writer.project = AsyncMock(side_effect=project_side_effect)

        # Two rows: first succeeds, second triggers infra error.
        rows = []
        for _ in range(2):
            fake_row = MagicMock()
            fake_row.id = uuid4()
            fake_row.status = "active"
            fake_row.content = "test"
            fake_row.created_at = datetime.now(timezone.utc)
            fake_row.updated_at = datetime.now(timezone.utc)
            fake_row.expires_at = None
            fake_row.session_id = None
            fake_row.agent_id = None
            fake_row.knowledge_base_id = None
            fake_row.project_id = None
            fake_row.provenance = None
            fake_row.metadata_ = {}
            rows.append(fake_row)

        fake_result = MagicMock()
        fake_result.scalars.return_value.all.return_value = rows

        fake_session = AsyncMock()
        fake_session.execute = AsyncMock(return_value=fake_result)

        import app.memory_core.repositories.redis_hot.rebuild as rebuild_mod

        original_hydrate = rebuild_mod._hydrate_evidence_batch
        rebuild_mod._hydrate_evidence_batch = AsyncMock(return_value={})
        try:
            with pytest.raises(RebuildInfrastructureError):
                await rebuild_from_postgres(
                    session=fake_session,
                    writer=fake_writer,
                )
        finally:
            rebuild_mod._hydrate_evidence_batch = original_hydrate
        # The second item's project() must have been called (the error
        # happens inside project(), not before).
        assert call_count == 2


# ---------------------------------------------------------------------------
# Compose-root failure log sanitisation (P1-05 review fix 1)
# ---------------------------------------------------------------------------


class TestComposeRootLogSanitisation:
    """Compose-root failure logs must contain neither the raw exception
    message nor credentials/endpoint text. Must use a fixed safe event.
    """

    @pytest.mark.asyncio
    async def test_compose_root_failure_log_is_sanitized(self, caplog) -> None:
        from app.memory_core.redis_hot_compose import (
            compose_and_run_redis_hot_worker,
        )
        from unittest.mock import MagicMock
        from sqlalchemy.ext.asyncio import async_sessionmaker

        # A dead Redis URL with credentials embedded.
        secret_redis_url = "redis://:supersecretpassword@127.0.0.1:1/0"
        fake_factory = MagicMock(spec=async_sessionmaker)

        with caplog.at_level(logging.DEBUG):
            try:
                await compose_and_run_redis_hot_worker(
                    session_factory=fake_factory,
                    redis_url=secret_redis_url,
                    worker_identity="p1-05-log-test",
                    preflight_timeout_s=1.0,
                )
            except Exception:
                pass

        # Verify no raw exception messages or secrets in log output.
        for rec in caplog.records:
            msg = rec.getMessage()
            assert "supersecretpassword" not in msg, f"Log message leaked credential: {msg!r}"
            assert "redis://:supersecretpassword" not in msg
            assert "Traceback" not in msg, f"Log message contained raw traceback: {msg!r}"

        # Verify the safe fixed event was logged.
        safe_events = [
            rec.getMessage()
            for rec in caplog.records
            if "redis hot worker startup/run failed" in rec.getMessage()
        ]
        assert len(safe_events) >= 1, "Expected at least one safe fixed-event log line"
        # The log line must contain only the exception class name, not
        # the exception message or credentials.
        for msg in safe_events:
            assert "ConnectionError" in msg or "TimeoutError" in msg or "Error" in msg


# ---------------------------------------------------------------------------
# Reader — stale snapshot rejection on PTTL race (P1-05 review fix 1)
# ---------------------------------------------------------------------------


class TestReaderStaleSnapshotRejection:
    """When PTTL is missing, negative, or zero after an HGET race, the
    reader must return None (not a stale snapshot) and in session scans
    must enqueue the ZSET member for lazy removal.
    """

    @pytest.mark.asyncio
    async def test_get_item_typed_returns_none_when_pttl_missing(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from redis.asyncio import Redis
        from app.memory_core.repositories.redis_hot.reader import (
            RedisHotStateReader,
        )

        client = MagicMock(spec=Redis)
        # Pipeline returns a snapshot but PTTL = None (key expired between
        # HGET and PTTL).
        mock_pipe = MagicMock()
        mock_pipe.hget = MagicMock(return_value=mock_pipe)
        mock_pipe.pttl = MagicMock(return_value=mock_pipe)
        mock_pipe.execute = AsyncMock(
            return_value=[b'{"id":"abc","content":"x","status":"active"}', None],
        )
        client.pipeline = MagicMock(return_value=mock_pipe)

        reader = RedisHotStateReader(client)
        result = await reader.get_item_typed(uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_get_item_typed_returns_none_when_pttl_negative(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from redis.asyncio import Redis
        from app.memory_core.repositories.redis_hot.reader import (
            RedisHotStateReader,
        )

        client = MagicMock(spec=Redis)
        mock_pipe = MagicMock()
        mock_pipe.hget = MagicMock(return_value=mock_pipe)
        mock_pipe.pttl = MagicMock(return_value=mock_pipe)
        # PTTL = -2 (key does not exist in Redis)
        mock_pipe.execute = AsyncMock(
            return_value=[b'{"id":"abc","content":"x","status":"active"}', -2],
        )
        client.pipeline = MagicMock(return_value=mock_pipe)

        reader = RedisHotStateReader(client)
        result = await reader.get_item_typed(uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_get_item_typed_returns_none_when_pttl_zero(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from redis.asyncio import Redis
        from app.memory_core.repositories.redis_hot.reader import (
            RedisHotStateReader,
        )

        client = MagicMock(spec=Redis)
        mock_pipe = MagicMock()
        mock_pipe.hget = MagicMock(return_value=mock_pipe)
        mock_pipe.pttl = MagicMock(return_value=mock_pipe)
        # PTTL = 0 (key is about to expire — treat as absent).
        mock_pipe.execute = AsyncMock(
            return_value=[b'{"id":"abc","content":"x","status":"active"}', 0],
        )
        client.pipeline = MagicMock(return_value=mock_pipe)

        reader = RedisHotStateReader(client)
        result = await reader.get_item_typed(uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_session_scan_omits_item_with_nonpositive_pttl_and_enqueues_zrem(
        self,
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from redis.asyncio import Redis
        from app.memory_core.repositories.redis_hot.reader import (
            RedisHotStateReader,
        )

        mid = uuid4()
        sid = uuid4()
        client = MagicMock(spec=Redis)

        # ZSET scan returns the memory ID.
        client.zrange = AsyncMock(return_value=[mid.hex.encode()])

        # Pipeline for HGET + PTTL: snapshot present, PTTL = -1 (no TTL).
        mock_pipe = MagicMock()
        mock_pipe.hget = MagicMock(return_value=mock_pipe)
        mock_pipe.pttl = MagicMock(return_value=mock_pipe)
        mock_pipe.execute = AsyncMock(
            return_value=[
                b'{"id":"' + mid.hex.encode() + b'","content":"x","status":"active"}',
                -1,
            ],
        )
        client.pipeline = MagicMock(return_value=mock_pipe)
        client.zrem = AsyncMock(return_value=1)
        client.pttl = AsyncMock(return_value=5000)

        reader = RedisHotStateReader(client)
        result = await reader.list_session_items_typed(sid, limit=10)
        # Item must NOT be in the results (non-positive PTTL -> absent/stale).
        assert len(result.items) == 0
        # ZREM must have been called with the memory ID for lazy removal.
        client.zrem.assert_called_once()
        call_args = client.zrem.call_args
        # The first positional arg is the session key, the rest are members.
        # Members can be either str or bytes depending on decode_responses.
        members = call_args.args[1:]
        member_strs = [m.decode() if isinstance(m, bytes) else str(m) for m in members]
        assert mid.hex in member_strs, f"Expected {mid.hex!r} in zrem members, got {member_strs!r}"
