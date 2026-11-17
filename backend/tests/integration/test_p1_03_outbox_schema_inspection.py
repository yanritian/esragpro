"""P1-03 PostgreSQL schema tests: indexes, checks, forbidden keys, cycle."""

from __future__ import annotations

import os
import subprocess

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration._p1_03_schema_helpers import (
    checks,
    cols,
    indexes,
)

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_ALEMBIC = os.path.join(_BACKEND, ".venv", "Scripts", "alembic.exe")

# Forbidden name fragments — any column whose lowercase name contains one
# of these strings is rejected, except for columns in ``_EXEMPT_COLUMNS``.
_FORBIDDEN = (
    "tenant", "principal", "owner_subject", "user", "role",
    "permission", "acl", "scope", "oauth", "authorization",
    "auth", "owner",
)
_EXEMPT_COLUMNS = frozenset({"lease_owner"})


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


async def _tables(e) -> set[str]:
    from sqlalchemy import text
    async with e.connect() as c:
        return {r[0] for r in await c.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"))}


# Exact index contract: name -> ordered column list.
_EXPECTED_INDEXES: dict[str, list[str]] = {
    "ix_memory_projection_outbox_claim": [
        "projection_target", "state", "available_at", "created_at",
    ],
    "ix_memory_projection_outbox_lease_recovery": [
        "state", "lease_expires_at",
    ],
    "ix_memory_projection_outbox_aggregate_created": [
        "aggregate_id", "created_at",
    ],
}


@pytest.mark.asyncio
class TestIndexesAndChecks:
    async def test_exact_indexes(self) -> None:
        e = create_async_engine(_url())
        try:
            idx = await indexes(e, "memory_projection_outbox")
            for name, expected_cols in _EXPECTED_INDEXES.items():
                assert name in idx, f"index {name!r} missing"
                assert idx[name] == expected_cols, (
                    f"index {name!r}: expected {expected_cols}, got {idx[name]}"
                )
        finally:
            await e.dispose()

    async def test_check_constraints(self) -> None:
        e = create_async_engine(_url())
        try:
            ck = await checks(e, "memory_projection_outbox")
            joined = " || ".join(ck.values())
            assert "state" in joined and "pending" in joined
            assert "schema_version" in joined
            assert "attempt_count" in joined
            assert "max_attempts" in joined
            assert "event_type" in joined
            assert "projection_target" in joined
            assert "idempotency_key" in joined
        finally:
            await e.dispose()

    async def test_no_forbidden_columns(self) -> None:
        e = create_async_engine(_url())
        try:
            columns = await cols(e, "memory_projection_outbox")
            for col in columns:
                if col in _EXEMPT_COLUMNS:
                    continue
                low = col.lower()
                for f in _FORBIDDEN:
                    assert f not in low, f"forbidden {f!r} in {col}"
        finally:
            await e.dispose()


@pytest.mark.asyncio
class TestDowngradeReUpgrade:
    async def test_cycle(self) -> None:
        e = create_async_engine(_url())
        re_upgrade_failed: BaseException | None = None
        try:
            _alembic("upgrade", "head")
            assert "memory_projection_outbox" in await _tables(e)
            _alembic("downgrade", "p1_01_memory_schema")
            after = await _tables(e)
            assert "memory_projection_outbox" not in after
            for kept in ("memory_items", "evidence_refs",
                         "context_packs", "audit_log"):
                assert kept in after, f"{kept} lost"
        finally:
            try:
                _alembic("upgrade", "head")
                assert "memory_projection_outbox" in await _tables(e)
            except BaseException as exc:  # noqa: BLE001
                re_upgrade_failed = exc
            await e.dispose()
        if re_upgrade_failed is not None:
            raise re_upgrade_failed
