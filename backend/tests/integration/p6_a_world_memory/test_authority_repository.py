"""Integration tests for the P6-A world-memory authority repository.

Covers:
- Schema/model mapping (stored envelope round-trip)
- Transaction rollback (no partial rows on failure)
- Cross-session read-back (write in session A, read in session B)
- Evidence refusal (require_evidence filters before max_items)
- Idempotency (exact duplicate returns existing row)
- Conflict (same key, different payload raises typed error)
- Deterministic ordering (occurred_at DESC, id ASC)
"""

from __future__ import annotations

import pytest

from app.memory_core.repositories.world_memory_authority import (
    WorldMemoryAuthorityRepository,
    WorldMemoryIdempotencyConflict,
)

from .conftest import make_observation_envelope


@pytest.mark.asyncio
class TestSchemaModelMapping:
    """Stored envelope round-trip preserves the full P7-B envelope."""

    async def test_remember_stores_full_envelope(self, session_factory):
        async with session_factory() as session:
            repo = WorldMemoryAuthorityRepository(session)
            envelope = make_observation_envelope(content="round trip test")
            stored = await repo.remember(envelope)
            await session.commit()

            assert stored.event_type == "world.observation.ingested"
            assert stored.schema_version == 1
            assert stored.kind == "observation"
            assert stored.idempotency_key == envelope["idempotency_key"]
            assert stored.aggregate_id.hex == envelope["aggregate_id"].replace("-", "")
            # Envelope round-trip
            assert stored.envelope["event_type"] == envelope["event_type"]
            assert stored.envelope["world_memory_snapshot"]["content"] == "round trip test"

    async def test_count_reflects_stored_rows(self, session_factory):
        async with session_factory() as session:
            repo = WorldMemoryAuthorityRepository(session)
            assert await repo.count() == 0
            await repo.remember(make_observation_envelope(content="one"))
            await repo.remember(make_observation_envelope(content="two"))
            await session.commit()
            assert await repo.count() == 2


@pytest.mark.asyncio
class TestTransactionSemantics:
    """Caller owns the transaction — rollback leaves no rows."""

    async def test_rollback_leaves_no_row(self, session_factory):
        async with session_factory() as session:
            repo = WorldMemoryAuthorityRepository(session)
            await repo.remember(make_observation_envelope(content="will rollback"))
            # No commit — simulate failure by rolling back
            await session.rollback()

        # Read from a fresh session
        async with session_factory() as session:
            repo = WorldMemoryAuthorityRepository(session)
            assert await repo.count() == 0

    async def test_commit_makes_row_visible(self, session_factory):
        async with session_factory() as session:
            repo = WorldMemoryAuthorityRepository(session)
            await repo.remember(make_observation_envelope(content="will commit"))
            await session.commit()

        async with session_factory() as session:
            repo = WorldMemoryAuthorityRepository(session)
            assert await repo.count() == 1


@pytest.mark.asyncio
class TestCrossSessionReadBack:
    """A write committed in session A is readable from session B."""

    async def test_cross_session_visibility(self, session_factory):
        # Session A writes and commits
        async with session_factory() as session_a:
            repo_a = WorldMemoryAuthorityRepository(session_a)
            envelope = make_observation_envelope(content="cross-session")
            stored = await repo_a.remember(envelope)
            await session_a.commit()
            stored_id = stored.id

        # Session B reads and finds the committed row
        async with session_factory() as session_b:
            repo_b = WorldMemoryAuthorityRepository(session_b)
            results = await repo_b.query()
            assert len(results) == 1
            assert results[0].id == stored_id
            assert results[0].envelope["world_memory_snapshot"]["content"] == "cross-session"


@pytest.mark.asyncio
class TestIdempotency:
    """Exact duplicate returns the existing record; no second row."""

    async def test_exact_duplicate_returns_existing(self, session_factory):
        async with session_factory() as session:
            repo = WorldMemoryAuthorityRepository(session)
            envelope = make_observation_envelope(content="idempotent")
            first = await repo.remember(envelope)
            await session.commit()

            second = await repo.remember(envelope)
            await session.commit()

            assert first.id == second.id
            assert await repo.count() == 1


@pytest.mark.asyncio
class TestConflict:
    """Conflicting reuse of idempotency key raises typed error."""

    async def test_conflicting_reuse_raises(self, session_factory):
        async with session_factory() as session:
            repo = WorldMemoryAuthorityRepository(session)
            envelope_a = make_observation_envelope(content="original")
            await repo.remember(envelope_a)
            await session.commit()

            # Build a conflicting envelope with the same idempotency key
            # but a different payload. Copy the original envelope and
            # tamper with the snapshot so payloads differ while the key
            # (derived from aggregate_id + event_type + target + version)
            # stays identical.
            import copy
            envelope_b = copy.deepcopy(envelope_a)
            envelope_b["world_memory_snapshot"]["content"] = "tampered content"

            with pytest.raises(WorldMemoryIdempotencyConflict) as exc_info:
                await repo.remember(envelope_b)
            assert exc_info.value.idempotency_key == envelope_a["idempotency_key"]


