"""P6-B: Migration upgrade/downgrade/re-upgrade test.

Verifies that the world_memory_projection_outbox table is created with
the correct columns, constraints, and indexes. The legacy
memory_projection_outbox is verified to remain untouched.
"""
from __future__ import annotations

from hashlib import md5

from sqlalchemy import CheckConstraint, text

from app.db.models.world_memory import WorldMemoryProjectionOutbox


async def test_projection_outbox_table_exists(session_factory):
    """After migration, world_memory_projection_outbox exists."""
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname='public' AND tablename = 'world_memory_projection_outbox'"
            )
        )
        rows = result.fetchall()
        assert len(rows) == 1, "world_memory_projection_outbox table must exist"


async def test_projection_outbox_columns(session_factory):
    """Table has all required columns."""
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = 'world_memory_projection_outbox' "
                "ORDER BY ordinal_position"
            )
        )
        columns = {row[0]: (row[1], row[2]) for row in result.fetchall()}

    required = [
        "id", "authority_envelope_id", "event_type", "schema_version",
        "aggregate_id", "projection_target", "idempotency_key", "envelope",
        "state", "attempt_count", "max_attempts", "available_at",
        "lease_owner", "lease_expires_at", "processed_at", "last_error",
        "created_at", "updated_at",
    ]
    for col in required:
        assert col in columns, f"Missing column: {col}"

    # Verify NOT NULL constraints on key columns
    for col in ["id", "authority_envelope_id", "event_type", "envelope", "state"]:
        assert columns[col][1] == "NO", f"{col} must be NOT NULL"


async def test_projection_outbox_check_constraints(session_factory):
    """Table has exactly the CHECK constraints declared on the ORM model.

    Expected names are derived from the ORM model's ``__table_args__`` and
    the active SQLAlchemy naming convention (``ck_<table>_<constraint>``),
    then truncated with the deterministic MD5 suffix that PostgreSQL applies
    to identifiers exceeding the 63-character limit.  The resulting set is
    compared exactly against the names reported by ``pg_constraint``.
    """
    PG_MAX_IDENTIFIER = 63

    def _pg_truncated(name: str) -> str:
        if len(name) <= PG_MAX_IDENTIFIER:
            return name
        digest = md5(name.encode(), usedforsecurity=False).hexdigest()
        return name[: PG_MAX_IDENTIFIER - 8] + "_" + digest[-4:]

    expected_names: set[str] = set()
    for item in WorldMemoryProjectionOutbox.__table_args__:
        if isinstance(item, CheckConstraint) and isinstance(item.name, str):
            # item.name already reflects the active naming convention (conv).
            expected_names.add(_pg_truncated(item.name))

    # Sanity: the model must declare the constraints we expect.
    assert len(expected_names) == 8, (
        f"ORM model should declare 8 CHECK constraints, got {len(expected_names)}"
    )

    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) "
                "FROM pg_constraint "
                "WHERE conrelid = 'world_memory_projection_outbox'::regclass "
                "AND contype = 'c'"
            )
        )
        db_constraints = {row[0]: row[1] for row in result.fetchall()}

    actual_names = set(db_constraints)
    missing = expected_names - actual_names
    extra = actual_names - expected_names
    assert actual_names == expected_names, (
        f"CHECK constraint mismatch — "
        f"missing={sorted(missing)}, extra={sorted(extra)}, "
        f"expected={sorted(expected_names)}, "
        f"actual={sorted(actual_names)}"
    )

    # Spot-check the definitions still mention the expected columns.
    all_defs = " ".join(db_constraints.values())
    assert "state" in all_defs and "pending" in all_defs
    assert "schema_version" in all_defs
    assert "attempt_count" in all_defs
    assert "max_attempts" in all_defs
    assert "event_type" in all_defs
    assert "projection_target" in all_defs
    assert "idempotency_key" in all_defs


async def test_projection_outbox_indexes(session_factory):
    """Table has required indexes including partial indexes for claiming."""
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'world_memory_projection_outbox'"
            )
        )
        indexes = {row[0] for row in result.fetchall()}

    assert "ix_wm_projection_outbox_claim" in indexes
    assert "ix_wm_projection_outbox_lease_recovery" in indexes
    assert "ix_wm_projection_outbox_authority" in indexes


async def test_projection_outbox_unique_constraint(session_factory):
    """Idempotency key has a unique constraint."""
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'world_memory_projection_outbox'::regclass "
                "AND contype = 'u'"
            )
        )
        constraints = {row[0] for row in result.fetchall()}

    assert "uq_wm_projection_outbox_idempotency_key" in constraints


async def test_projection_outbox_fk_to_authority(session_factory):
    """FK links to world_memory_envelopes.id."""
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT conname, confrelid::regclass::text "
                "FROM pg_constraint "
                "WHERE conrelid = 'world_memory_projection_outbox'::regclass "
                "AND contype = 'f'"
            )
        )
        fks = {row[0]: row[1] for row in result.fetchall()}

    assert "fk_wmproj_authority_envelope_id" in fks
    assert fks["fk_wmproj_authority_envelope_id"] == "world_memory_envelopes"


async def test_legacy_outbox_unchanged(session_factory):
    """Legacy memory_projection_outbox still exists and is unaffected."""
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname='public' AND tablename = 'memory_projection_outbox'"
            )
        )
        rows = result.fetchall()
        assert len(rows) == 1, "legacy memory_projection_outbox must still exist"

        # Verify its columns are unchanged
        col_result = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'memory_projection_outbox' "
                "ORDER BY ordinal_position"
            )
        )
        cols = [row[0] for row in col_result.fetchall()]
        # Legacy outbox has aggregate_id but NOT authority_envelope_id
        assert "aggregate_id" in cols
        assert "authority_envelope_id" not in cols
