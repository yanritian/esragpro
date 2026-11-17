"""P1-03 PostgreSQL schema tests: topology, columns, types, defaults."""

from __future__ import annotations

import ast
import os
import subprocess
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration._p1_03_schema_helpers import col_info, cols, pk

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_ALEMBIC = os.path.join(_BACKEND, ".venv", "Scripts", "alembic.exe")

# Exact ordered column contract — all 17, nothing more.
EXPECTED_COLUMNS: list[str] = [
    "id", "aggregate_id", "event_type", "schema_version",
    "projection_target", "idempotency_key", "payload",
    "state", "attempt_count", "max_attempts", "available_at",
    "lease_owner", "lease_expires_at", "processed_at",
    "last_error", "created_at", "updated_at",
]

# Exact per-column contract: udt_name, nullable, expected default substring
# (``None`` means the column must have no server default at all).
COLUMN_CONTRACT: tuple[tuple[str, str, str, str | None], ...] = (
    ("id", "uuid", "NO", None),
    ("aggregate_id", "uuid", "NO", None),
    ("event_type", "varchar", "NO", None),
    ("schema_version", "int4", "NO", "1"),
    ("projection_target", "varchar", "NO", None),
    ("idempotency_key", "varchar", "NO", None),
    ("payload", "jsonb", "NO", None),
    ("state", "varchar", "NO", "pending"),
    ("attempt_count", "int4", "NO", "0"),
    ("max_attempts", "int4", "NO", "8"),
    ("available_at", "timestamptz", "NO", "now()"),
    ("lease_owner", "varchar", "YES", None),
    ("lease_expires_at", "timestamptz", "YES", None),
    ("processed_at", "timestamptz", "YES", None),
    ("last_error", "text", "YES", None),
    ("created_at", "timestamptz", "NO", "now()"),
    ("updated_at", "timestamptz", "NO", "now()"),
)


def _url() -> str:
    u = os.environ.get("DATABASE_URL")
    if not u:
        pytest.skip("DATABASE_URL not set (run via run-p1-03-outbox-test.ps1)")
    return u


def _alembic(*args: str) -> str:
    r = subprocess.run([_ALEMBIC, *args], capture_output=True, text=True, cwd=_BACKEND)
    if r.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)}: {r.stderr}")
    return r.stdout


def _read_migration_assignment(name: str) -> str | None:
    """Return the literal string assigned to ``name`` in the migration file.

    Uses AST parsing so the migration module is never executed; this avoids
    the ``backend/alembic`` package shadowing the installed ``alembic``
    distribution when the test is imported from the backend pytest root.
    Handles both plain assignments (``x = "..."``) and annotated assignments
    (``x: str = "..."``) used by Alembic migration templates.
    """
    path = os.path.join(_BACKEND, "alembic", "versions",
                        "p1_03_memory_outbox.py")
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    for node in tree.body:
        target: ast.Name | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                continue
            if not isinstance(node.targets[0], ast.Name):
                continue
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            if not isinstance(node.target, ast.Name):
                continue
            if node.value is None:
                continue
            target = node.target
            value = node.value
        else:
            continue
        if target.id != name:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


async def _tables(e: Any) -> set[str]:
    async with e.connect() as c:
        return {r[0] for r in await c.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"))}


class TestAlembicTopology:
    def test_single_head(self) -> None:
        out = _alembic("heads")
        heads = [x.strip().split()[0] for x in out.splitlines() if x.strip()]
        assert heads == ["p9_d3_wm_occurred_at_utc"]

    def test_down_revision_chain(self) -> None:
        assert _read_migration_assignment("revision") == "p1_03_memory_outbox"
        assert _read_migration_assignment("down_revision") == "p1_01_memory_schema"


class TestOrmMetadata:
    def test_outbox_table_registered(self) -> None:
        from app.db.base import Base
        import app.db.models  # noqa: F401
        assert "memory_projection_outbox" in Base.metadata.tables


@pytest.mark.asyncio
class TestSchemaInspection:
    async def test_upgrade_creates_table(self) -> None:
        e = create_async_engine(_url())
        try:
            _alembic("upgrade", "head")
            assert "memory_projection_outbox" in await _tables(e)
        finally:
            await e.dispose()

    async def test_primary_key(self) -> None:
        e = create_async_engine(_url())
        try:
            assert await pk(e, "memory_projection_outbox") == ["id"]
        finally:
            await e.dispose()

    async def test_exact_columns_ordered(self) -> None:
        e = create_async_engine(_url())
        try:
            columns = await cols(e, "memory_projection_outbox")
            assert columns == EXPECTED_COLUMNS, (
                f"expected {EXPECTED_COLUMNS}, got {columns}"
            )
        finally:
            await e.dispose()

    async def test_exact_types_nullability_defaults(self) -> None:
        e = create_async_engine(_url())
        try:
            for name, udt, nullable, default in COLUMN_CONTRACT:
                info = await col_info(e, "memory_projection_outbox", name)
                assert info, f"column {name} missing"
                assert info["udt_name"] == udt, (
                    f"{name}: udt_name {info['udt_name']!r} != {udt!r}"
                )
                assert info["is_nullable"] == nullable, (
                    f"{name}: nullable {info['is_nullable']!r} != {nullable!r}"
                )
                if default is not None:
                    assert info["column_default"] is not None, (
                        f"{name}: expected default containing {default!r}"
                    )
                    assert default in info["column_default"], (
                        f"{name}: default {info['column_default']!r} "
                        f"missing {default!r}"
                    )
                else:
                    assert info["column_default"] is None, (
                        f"{name}: unexpected default "
                        f"{info['column_default']!r}"
                    )
        finally:
            await e.dispose()

    async def test_aggregate_id_has_no_foreign_key(self) -> None:
        from tests.integration._p1_03_schema_helpers import has_fk
        e = create_async_engine(_url())
        try:
            assert not await has_fk(e, "memory_projection_outbox",
                                    "aggregate_id")
        finally:
            await e.dispose()

    async def test_unique_idempotency_key(self) -> None:
        from tests.integration._p1_03_schema_helpers import unique_idxs
        e = create_async_engine(_url())
        try:
            uniq = await unique_idxs(e, "memory_projection_outbox")
            assert ["idempotency_key"] in uniq
        finally:
            await e.dispose()