@pytest.mark.asyncio
class TestEvidenceRefusal:
    """require_evidence filters before max_items."""

    async def test_require_evidence_filters_out_evidenceless_rows(
        self, session_factory
    ):
        from .conftest import make_observation_envelope as make_env

        async with session_factory() as session:
            repo = WorldMemoryAuthorityRepository(session)
            # Row with evidence
            await repo.remember(make_env(content="has evidence", evidence_ref="doc-1"))
            # Row without evidence
            await repo.remember(make_env(content="no evidence", evidence_ref=None))
            await session.commit()

            # Query with require_evidence=True
            results = await repo.query(require_evidence=True)
            assert len(results) == 1
            assert results[0].envelope["world_memory_snapshot"]["content"] == "has evidence"

    async def test_require_evidence_does_not_miss_due_to_limit(
        self, session_factory
    ):
        """Evidence-backed rows beyond max_items non-evidence rows still appear.

        The fixture is deterministic: evidence-less rows are stamped with
        LATER ``occurred_at`` values than the evidence-backed row, so the
        ``occurred_at DESC`` query order is guaranteed to place the five
        evidence-less rows ahead of the single evidence-backed row. A SQL
        ``LIMIT 3`` applied before the evidence filter would therefore
        return three evidence-less rows and drop the evidence-backed row;
        the correct implementation fetches all matches, filters, then caps.
        """
        from .conftest import make_observation_envelope as make_env

        async with session_factory() as session:
            repo = WorldMemoryAuthorityRepository(session)
            # Insert five evidence-less rows with LATER timestamps so
            # they deterministically sort BEFORE the evidence-backed row
            # under the occurred_at DESC ordering.
            for i in range(5):
                env = make_env(content=f"no-evidence-{i}", evidence_ref=None)
                env["occurred_at"] = f"2026-07-31T12:{10 + i:02d}:00+00:00"
                await repo.remember(env)
            # Insert one evidence-backed row with an EARLIER timestamp
            # so it sorts LAST in the deterministic query order.
            evidence_env = make_env(content="has-evidence", evidence_ref="doc-1")
            evidence_env["occurred_at"] = "2026-07-31T12:00:00+00:00"
            await repo.remember(evidence_env)
            await session.commit()

            # max_items=3 without the fix would cap SQL at the three
            # most-recent (evidence-less) rows, missing the evidence row.
            # With the fix, we fetch all six, filter to the one
            # evidence-backed row, then truncate to max_items=3.
            results = await repo.query(require_evidence=True, max_items=3)
            assert len(results) == 1
            assert (
                results[0].envelope["world_memory_snapshot"]["content"]
                == "has-evidence"
            )

    async def test_non_positive_max_items_returns_empty(self, session_factory):
        """max_items <= 0 returns [] without executing a query (P1-02 parity)."""
        from .conftest import make_observation_envelope as make_env

        async with session_factory() as session:
            repo = WorldMemoryAuthorityRepository(session)
            await repo.remember(make_env(content="row", evidence_ref="doc-1"))
            await session.commit()

        async with session_factory() as session:
            repo = WorldMemoryAuthorityRepository(session)
            assert await repo.query(max_items=0) == []
            assert await repo.query(max_items=-1) == []
            assert await repo.query(require_evidence=True, max_items=0) == []


@pytest.mark.asyncio
class TestDeterministicOrdering:
    """Query results are deterministic: occurred_at DESC, id ASC."""

    async def test_ordering_is_deterministic(self, session_factory):
        async with session_factory() as session:
            repo = WorldMemoryAuthorityRepository(session)
            # Insert envelopes with different occurred_at
            env1 = make_observation_envelope(content="first")
            env2 = make_observation_envelope(content="second")
            # Tamper occurred_at to produce a known order
            env1["occurred_at"] = "2026-07-31T10:00:00+00:00"
            env2["occurred_at"] = "2026-07-31T12:00:00+00:00"

            await repo.remember(env1)
            await repo.remember(env2)
            await session.commit()

            results = await repo.query()
            # Most recent first
            assert results[0].envelope["world_memory_snapshot"]["content"] == "second"
            assert results[1].envelope["world_memory_snapshot"]["content"] == "first"
