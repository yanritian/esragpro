"""Direct live smoke test for P6-A world-memory authority.

Bypasses the HTTP layer and verifies the repository directly against
the live ``esragpro`` database. This confirms:
- Migration applied
- Repository can write and read back
- Cross-session durability
- Idempotency and conflict behavior
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from app.memory_core.repositories.world_memory_authority import (
    WorldMemoryAuthorityRepository,
    WorldMemoryIdempotencyConflict,
)
from app.memory_core.world_memory import WorldObservation
from app.memory_core.world_memory_serializer import (
    EVENT_OBSERVATION_INGESTED,
    build_world_memory_envelope,
)

DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/esragpro"


def build_test_envelope(content: str = "live smoke") -> dict[str, Any]:
    obs = WorldObservation(
        id=uuid4(),
        content=content,
        observed_at=datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc),
        source="live-smoke",
        ingested_at=datetime(2026, 7, 31, 12, 0, 1, tzinfo=timezone.utc),
        version=1,
        raw_evidence_ref="doc-live-1",
    )
    return build_world_memory_envelope(
        obs,
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="live-smoke",
    )


async def main() -> int:
    print(f"[smoke] Connecting to {DATABASE_URL.split('@')[-1]}")
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Clean up any prior smoke-test rows
    async with factory() as session:
        from sqlalchemy import text
        await session.execute(text(
            "DELETE FROM world_memory_envelopes "
            "WHERE projection_target = 'live-smoke'"
        ))
        await session.commit()

    # Session A: write + commit
    async with factory() as session_a:
        repo_a = WorldMemoryAuthorityRepository(session_a)
        envelope = build_test_envelope("smoke-content-1")
        stored = await repo_a.remember(envelope)
        await session_a.commit()
        stored_id = stored.id
        print(f"[smoke] Session A wrote row id={stored_id}, kind={stored.kind}")

    # Session B: read back (cross-session durability)
    async with factory() as session_b:
        repo_b = WorldMemoryAuthorityRepository(session_b)
        results = await repo_b.query(kind="observation")
        smoke = [
            r for r in results
            if r.envelope.get("world_memory_snapshot", {}).get("content") == "smoke-content-1"
        ]
        print(f"[smoke] Session B read back: {len(smoke)} row(s)")
        if not smoke:
            print("[smoke] FAIL: row not visible across sessions")
            return 1

    # Idempotency: exact duplicate returns same row
    async with factory() as session_c:
        repo_c = WorldMemoryAuthorityRepository(session_c)
        dup = await repo_c.remember(envelope)
        await session_c.commit()
        print(f"[smoke] Exact duplicate: id matches = {dup.id == stored_id}")
        if dup.id != stored_id:
            print("[smoke] FAIL: duplicate created a new row")
            return 1

    # Conflict: tampered payload raises
    import copy
    conflicting = copy.deepcopy(envelope)
    conflicting["world_memory_snapshot"]["content"] = "TAMPERED"
    async with factory() as session_d:
        repo_d = WorldMemoryAuthorityRepository(session_d)
        try:
            await repo_d.remember(conflicting)
            print("[smoke] FAIL: conflict not detected")
            return 1
        except WorldMemoryIdempotencyConflict:
            await session_d.rollback()
            print("[smoke] Conflict correctly rejected")

    # Evidence refusal
    async with factory() as session_e:
        repo_e = WorldMemoryAuthorityRepository(session_e)
        # Row without evidence
        no_ev = build_test_envelope("no-evidence")
        no_ev["world_memory_snapshot"]["raw_evidence_ref"] = None
        await repo_e.remember(no_ev)
        await session_e.commit()

        results = await repo_e.query(require_evidence=True)
        smoke_evidence = [
            r for r in results
            if r.envelope.get("world_memory_snapshot", {}).get("content") == "smoke-content-1"
        ]
        print(f"[smoke] require_evidence: found {len(smoke_evidence)} smoke row(s)")
        if len(smoke_evidence) != 1:
            print("[smoke] FAIL: evidence filter missed the smoke row")
            return 1

    # Cleanup
    async with factory() as session_f:
        from sqlalchemy import text
        await session_f.execute(text(
            "DELETE FROM world_memory_envelopes "
            "WHERE projection_target = 'live-smoke'"
        ))
        await session_f.commit()

    await engine.dispose()
    print("[smoke] PASS — PostgreSQL authority verified for P6-A")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
