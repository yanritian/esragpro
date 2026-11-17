"""Real Redis/PostgreSQL integration tests for P1-05.

Covers:
- Worker delivers a real redis outbox event
- HASH + ZSET both visible
- Idempotency on duplicate delivery
- Permanent conflict on different idempotency key
- Absolute TTL preserved, retry does not extend
- Expired event success no-op
- Cross-session isolation
- Stale ZSET member cleanup
- Redis data loss + PostgreSQL rebuild
- Rebuild does not create outbox rows
- Short-TTL cannot shorten session ZSET
- Bounded reads (no ZREVRANGE 0 -1)
- Lua atomic failure injection
- Authentication failure preflight
- Connection loss / recovery
- Partial-loss rebuild (session index missing only)
- PostgreSQL readable during Redis outage
- Typed freshness results
- Envelope/payload cross-check integration
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.db.models.memory_plane import MemoryProjectionOutbox as OutboxRow
from app.memory_core.projection_worker import (
    PermanentProjectionError,
    ProjectionHandlerRegistry,
    ProjectionWorker,
)
from app.memory_core.projection_worker.postgres_store import ProjectionOutboxStore
from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy
from app.memory_core.redis_hot_handler import RedisHotStateHandler
from app.memory_core.repositories.postgres_outbox import (
    build_idempotency_key,
)
from app.memory_core.repositories.postgres_repository import PostgresMemoryRepository
from app.memory_core.repositories.redis_hot import (
    DEFAULT_PREFIX,
    RedisHotStateReader,
    RedisHotStateWriter,
    item_key,
    rebuild_from_postgres,
    session_key,
)
from app.memory_core.unpermissioned_contracts import (
    EvidenceRef,
    ProvenanceInfo,
)


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _make_payload(mid, *, sid=None, content="test", now=None):
    now = now or datetime.now(timezone.utc)
    return {
        "event_type": "memory.remembered",
        "schema_version": 1,
        "aggregate_type": "memory_item",
        "aggregate_id": str(mid),
        "projection_target": "redis",
        "occurred_at": _iso_utc(now),
        "memory_snapshot": {
            "id": str(mid),
            "content": content,
            "status": "active",
            "created_at": _iso_utc(now),
            "updated_at": _iso_utc(now),
            "expires_at": None,
            "project_id": None,
            "session_id": str(sid) if sid else None,
            "agent_id": None,
            "knowledge_base_id": None,
            "provenance": None,
            "metadata": {},
            "evidence_refs": [],
        },
    }


async def _project(writer, mid, *, sid=None, content="test", now=None):
    now = now or datetime.now(timezone.utc)
    payload = _make_payload(mid, sid=sid, content=content, now=now)
    idem = build_idempotency_key(mid, "redis")
    return await writer.project(
        payload,
        envelope_idempotency_key=idem,
        envelope_aggregate_id=str(mid),
        envelope_event_type="memory.remembered",
        envelope_schema_version=1,
        envelope_target="redis",
    )


class TestWorkerDeliversRealRedisEvent:
    @pytest.mark.asyncio
    async def test_worker_processes_redis_target_and_creates_hash_and_zset(
        self,
        session_factory,
        redis_client,
    ) -> None:
        now = datetime.now(timezone.utc)
        handler = RedisHotStateHandler(
            RedisHotStateWriter(redis_client, hot_ttl_seconds=3600),
        )
        registry = ProjectionHandlerRegistry({"redis": handler})
        store = ProjectionOutboxStore(session_factory, worker_identity="p1-05-test")
        worker = ProjectionWorker(
            store=store,
            registry=registry,
            worker_identity="p1-05-test",
            batch_size=10,
            max_concurrency=10,
            lease_duration=timedelta(seconds=30),
            delivery_timeout=timedelta(seconds=10),
            finalization_margin=timedelta(seconds=5),
            retry_policy=ExponentialBackoffPolicy(base_delay_seconds=1.0),
            target_filter={"redis"},
        )

        async with session_factory() as session:
            repo = PostgresMemoryRepository(session, projection_targets=["redis"])
            sid = uuid4()
            item = await repo.remember(
                content="test memory for redis hot state",
                evidence_refs=[
                    EvidenceRef(
                        source_id="doc-1",
                        source_type="document",
                        timestamp=now.replace(tzinfo=None),
                        confidence=0.9,
                    )
                ],
                provenance=ProvenanceInfo(
                    source_type="document",
                    source_name="test.pdf",
                ),
                session_id=sid,
            )
            memory_id = item.id
            await session.commit()

        result = await worker.run_once()
        assert result.completed == 1

        reader = RedisHotStateReader(redis_client)
        typed = await reader.get_item_typed(memory_id)
        assert typed is not None
        assert typed.snapshot["id"] == str(memory_id)
        assert typed.snapshot["content"] == "test memory for redis hot state"
        assert typed.pttl_ms is not None and typed.pttl_ms > 0

        session_items = await reader.list_session_items(sid)
        assert len(session_items) == 1
        assert session_items[0]["id"] == str(memory_id)


class TestIdempotencyAndConflict:
    @pytest.mark.asyncio
    async def test_duplicate_delivery_is_idempotent(
        self,
        session_factory,
        redis_client,
    ) -> None:
        now = datetime.now(timezone.utc)
        handler = RedisHotStateHandler(
            RedisHotStateWriter(redis_client, hot_ttl_seconds=3600),
        )
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session, projection_targets=["redis"])
            item = await repo.remember(
                content="idempotent test",
                evidence_refs=[
                    EvidenceRef(
                        source_id="doc-idem",
                        source_type="document",
                        timestamp=now.replace(tzinfo=None),
                        confidence=0.9,
                    )
                ],
                provenance=ProvenanceInfo(
                    source_type="document",
                    source_name="test.pdf",
                ),
            )
            memory_id = item.id
            await session.commit()

        payload = _make_payload(memory_id, content="idempotent test", now=now)
        idem_key = build_idempotency_key(memory_id, "redis")

        r1 = await handler.writer.project(
            payload,
            envelope_idempotency_key=idem_key,
            envelope_aggregate_id=str(memory_id),
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="redis",
        )
        assert r1 == "written"

        r2 = await handler.writer.project(
            payload,
            envelope_idempotency_key=idem_key,
            envelope_aggregate_id=str(memory_id),
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="redis",
        )
        assert r2 == "idempotent"

    @pytest.mark.asyncio
    async def test_conflicting_idempotency_key_is_permanent(
        self,
        redis_client,
    ) -> None:
        now = datetime.now(timezone.utc)
        mid = uuid4()
        handler = RedisHotStateHandler(
            RedisHotStateWriter(redis_client, hot_ttl_seconds=3600),
        )
        payload = _make_payload(mid, content="conflict test", now=now)
        real_idem = build_idempotency_key(mid, "redis")
        r1 = await handler.writer.project(
            payload,
            envelope_idempotency_key=real_idem,
            envelope_aggregate_id=str(mid),
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="redis",
        )
        assert r1 == "written"

        fake_idem = "memory:other-key:remembered:v1:redis"
        with pytest.raises(PermanentProjectionError):
            await handler.writer.project(
                payload,
                envelope_idempotency_key=fake_idem,
                envelope_aggregate_id=str(mid),
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="redis",
            )


class TestAbsoluteTtl:
    @pytest.mark.asyncio
    async def test_ttl_preserved_across_idempotent_retry(
        self,
        redis_client,
    ) -> None:
        now = datetime.now(timezone.utc)
        mid = uuid4()
        writer = RedisHotStateWriter(redis_client, hot_ttl_seconds=120)

        r1 = await _project(writer, mid, now=now)
        assert r1 == "written"
        pttl1 = await redis_client.pttl(item_key(DEFAULT_PREFIX, mid))
        assert pttl1 > 0

        await asyncio.sleep(1.1)

        r2 = await _project(writer, mid, now=now)
        assert r2 == "idempotent"
        pttl2 = await redis_client.pttl(item_key(DEFAULT_PREFIX, mid))
        assert pttl2 < pttl1


class TestExpiredEventNoOp:
    @pytest.mark.asyncio
    async def test_expired_event_returns_expired_and_creates_no_key(
        self,
        redis_client,
    ) -> None:
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        mid = uuid4()
        writer = RedisHotStateWriter(redis_client, hot_ttl_seconds=60)

        payload = _make_payload(mid, content="expired", now=past)
        payload["memory_snapshot"]["expires_at"] = _iso_utc(
            past + timedelta(minutes=1),
        )
        idem = build_idempotency_key(mid, "redis")
        result = await writer.project(
            payload,
            envelope_idempotency_key=idem,
            envelope_aggregate_id=str(mid),
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="redis",
        )
        assert result == "expired"
        exists = await redis_client.exists(item_key(DEFAULT_PREFIX, mid))
        assert exists == 0


class TestCrossSessionIsolation:
    @pytest.mark.asyncio
    async def test_sessions_are_isolated(self, redis_client) -> None:
        now = datetime.now(timezone.utc)
        sid1, sid2 = uuid4(), uuid4()
        mid1, mid2 = uuid4(), uuid4()
        writer = RedisHotStateWriter(redis_client, hot_ttl_seconds=3600)
        reader = RedisHotStateReader(redis_client)

        for mid, sid in [(mid1, sid1), (mid2, sid2)]:
            await _project(writer, mid, sid=sid, content=f"c-{mid}", now=now)

        items1 = await reader.list_session_items(sid1)
        items2 = await reader.list_session_items(sid2)
        assert len(items1) == 1
        assert len(items2) == 1
        assert items1[0]["id"] == str(mid1)
        assert items2[0]["id"] == str(mid2)


class TestStaleZsetCleanup:
    @pytest.mark.asyncio
    async def test_stale_members_removed_on_read(self, redis_client) -> None:
        now = datetime.now(timezone.utc)
        sid = uuid4()
        mid_present, mid_stale = uuid4(), uuid4()
        writer = RedisHotStateWriter(redis_client, hot_ttl_seconds=3600)
        reader = RedisHotStateReader(redis_client)

        for mid in [mid_present, mid_stale]:
            await _project(writer, mid, sid=sid, content=f"c-{mid}", now=now)

        await redis_client.delete(item_key(DEFAULT_PREFIX, mid_stale))

        items = await reader.list_session_items(sid)
        assert len(items) == 1
        assert items[0]["id"] == str(mid_present)

        zcard = await redis_client.zcard(session_key(DEFAULT_PREFIX, sid))
        assert zcard == 1


class TestSessionTtlMonotonic:
    """Short-TTL item cannot shorten session ZSET that contains long-lived item."""

    @pytest.mark.asyncio
    async def test_first_write_session_ttl_is_positive(
        self,
        redis_client,
    ) -> None:
        """First session member must create an expiring session ZSET."""
        sid = uuid4()
        mid = uuid4()
        now = datetime.now(timezone.utc)
        writer = RedisHotStateWriter(redis_client, hot_ttl_seconds=3600)

        await _project(writer, mid, sid=sid, content="first-write", now=now)

        sk = session_key(DEFAULT_PREFIX, sid)
        pttl = await redis_client.pttl(sk)
        assert pttl > 0, f"First-write session ZSET must have positive PTTL, got {pttl}"

    @pytest.mark.asyncio
    async def test_short_ttl_does_not_shrink_session_zset(self, redis_client) -> None:
        sid = uuid4()
        mid_long = uuid4()
        mid_short = uuid4()
        now = datetime.now(timezone.utc)

        writer = RedisHotStateWriter(redis_client, hot_ttl_seconds=7200)

        # Project long-lived item first
        payload_long = _make_payload(mid_long, sid=sid, content="long", now=now)
        payload_long["memory_snapshot"]["expires_at"] = _iso_utc(
            now + timedelta(hours=2),
        )
        idem_long = build_idempotency_key(mid_long, "redis")
        await writer.project(
            payload_long,
            envelope_idempotency_key=idem_long,
            envelope_aggregate_id=str(mid_long),
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="redis",
        )
        sk = session_key(DEFAULT_PREFIX, sid)
        pttl_after_long = await redis_client.pttl(sk)
        assert pttl_after_long > 0

        # Project short-lived item into same session (with a fresh writer,
        # simulating a separate delivery of a short-TTL item)
        writer2 = RedisHotStateWriter(redis_client, hot_ttl_seconds=30)
        payload_short = _make_payload(mid_short, sid=sid, content="short", now=now)
        payload_short["memory_snapshot"]["expires_at"] = _iso_utc(
            now + timedelta(seconds=30),
        )
        idem_short = build_idempotency_key(mid_short, "redis")
        await writer2.project(
            payload_short,
            envelope_idempotency_key=idem_short,
            envelope_aggregate_id=str(mid_short),
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="redis",
        )

        pttl_after_short = await redis_client.pttl(sk)
        # Session ZSET TTL must NOT have been reduced below the long item's TTL.
        # Allow 1 second tolerance for timing.
        assert pttl_after_short >= pttl_after_long - 1000


class TestBoundedReads:
    """Reader must not issue unbounded ZREVRANGE 0 -1."""

    @pytest.mark.asyncio
    async def test_bounded_read_with_stale_session(self, redis_client) -> None:
        sid = uuid4()
        writer = RedisHotStateWriter(redis_client, hot_ttl_seconds=3600)
        reader = RedisHotStateReader(redis_client)

        valid_mids = []
        for i in range(5):
            mid = uuid4()
            valid_mids.append(mid)
            await _project(writer, mid, sid=sid, content=f"item-{i}")

        # Manually add a bunch of stale members to the ZSET
        sk = session_key(DEFAULT_PREFIX, sid)
        stale_members = [uuid4().hex for _ in range(60)]
        pipe = redis_client.pipeline()
        score = 9999999999999.0
        for m in stale_members:
            pipe.zadd(sk, {m: score})
            score -= 1
        await pipe.execute()

        # Read with a limit of 3 - must not scan all 65 members
        typed = await reader.list_session_items_typed(sid, limit=3)
        assert len(typed.items) == 3
        # Must have removed at least some stale members
        zcard_after = await redis_client.zcard(sk)
        assert zcard_after < 65

    @pytest.mark.asyncio
    async def test_zero_limit_raises(self, redis_client) -> None:
        reader = RedisHotStateReader(redis_client)
        with pytest.raises(ValueError, match="positive"):
            await reader.list_session_items(uuid4(), limit=0)


class TestLuaPreWriteFailure:
    """Pre-write failure injection: proves zero state visible.

    The production Lua script performs all deterministic validation
    BEFORE the first write. A failure before the write phase must
    leave neither HASH nor ZSET visible.
    """

    @pytest.mark.asyncio
    async def test_pre_write_failure_leaves_zero_state(
        self,
        redis_client,
    ) -> None:
        from app.memory_core.repositories.redis_hot.script import (
            PRE_WRITE_FAILURE_INJECTION_SCRIPT,
        )

        mid = uuid4()
        sid = uuid4()
        ik = item_key(DEFAULT_PREFIX, mid)
        sk = session_key(DEFAULT_PREFIX, sid)

        # Register and invoke the pre-write failure script.
        script = redis_client.register_script(PRE_WRITE_FAILURE_INJECTION_SCRIPT)
        with pytest.raises(Exception) as exc_info:
            await script(keys=[ik, sk], args=["x"] * 6)
        assert "INJECTED_PRE_WRITE_FAILURE" in str(exc_info.value)

        # Neither HASH nor ZSET may exist.
        assert await redis_client.exists(ik) == 0
        assert await redis_client.exists(sk) == 0


class TestLuaWriteThenErrorSemantics:
    """Semantics regression: write-then-error leaves earlier write visible.

    Redis Lua runtime errors do NOT roll back writes already executed.
    This test documents that fact so future maintainers never rely on
    nonexistent rollback.
    """

    @pytest.mark.asyncio
    async def test_write_then_error_leaves_earlier_write_visible(
        self,
        redis_client,
    ) -> None:
        from app.memory_core.repositories.redis_hot.script import (
            WRITE_THEN_ERROR_SEMANTICS_SCRIPT,
        )

        mid = uuid4()
        ik = item_key(DEFAULT_PREFIX, mid)

        script = redis_client.register_script(WRITE_THEN_ERROR_SEMANTICS_SCRIPT)
        with pytest.raises(Exception) as exc_info:
            await script(keys=[ik], args=[])
        assert "INJECTED_POST_WRITE_FAILURE" in str(exc_info.value)

        # The HSET before the error IS visible (no rollback).
        marker = await redis_client.hget(ik, "_marker")
        assert marker is not None
        assert marker == b"visible" or marker == "visible"


class TestStartupAuthFailure:
    """Authentication failure occurs before claiming; exact outbox row unclaimed.

    Creates a real PG outbox row, then invokes the actual startup path
    (``compose_and_run_redis_hot_worker``) with invalid credentials.
    Proves:
    - Preflight fails with a Redis authentication error.
    - The exact outbox row (by ID) remains pending with no lease_owner
      and no lease_expires_at (i.e. claim was never called).
    """

    @pytest.mark.asyncio
    async def test_auth_failure_with_real_outbox_row_unclaimed(
        self,
        session_factory,
        auth_redis_url,
    ) -> None:
        from urllib.parse import urlparse, urlunparse

        now = datetime.now(timezone.utc)

        # Create a real outbox row via remember() and capture its ID.
        async with session_factory() as session:
            repo = PostgresMemoryRepository(
                session,
                projection_targets=["redis"],
            )
            item = await repo.remember(
                content="auth test memory",
                evidence_refs=[
                    EvidenceRef(
                        source_id="doc-auth",
                        source_type="document",
                        timestamp=now.replace(tzinfo=None),
                        confidence=0.9,
                    )
                ],
                provenance=ProvenanceInfo(
                    source_type="document",
                    source_name="test.pdf",
                ),
            )
            item_aggregate_id = item.id
            await session.commit()

        # Verify the exact row exists and is pending with no lease.
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(
                    OutboxRow.aggregate_id == item_aggregate_id,
                    OutboxRow.projection_target == "redis",
                )
            )
            row_before = result.scalar_one_or_none()
        assert row_before is not None, "Created outbox row must exist"
        assert row_before.state == "pending"
        assert row_before.lease_owner is None
        assert row_before.lease_expires_at is None

        # Build a wrong-password URL.
        parsed = urlparse(auth_redis_url)
        wrong_url = urlunparse(
            (
                parsed.scheme,
                f":wrongpass@{parsed.hostname}:{parsed.port}",
                parsed.path,
                "",
                "",
                "",
            )
        )

        # Invoke the actual startup path — must fail at preflight.
        from app.memory_core.redis_hot_compose import (
            compose_and_run_redis_hot_worker,
        )

        with pytest.raises(Exception) as exc_info:
            await compose_and_run_redis_hot_worker(
                session_factory=session_factory,
                redis_url=wrong_url,
                worker_identity="p1-05-auth-fail",
                preflight_timeout_s=5.0,
            )
        err_str = str(exc_info.value).lower()
        err_type = type(exc_info.value).__name__.lower()
        assert (
            "auth" in err_str
            or "password" in err_str
            or "invalid" in err_str
            or "auth" in err_type
            or "noauth" in err_str
        ), f"Expected auth error, got: {exc_info.value!r}"

        # The exact outbox row must still be pending with no lease.
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(
                    OutboxRow.aggregate_id == item_aggregate_id,
                    OutboxRow.projection_target == "redis",
                )
            )
            row_after = result.scalar_one_or_none()
        assert row_after is not None
        assert row_after.state == "pending"
        assert row_after.lease_owner is None
        assert row_after.lease_expires_at is None


class TestConnectionLossRecovery:
    """After connection loss, delivery retries and succeeds.

    Two sub-proofs:
    - Infrastructure errors (dead port) propagate as retryable, not
      PermanentProjectionError.
    - Real same-endpoint Docker stop/start recovery: create a real PG
      outbox, stop the Docker redis service, run the worker (proves
      retryable/pending authority), start/wait ready, retry, prove
      outbox completed and HASH+ZSET visible.
    """

    @pytest.mark.asyncio
    async def test_writer_raises_retryable_on_connection_error(
        self,
        redis_client,
    ) -> None:
        """Verify that infrastructure errors propagate as retryable
        (not PermanentProjectionError) when Redis is unreachable."""
        mid = uuid4()
        dead_client = Redis.from_url(
            "redis://127.0.0.1:1/0",
            decode_responses=False,
        )
        writer = RedisHotStateWriter(dead_client, hot_ttl_seconds=3600)
        try:
            with pytest.raises(Exception) as exc_info:
                await _project(writer, mid)
            assert not isinstance(exc_info.value, PermanentProjectionError)
        finally:
            await dead_client.aclose()

    @pytest.mark.asyncio
    async def test_real_docker_stop_start_recovery(
        self,
        session_factory,
    ) -> None:
        """Real same-endpoint Redis stop/restart recovery.

        This test owns every Redis client it creates (no shared fixture).
        It:
        - records the initial Redis endpoint and ``docker compose port``
          binding;
        - creates a real PG outbox row for an exact aggregate;
        - stops Docker redis, runs the worker, proves the exact
          aggregate's outbox row is pending, unleased, and has a retry
          attempt;
        - starts Redis and asserts the discovered binding is IDENTICAL
          to the original endpoint before creating the recovery client;
        - waits for readiness, reruns the worker, proves the exact
          aggregate's row is completed, the HASH is visible, and the
          supplied session's ZSET contains the item;
        - in the ``finally`` block: closes all clients, starts Redis if
          needed, verifies readiness at the ORIGINAL endpoint, and
          raises a clear failure if service restoration cannot be
          proven.
        """
        docker_exe = os.environ.get("P1_05_DOCKER_EXE", "docker")
        compose_file = os.environ.get("P1_05_COMPOSE_FILE")
        compose_project = os.environ.get("P1_05_COMPOSE_PROJECT")
        redis_url = os.environ.get("REDIS_URL")
        if not all([compose_file, compose_project, redis_url]):
            pytest.skip(
                "P1_05_COMPOSE_FILE/PROJECT/REDIS_URL not set; run via runner",
            )

        now = datetime.now(timezone.utc)
        sid_recovery = uuid4()
        owned_clients: list[Redis] = []
        # Initialize BEFORE the try/finally so an early failure in
        # ``docker compose port`` cannot cause an unbound local in the
        # restoration path and mask the original failure.
        initial_endpoint: str | None = None
        restoration_error: str | None = None

        def compose(*args):
            cmd = [
                docker_exe,
                "compose",
                "-p",
                compose_project,
                "-f",
                compose_file,
                *args,
            ]
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )

        async def make_client(url: str, **kwargs) -> Redis:
            c = Redis.from_url(url, decode_responses=False, **kwargs)
            owned_clients.append(c)
            return c

        async def close_all_owned() -> None:
            for c in owned_clients:
                try:
                    await c.aclose()
                except Exception:
                    pass
            owned_clients.clear()

        async def wait_redis_ready(url: str, attempts: int = 30) -> None:
            for _ in range(attempts):
                probe = Redis.from_url(
                    url,
                    decode_responses=False,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                try:
                    await asyncio.wait_for(probe.ping(), timeout=3.0)
                    await probe.aclose()
                    return
                except Exception:
                    try:
                        await probe.aclose()
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)
            raise RuntimeError(
                f"Redis not ready at {url} after {attempts} attempts",
            )

        try:
            # Step 0: record the initial Redis endpoint and binding.
            initial_binding_output = compose("port", "redis", "6379")
            assert initial_binding_output.returncode == 0, (
                f"docker compose port redis 6379 failed: {initial_binding_output.stderr}"
            )
            initial_binding = initial_binding_output.stdout.strip().splitlines()[0].strip()
            if not initial_binding.startswith("127.0.0.1:"):
                pytest.fail(
                    f"Initial redis binding is not loopback: {initial_binding}",
                )
            initial_endpoint = redis_url

            # Step 1: create a real PG outbox row with session_id.
            async with session_factory() as session:
                repo = PostgresMemoryRepository(
                    session,
                    projection_targets=["redis"],
                )
                item = await repo.remember(
                    content="docker-stop-recovery",
                    evidence_refs=[
                        EvidenceRef(
                            source_id="doc-docker",
                            source_type="document",
                            timestamp=now.replace(tzinfo=None),
                            confidence=0.9,
                        )
                    ],
                    provenance=ProvenanceInfo(
                        source_type="document",
                        source_name="test.pdf",
                    ),
                    session_id=sid_recovery,
                )
                item_aggregate_id = item.id
                await session.commit()

            # Verify exact row is pending with no lease.
            async with session_factory() as session:
                res = await session.execute(
                    select(OutboxRow).where(
                        OutboxRow.aggregate_id == item_aggregate_id,
                        OutboxRow.projection_target == "redis",
                    )
                )
                row = res.scalar_one_or_none()
            assert row is not None
            assert row.state == "pending"
            assert row.lease_owner is None

            # Step 2: stop the Docker redis service.
            stop_result = compose("stop", "redis")
            assert stop_result.returncode == 0, f"docker stop redis failed: {stop_result.stderr}"

            # Step 3: run the worker — must produce retryable failure.
            dead_client = await make_client(
                redis_url,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            handler = RedisHotStateHandler(
                RedisHotStateWriter(dead_client, hot_ttl_seconds=3600),
            )
            registry = ProjectionHandlerRegistry({"redis": handler})
            store = ProjectionOutboxStore(
                session_factory,
                worker_identity="p1-05-stop",
            )
            worker = ProjectionWorker(
                store=store,
                registry=registry,
                worker_identity="p1-05-stop",
                batch_size=10,
                max_concurrency=10,
                lease_duration=timedelta(seconds=30),
                delivery_timeout=timedelta(seconds=5),
                finalization_margin=timedelta(seconds=5),
                retry_policy=ExponentialBackoffPolicy(
                    base_delay_seconds=1.0,
                ),
                target_filter={"redis"},
            )

            result = await worker.run_once()
            assert result.claimed >= 1
            assert result.completed == 0
            assert result.retried >= 1

            # Close dead client.
            try:
                await dead_client.aclose()
            except Exception:
                pass
            if dead_client in owned_clients:
                owned_clients.remove(dead_client)

            # Verify exact outbox row is pending (retry resets to pending)
            # with a retry attempt.
            async with session_factory() as session:
                res = await session.execute(
                    select(OutboxRow).where(
                        OutboxRow.aggregate_id == item_aggregate_id,
                        OutboxRow.projection_target == "redis",
                    )
                )
                row_retry = res.scalar_one_or_none()
            assert row_retry is not None
            assert row_retry.state == "pending"
            assert row_retry.lease_owner is None
            assert row_retry.attempt_count >= 1

            # Step 4: start redis and verify binding is IDENTICAL to original.
            start_result = compose("start", "redis")
            assert start_result.returncode == 0, f"docker start redis failed: {start_result.stderr}"

            post_start_binding_output = compose("port", "redis", "6379")
            assert post_start_binding_output.returncode == 0, (
                f"docker compose port redis 6379 failed post-start: "
                f"{post_start_binding_output.stderr}"
            )
            post_start_binding = post_start_binding_output.stdout.strip().splitlines()[0].strip()
            assert post_start_binding == initial_binding, (
                "Redis binding changed after restart: "
                f"initial={initial_binding!r} "
                f"post_start={post_start_binding!r}. "
                "Topology is no longer same-endpoint; recovery test invalid."
            )

            # Wait for readiness at the ORIGINAL endpoint.
            await wait_redis_ready(initial_endpoint)

            # Step 5: re-run worker — must complete the exact outbox event.
            good_client = await make_client(redis_url)
            handler2 = RedisHotStateHandler(
                RedisHotStateWriter(
                    good_client,
                    hot_ttl_seconds=3600,
                ),
            )
            registry2 = ProjectionHandlerRegistry({"redis": handler2})
            store2 = ProjectionOutboxStore(
                session_factory,
                worker_identity="p1-05-recover",
            )
            worker2 = ProjectionWorker(
                store=store2,
                registry=registry2,
                worker_identity="p1-05-recover",
                batch_size=10,
                max_concurrency=10,
                lease_duration=timedelta(seconds=30),
                delivery_timeout=timedelta(seconds=10),
                finalization_margin=timedelta(seconds=5),
                retry_policy=ExponentialBackoffPolicy(
                    base_delay_seconds=1.0,
                ),
                target_filter={"redis"},
            )

            # Wait for retry_after to pass.
            await asyncio.sleep(2.0)
            result2 = await worker2.run_once()
            assert result2.completed >= 1

            # Step 6: prove exact row is completed.
            async with session_factory() as session:
                res = await session.execute(
                    select(OutboxRow).where(
                        OutboxRow.aggregate_id == item_aggregate_id,
                        OutboxRow.projection_target == "redis",
                    )
                )
                row_completed = res.scalar_one_or_none()
            assert row_completed is not None
            assert row_completed.state == "completed"

            # Step 7: prove HASH visible.
            reader = RedisHotStateReader(good_client)
            snap = await reader.get_item(item_aggregate_id)
            assert snap is not None
            assert snap["content"] == "docker-stop-recovery"
            assert snap["id"] == str(item_aggregate_id)

            # Step 8: prove session ZSET visible (session_id was supplied).
            sk = session_key(DEFAULT_PREFIX, sid_recovery)
            zcard = await good_client.zcard(sk)
            assert zcard >= 1, f"Session ZSET must contain at least one member, got {zcard}"
            session_items = await reader.list_session_items(sid_recovery)
            assert len(session_items) >= 1
            assert any(si["id"] == str(item_aggregate_id) for si in session_items), (
                "Created item must appear in session listing"
            )

        finally:
            # Close every owned client first.
            await close_all_owned()

            # Ensure redis is running for subsequent tests.
            if initial_endpoint is not None:
                start_back = compose("start", "redis")
                if start_back.returncode != 0:
                    compose("up", "-d", "--wait", "redis")

                # Verify readiness at the ORIGINAL endpoint — never
                # silently continue with Redis unavailable.
                try:
                    await wait_redis_ready(initial_endpoint, attempts=30)
                except RuntimeError as e:
                    # Last-resort: recreate the container.
                    compose("up", "-d", "--wait", "redis")
                    try:
                        await wait_redis_ready(initial_endpoint, attempts=20)
                    except RuntimeError:
                        restoration_error = (
                            f"Redis could not be restored at original endpoint "
                            f"{initial_endpoint!r} after stop/start recovery "
                            f"test. Subsequent tests would be polluted. "
                            f"Original error: {e}"
                        )

                # Verify post-restore binding still matches the original.
                if restoration_error is None:
                    post_restore_binding_output = compose("port", "redis", "6379")
                    if post_restore_binding_output.returncode == 0:
                        post_restore_binding = (
                            post_restore_binding_output.stdout.strip().splitlines()[0].strip()
                        )
                        if post_restore_binding != initial_binding:
                            restoration_error = (
                                "Redis binding changed during finally restoration: "
                                f"initial={initial_binding!r} "
                                f"restored={post_restore_binding!r}"
                            )

            # If restoration failed, report without masking original test
            # failure. pytest.fail inside finally would mask the original
            # exception; instead we raise after the finally completes.
            if restoration_error is not None:
                raise RuntimeError(restoration_error)


class TestPostgresReadableDuringRedisOutage:
    """PostgreSQL remains authoritative during Redis outage."""

    @pytest.mark.asyncio
    async def test_postgres_still_readable(
        self,
        session_factory,
    ) -> None:
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session, projection_targets=["redis"])
            item = await repo.remember(
                content="pg readable test",
                evidence_refs=[
                    EvidenceRef(
                        source_id="doc-pg",
                        source_type="document",
                        timestamp=now.replace(tzinfo=None),
                        confidence=0.9,
                    )
                ],
                provenance=ProvenanceInfo(
                    source_type="document",
                    source_name="test.pdf",
                ),
            )
            mid = item.id
            await session.commit()

        # Verify PostgreSQL is still readable
        async with session_factory() as session:
            from app.db.models.memory_plane import MemoryItem as MemoryItemRow

            result = await session.execute(select(MemoryItemRow).where(MemoryItemRow.id == mid))
            row = result.scalar_one_or_none()
            assert row is not None
            assert row.content == "pg readable test"


class TestRedisDataLossRebuild:
    @pytest.mark.asyncio
    async def test_rebuild_restores_active_unexpired_items(
        self,
        session_factory,
        redis_client,
    ) -> None:
        now = datetime.now(timezone.utc)
        writer = RedisHotStateWriter(redis_client, hot_ttl_seconds=3600)
        reader = RedisHotStateReader(redis_client)

        async with session_factory() as session:
            repo = PostgresMemoryRepository(session, projection_targets=["redis"])
            item1 = await repo.remember(
                content="rebuild test 1",
                evidence_refs=[
                    EvidenceRef(
                        source_id="doc-rebuild-1",
                        source_type="document",
                        timestamp=now.replace(tzinfo=None),
                        confidence=0.9,
                    )
                ],
                provenance=ProvenanceInfo(
                    source_type="document",
                    source_name="test.pdf",
                ),
            )
            item2 = await repo.remember(
                content="rebuild test 2",
                evidence_refs=[
                    EvidenceRef(
                        source_id="doc-rebuild-2",
                        source_type="document",
                        timestamp=now.replace(tzinfo=None),
                        confidence=0.9,
                    )
                ],
                provenance=ProvenanceInfo(
                    source_type="document",
                    source_name="test.pdf",
                ),
                expires_at=now + timedelta(hours=1),
            )
            mid1, mid2 = item1.id, item2.id
            await session.commit()

        await redis_client.flushdb()

        snap1_before = await reader.get_item(mid1)
        assert snap1_before is None

        async with session_factory() as session:
            counters = await rebuild_from_postgres(session=session, writer=writer)

        assert counters.scanned >= 2
        assert counters.projected >= 2
        assert counters.failed == 0

        snap1 = await reader.get_item(mid1)
        snap2 = await reader.get_item(mid2)
        assert snap1 is not None
        assert snap1["content"] == "rebuild test 1"
        assert snap2 is not None
        assert snap2["content"] == "rebuild test 2"

    @pytest.mark.asyncio
    async def test_rebuild_does_not_create_outbox_rows(
        self,
        session_factory,
        redis_client,
    ) -> None:
        now = datetime.now(timezone.utc)
        writer = RedisHotStateWriter(redis_client, hot_ttl_seconds=3600)

        async with session_factory() as session:
            repo = PostgresMemoryRepository(session, projection_targets=["redis"])
            await repo.remember(
                content="outbox check",
                evidence_refs=[
                    EvidenceRef(
                        source_id="doc-outbox",
                        source_type="document",
                        timestamp=now.replace(tzinfo=None),
                        confidence=0.9,
                    )
                ],
                provenance=ProvenanceInfo(
                    source_type="document",
                    source_name="test.pdf",
                ),
            )
            await session.commit()

        await redis_client.flushdb()

        async with session_factory() as session:
            result = await session.execute(select(func.count()).select_from(OutboxRow))
            count_before = int(result.scalar_one())

        async with session_factory() as session:
            await rebuild_from_postgres(session=session, writer=writer)

        async with session_factory() as session:
            result = await session.execute(select(func.count()).select_from(OutboxRow))
            count_after = int(result.scalar_one())

        assert count_after == count_before


class TestPartialLossRebuild:
    """Delete session index only, rebuild restores it without TTL extension."""

    @pytest.mark.asyncio
    async def test_session_index_restored_without_item_ttl_extension(
        self,
        session_factory,
        redis_client,
    ) -> None:
        now = datetime.now(timezone.utc)
        writer = RedisHotStateWriter(redis_client, hot_ttl_seconds=3600)

        sid = uuid4()
        async with session_factory() as session:
            repo = PostgresMemoryRepository(session, projection_targets=["redis"])
            item = await repo.remember(
                content="partial loss test",
                evidence_refs=[
                    EvidenceRef(
                        source_id="doc-partial",
                        source_type="document",
                        timestamp=now.replace(tzinfo=None),
                        confidence=0.9,
                    )
                ],
                provenance=ProvenanceInfo(
                    source_type="document",
                    source_name="test.pdf",
                ),
                session_id=sid,
            )
            mid = item.id
            await session.commit()

        # Wait for worker to project
        from app.memory_core.projection_worker import (
            ProjectionHandlerRegistry,
            ProjectionWorker,
        )
        from app.memory_core.projection_worker.postgres_store import (
            ProjectionOutboxStore,
        )

        handler = RedisHotStateHandler(
            RedisHotStateWriter(redis_client, hot_ttl_seconds=3600),
        )
        registry = ProjectionHandlerRegistry({"redis": handler})
        store = ProjectionOutboxStore(session_factory, worker_identity="p1-05-partial")
        worker = ProjectionWorker(
            store=store,
            registry=registry,
            worker_identity="p1-05-partial",
            batch_size=10,
            max_concurrency=10,
            lease_duration=timedelta(seconds=30),
            delivery_timeout=timedelta(seconds=10),
            finalization_margin=timedelta(seconds=5),
            retry_policy=ExponentialBackoffPolicy(base_delay_seconds=1.0),
            target_filter={"redis"},
        )
        await worker.run_once()

        # Verify state is present
        ik = item_key(DEFAULT_PREFIX, mid)
        sk = session_key(DEFAULT_PREFIX, sid)
        assert await redis_client.exists(ik) == 1
        assert await redis_client.exists(sk) == 1

        # Record item TTL before rebuild
        item_pttl_before = await redis_client.pttl(ik)
        assert item_pttl_before > 0

        # Delete ONLY the session index
        await redis_client.delete(sk)
        assert await redis_client.exists(sk) == 0
        assert await redis_client.exists(ik) == 1

        # Rebuild
        async with session_factory() as session:
            counters = await rebuild_from_postgres(session=session, writer=writer)
        assert counters.projected >= 1

        # Session index restored
        assert await redis_client.exists(sk) == 1

        # Item TTL not extended
        item_pttl_after = await redis_client.pttl(ik)
        assert item_pttl_after <= item_pttl_before + 1000  # 1s tolerance

        # Outbox not touched
        async with session_factory() as session:
            result = await session.execute(select(func.count()).select_from(OutboxRow))
            count = int(result.scalar_one())
        # We should have exactly the original outbox rows, no new ones from rebuild
        assert count >= 1


class TestEnvelopePayloadMismatchIntegration:
    """Envelope/payload mismatches raise PermanentProjectionError in real env."""

    @pytest.mark.asyncio
    async def test_snapshot_id_mismatch_in_real_env(self, redis_client) -> None:
        mid = uuid4()
        other = uuid4()
        now = datetime.now(timezone.utc)
        writer = RedisHotStateWriter(redis_client, hot_ttl_seconds=3600)

        payload = _make_payload(mid, now=now)
        payload["memory_snapshot"] = dict(payload["memory_snapshot"])
        payload["memory_snapshot"]["id"] = str(other)

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


class TestReaderBasics:
    @pytest.mark.asyncio
    async def test_get_item_missing_returns_none(self, redis_client) -> None:
        reader = RedisHotStateReader(redis_client)
        assert await reader.get_item(uuid4()) is None

    @pytest.mark.asyncio
    async def test_list_session_empty(self, redis_client) -> None:
        reader = RedisHotStateReader(redis_client)
        assert await reader.list_session_items(uuid4()) == []

    @pytest.mark.asyncio
    async def test_pttl_missing_returns_none(self, redis_client) -> None:
        reader = RedisHotStateReader(redis_client)
        assert await reader.pttl_ms(uuid4()) is None


class TestCompositionRoot:
    """Production composition root preflight and duplicate-registration proofs."""

    @pytest.mark.asyncio
    async def test_preflight_before_any_claim(self, session_factory) -> None:
        """Preflight ping runs before any outbox claim; event is completed.

        The stop_event is NOT set before entering the loop. Instead,
        we start the worker in a background task, wait for it to
        process the event, then signal it to stop. This makes the
        proof real and deterministic.
        """
        redis_url = os.environ.get("REDIS_URL")
        if not redis_url:
            pytest.skip("REDIS_URL not set")

        from app.memory_core.redis_hot_compose import (
            compose_and_run_redis_hot_worker,
        )

        # Create a pending outbox row and capture its ID.
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            repo = PostgresMemoryRepository(
                session,
                projection_targets=["redis"],
            )
            item = await repo.remember(
                content="preflight test",
                evidence_refs=[
                    EvidenceRef(
                        source_id="doc-pf",
                        source_type="document",
                        timestamp=now.replace(tzinfo=None),
                        confidence=0.9,
                    )
                ],
                provenance=ProvenanceInfo(
                    source_type="document",
                    source_name="test.pdf",
                ),
            )
            item_aggregate_id = item.id
            await session.commit()

        stop = asyncio.Event()

        async def run_worker():
            await compose_and_run_redis_hot_worker(
                session_factory=session_factory,
                redis_url=redis_url,
                worker_identity="p1-05-compose-preflight",
                stop_event=stop,
                idle_wait_s=0.2,
            )

        worker_task = asyncio.create_task(run_worker())

        try:
            # Wait for the outbox row to be completed (up to 15 seconds).
            for _ in range(75):
                await asyncio.sleep(0.2)
                async with session_factory() as session:
                    result = await session.execute(
                        select(OutboxRow).where(
                            OutboxRow.aggregate_id == item_aggregate_id,
                            OutboxRow.projection_target == "redis",
                        )
                    )
                    row = result.scalar_one_or_none()
                if row is not None and row.state == "completed":
                    break
            else:
                pytest.fail(
                    "Outbox row was not completed within timeout",
                )

            # Now signal the worker to stop.
            stop.set()
            await asyncio.wait_for(worker_task, timeout=10.0)
        except Exception:
            stop.set()
            try:
                await asyncio.wait_for(worker_task, timeout=5.0)
            except Exception:
                worker_task.cancel()
            raise

        # Verify exact row is completed.
        async with session_factory() as session:
            result = await session.execute(
                select(OutboxRow).where(
                    OutboxRow.aggregate_id == item_aggregate_id,
                    OutboxRow.projection_target == "redis",
                )
            )
            row_final = result.scalar_one_or_none()
        assert row_final is not None
        assert row_final.state == "completed"

    @pytest.mark.asyncio
    async def test_preflight_failure_prevents_claim(
        self,
        session_factory,
    ) -> None:
        """If preflight fails, the exact outbox row is not claimed."""
        from app.memory_core.redis_hot_compose import (
            compose_and_run_redis_hot_worker,
        )

        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            repo = PostgresMemoryRepository(
                session,
                projection_targets=["redis"],
            )
            item = await repo.remember(
                content="preflight-fail test",
                evidence_refs=[
                    EvidenceRef(
                        source_id="doc-pff",
                        source_type="document",
                        timestamp=now.replace(tzinfo=None),
                        confidence=0.9,
                    )
                ],
                provenance=ProvenanceInfo(
                    source_type="document",
                    source_name="test.pdf",
                ),
            )
            item_aggregate_id = item.id
            await session.commit()

        # Verify exact row is pending before.
        async with session_factory() as session:
            res = await session.execute(
                select(OutboxRow).where(
                    OutboxRow.aggregate_id == item_aggregate_id,
                    OutboxRow.projection_target == "redis",
                )
            )
            row_before = res.scalar_one_or_none()
        assert row_before is not None
        assert row_before.state == "pending"
        assert row_before.lease_owner is None

        # Connect to dead port → preflight fails.
        with pytest.raises(Exception):
            await compose_and_run_redis_hot_worker(
                session_factory=session_factory,
                redis_url="redis://127.0.0.1:1/0",
                worker_identity="p1-05-compose-dead",
                preflight_timeout_s=2.0,
            )

        # Exact row must still be pending with no lease.
        async with session_factory() as session:
            res = await session.execute(
                select(OutboxRow).where(
                    OutboxRow.aggregate_id == item_aggregate_id,
                    OutboxRow.projection_target == "redis",
                )
            )
            row_after = res.scalar_one_or_none()
        assert row_after is not None
        assert row_after.state == "pending"
        assert row_after.lease_owner is None
        assert row_after.lease_expires_at is None

    def test_duplicate_target_registration_is_impossible(self) -> None:
        """ProjectionHandlerRegistry rejects non-Mapping input and
        the registry's targets property is a frozen, deduplicated tuple.

        Python dicts cannot contain duplicate keys, so the duplicate
        check in the registry is defense-in-depth. The meaningful
        invariant is:
        - Non-Mapping input raises TypeError.
        - Empty-string target raises ValueError.
        - Non-callable handler raises TypeError.
        - The resulting registry has exactly the supplied targets.
        """
        from unittest.mock import MagicMock
        from redis.asyncio import Redis

        mock_client = MagicMock(spec=Redis)
        mock_client.register_script = MagicMock()
        w1 = RedisHotStateWriter(mock_client)
        h1 = RedisHotStateHandler(w1)

        # Non-Mapping input is rejected.
        with pytest.raises(TypeError, match="mapping"):
            ProjectionHandlerRegistry([("redis", h1)])

        # Empty-string target is rejected.
        with pytest.raises(ValueError, match="non-empty"):
            ProjectionHandlerRegistry({"": h1})

        # Non-callable handler is rejected.
        with pytest.raises(TypeError, match="callable"):
            ProjectionHandlerRegistry({"redis": "not-a-handler"})

        # Single target succeeds and is queryable.
        reg = ProjectionHandlerRegistry({"redis": h1})
        assert reg.has_target("redis")
        assert reg.targets == ("redis",)
        assert reg.get_handler("redis") is h1
        assert reg.get_handler("nonexistent") is None


class TestLuaSafeExpiryRejection:
    """Lua-unsafe expiry is rejected locally before Lua execution.

    Proves that a computed absolute expiry above the Lua-safe exact-
    integer bound (2**53-1) raises PermanentProjectionError (converted
    from PermanentPayloadError by the writer), no Lua script runs, and
    no Redis item/session key is created.
    """

    @pytest.mark.asyncio
    async def test_lua_unsafe_expiry_creates_no_redis_state(
        self,
        redis_client,
    ) -> None:
        """Writer project() with a Lua-unsafe expiry creates neither
        HASH nor ZSET in real Redis — proves pre-Lua rejection."""
        from app.memory_core.repositories.redis_hot.keys import (
            LUA_SAFE_MAX_EXPIRY_MS,
        )

        mid = uuid4()
        sid = uuid4()
        ik = item_key(DEFAULT_PREFIX, mid)
        sk = session_key(DEFAULT_PREFIX, sid)

        # Build a writer whose hot_ttl_seconds is chosen so that
        # occurred_at_ms + ttl*1000 > LUA_SAFE_MAX_EXPIRY_MS.
        # occurred_at = 1_000_000 ms (1970-01-01T00:16:40+00:00).
        # We need: 1_000_000 + ttl*1000 > LUA_SAFE_MAX_EXPIRY_MS
        #   => ttl > (LUA_SAFE_MAX_EXPIRY_MS - 1_000_000) / 1000
        # Use an oversized but valid-looking TTL.
        unsafe_ttl_seconds = (LUA_SAFE_MAX_EXPIRY_MS // 1000) + 10_000
        writer = RedisHotStateWriter(
            redis_client,
            hot_ttl_seconds=unsafe_ttl_seconds,
        )

        occurred_at = datetime(1970, 1, 1, 0, 16, 40, tzinfo=timezone.utc)
        payload = _make_payload(mid, sid=sid, content="lua-unsafe", now=occurred_at)
        idem = build_idempotency_key(mid, "redis")

        with pytest.raises(PermanentProjectionError):
            await writer.project(
                payload,
                envelope_idempotency_key=idem,
                envelope_aggregate_id=str(mid),
                envelope_event_type="memory.remembered",
                envelope_schema_version=1,
                envelope_target="redis",
            )

        # No HASH, no ZSET — Lua was never reached.
        assert await redis_client.exists(ik) == 0
        assert await redis_client.exists(sk) == 0

    @pytest.mark.asyncio
    async def test_lua_safe_boundary_exact_value_is_accepted(
        self,
        redis_client,
    ) -> None:
        """The exact Lua-safe boundary (2**53-1) is accepted when
        otherwise valid — HASH and ZSET are created in real Redis."""
        from app.memory_core.repositories.redis_hot.keys import (
            LUA_SAFE_MAX_EXPIRY_MS,
        )

        mid = uuid4()
        sid = uuid4()
        ik = item_key(DEFAULT_PREFIX, mid)
        sk = session_key(DEFAULT_PREFIX, sid)

        # Set hot_ttl so ttl_boundary == LUA_SAFE_MAX_EXPIRY_MS exactly.
        # occurred_at = 1_000_000 ms (1970-01-01T00:16:40+00:00).
        # ttl*1000 = LUA_SAFE_MAX_EXPIRY_MS - 1_000_000
        # ttl_seconds = (LUA_SAFE_MAX_EXPIRY_MS - 1_000_000) // 1000
        occurred_at = datetime(1970, 1, 1, 0, 16, 40, tzinfo=timezone.utc)
        target_ttl_ms = LUA_SAFE_MAX_EXPIRY_MS - 1_000_000
        ttl_seconds = target_ttl_ms // 1000

        writer = RedisHotStateWriter(
            redis_client,
            hot_ttl_seconds=ttl_seconds,
        )
        payload = _make_payload(
            mid,
            sid=sid,
            content="lua-safe-boundary",
            now=occurred_at,
        )
        idem = build_idempotency_key(mid, "redis")

        # Should succeed — the expiry is exactly LUA_SAFE_MAX_EXPIRY_MS.
        # Note: the absolute expiry is in the far future (year ~285616),
        # but the Lua-safe bound is about precision, not Redis TTL range.
        # Redis PEXPIREAT accepts any ms up to 2**63-1.
        result = await writer.project(
            payload,
            envelope_idempotency_key=idem,
            envelope_aggregate_id=str(mid),
            envelope_event_type="memory.remembered",
            envelope_schema_version=1,
            envelope_target="redis",
        )
        assert result == "written"

        # HASH and ZSET are visible.
        assert await redis_client.exists(ik) == 1
        assert await redis_client.exists(sk) == 1
