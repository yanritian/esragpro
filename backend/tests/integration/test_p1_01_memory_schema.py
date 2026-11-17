"""P1-01 PostgreSQL memory schema integration tests."""
from __future__ import annotations

import importlib.util
import os
import subprocess
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_P1 = frozenset({"memory_items", "evidence_refs", "context_packs", "audit_log"})
_FORBIDDEN = (
    "tenant", "principal", "owner_subject", "user", "role",
    "permission", "acl", "scope", "oauth", "authorization",
)
_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_ALEMBIC = os.path.join(_BACKEND, ".venv", "Scripts", "alembic.exe")


def _url() -> str:
    u = os.environ.get("DATABASE_URL")
    if not u:
        pytest.skip("DATABASE_URL not set (run via run-p1-01-migration-test.ps1)")
    return u


def _alembic(*args: str) -> str:
    r = subprocess.run([_ALEMBIC, *args], capture_output=True, text=True, cwd=_BACKEND)
    if r.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)}: {r.stderr}")
    return r.stdout


async def _tables(e: Any) -> set[str]:
    async with e.connect() as c:
        return {r[0] for r in await c.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"))}

async def _cols(e: Any, t: str) -> list[str]:
    async with e.connect() as c:
        return [r[0] for r in await c.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"
        ), {"t": t})]

async def _fk_cascade(e: Any, t: str, col: str) -> bool:
    async with e.connect() as c:
        rows = await c.execute(text(
            "SELECT rc.delete_rule FROM information_schema.referential_constraints rc "
            "JOIN information_schema.key_column_usage kcu "
            "ON rc.constraint_name=kcu.constraint_name "
            "AND rc.constraint_schema=kcu.constraint_schema "
            "WHERE kcu.table_name=:t AND kcu.column_name=:c"
        ), {"t": t, "c": col})
        return any(r[0] == "CASCADE" for r in rows)

async def _indexes(e: Any, t: str) -> dict[str, list[str]]:
    async with e.connect() as c:
        rows = await c.execute(text(
            "SELECT i.relname, a.attname FROM pg_index ix "
            "JOIN pg_class t ON t.oid=ix.indrelid "
            "JOIN pg_class i ON i.oid=ix.indexrelid "
            "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(ix.indkey) "
            "WHERE t.relname=:t ORDER BY i.relname, array_position(ix.indkey, a.attnum)"
        ), {"t": t})
        r: dict[str, list[str]] = {}
        for n, col in rows:
            r.setdefault(n, []).append(col)
        return r

async def _checks(e: Any, t: str) -> dict[str, str]:
    async with e.connect() as c:
        rows = await c.execute(text(
            "SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid=c.conrelid WHERE t.relname=:t AND c.contype='c'"
        ), {"t": t})
        return {r[0]: r[1] for r in rows}

async def _pk(e: Any, t: str) -> list[str]:
    async with e.connect() as c:
        return [r[0] for r in await c.execute(text(
            "SELECT a.attname FROM pg_index ix "
            "JOIN pg_class t ON t.oid=ix.indrelid "
            "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(ix.indkey) "
            "WHERE t.relname=:t AND ix.indisprimary "
            "ORDER BY array_position(ix.indkey, a.attnum)"
        ), {"t": t})]

async def _unique_idxs(e: Any, t: str) -> list[list[str]]:
    async with e.connect() as c:
        rows = await c.execute(text(
            "SELECT i.relname, a.attname FROM pg_index ix "
            "JOIN pg_class t ON t.oid=ix.indrelid "
            "JOIN pg_class i ON i.oid=ix.indexrelid "
            "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(ix.indkey) "
            "WHERE t.relname=:t AND ix.indisunique AND NOT ix.indisprimary "
            "ORDER BY i.relname, array_position(ix.indkey, a.attnum)"
        ), {"t": t})
        g: dict[str, list[str]] = {}
        for n, col in rows:
            g.setdefault(n, []).append(col)
        return list(g.values())

async def _col_info(e: Any, t: str, col: str) -> dict[str, Any]:
    async with e.connect() as c:
        r = (await c.execute(text(
            "SELECT data_type, is_nullable, column_default, udt_name "
            "FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
        ), {"t": t, "c": col})).first()
        if r is None:
            return {}
        return {"data_type": r[0], "is_nullable": r[1],
                "column_default": r[2], "udt_name": r[3]}

class TestAlembicTopology:
    def test_single_head(self) -> None:
        out = _alembic("heads")
        heads = [x.strip().split()[0] for x in out.splitlines() if x.strip()]
        # World-memory chain extends p1_03 (p6_a, p6_b, p6_g1, p9_d3).
        assert heads == ["p9_d3_wm_occurred_at_utc"]

    def test_merge_parents(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "m", os.path.join(_BACKEND, "alembic", "versions", "mem0_000_merge_heads.py"))
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert set(mod.down_revision) == {"wp4_001", "wp10_003_midterm_memory"}

class TestMetadata:
    def test_four_tables(self) -> None:
        from app.db.base import Base
        import app.db.models  # noqa: F401
        for t in _P1:
            assert t in Base.metadata.tables, f"{t!r} missing from Base.metadata"

@pytest.mark.asyncio
class TestSchemaInspection:
    async def test_upgrade_creates_tables(self) -> None:
        e = create_async_engine(_url())
        try:
            _alembic("upgrade", "head")
            tables = await _tables(e)
            for t in _P1:
                assert t in tables
        finally:
            await e.dispose()

    async def test_memory_items_columns_types_pk(self) -> None:
        e = create_async_engine(_url())
        try:
            cols = await _cols(e, "memory_items")
            for c in ["id", "content", "status", "provenance", "project_id",
                       "session_id", "agent_id", "knowledge_base_id", "expires_at",
                       "metadata", "created_at", "updated_at"]:
                assert c in cols, f"{c} missing"
            assert await _pk(e, "memory_items") == ["id"]
            md = await _col_info(e, "memory_items", "metadata")
            assert md["udt_name"] == "jsonb" and md["is_nullable"] == "NO"
            assert md["column_default"] and "jsonb" in md["column_default"].lower()
            prov = await _col_info(e, "memory_items", "provenance")
            assert prov["udt_name"] == "jsonb" and prov["is_nullable"] == "YES"
            st = await _col_info(e, "memory_items", "status")
            assert st["is_nullable"] == "NO"
        finally:
            await e.dispose()

    async def test_memory_items_checks(self) -> None:
        e = create_async_engine(_url())
        try:
            ck = await _checks(e, "memory_items")
            assert any("status" in v for v in ck.values())
            assert any("content" in v for v in ck.values())
        finally:
            await e.dispose()

    async def test_evidence_refs(self) -> None:
        e = create_async_engine(_url())
        try:
            assert await _fk_cascade(e, "evidence_refs", "memory_item_id")
            assert await _pk(e, "evidence_refs") == ["id"]
            assert ["memory_item_id", "position"] in await _unique_idxs(e, "evidence_refs")
            ck = await _checks(e, "evidence_refs")
            assert any("confidence" in v for v in ck.values())
            assert any("position" in v for v in ck.values())
            md = await _col_info(e, "evidence_refs", "metadata")
            assert md["udt_name"] == "jsonb" and md["is_nullable"] == "NO"
            assert md["column_default"] and "jsonb" in md["column_default"].lower()
        finally:
            await e.dispose()

    async def test_context_packs(self) -> None:
        e = create_async_engine(_url())
        try:
            assert await _pk(e, "context_packs") == ["id"]
            ck = await _checks(e, "context_packs")
            assert any("token_used" in v and "token_budget" in v for v in ck.values())
            mi = await _col_info(e, "context_packs", "memory_items")
            assert mi["udt_name"] == "jsonb" and mi["is_nullable"] == "NO"
            assert mi["column_default"] and "jsonb" in mi["column_default"].lower()
        finally:
            await e.dispose()

    async def test_audit_log(self) -> None:
        e = create_async_engine(_url())
        try:
            assert await _pk(e, "audit_log") == ["id"]
            d = await _col_info(e, "audit_log", "details")
            assert d["udt_name"] == "jsonb" and d["is_nullable"] == "NO"
            assert d["column_default"] and "jsonb" in d["column_default"].lower()
            assert (await _col_info(e, "audit_log", "created_at"))["is_nullable"] == "NO"
        finally:
            await e.dispose()

    async def test_indexes(self) -> None:
        e = create_async_engine(_url())
        try:
            mi = await _indexes(e, "memory_items")
            assert any("expires_at" in c for c in mi.values())
            assert any("status" in c and "updated_at" in c for c in mi.values())
            er = await _indexes(e, "evidence_refs")
            assert any("source_id" in c for c in er.values())
            assert any("source_type" in c and "source_id" in c and "chunk_id" in c
                       for c in er.values()), "composite source lookup missing"
            al = await _indexes(e, "audit_log")
            assert any("created_at" in c for c in al.values())
            assert any("entity_type" in c and "entity_id" in c for c in al.values())
            assert any("request_id" in c for c in al.values())
        finally:
            await e.dispose()

    async def test_no_forbidden_columns(self) -> None:
        e = create_async_engine(_url())
        try:
            for t in _P1:
                for col in await _cols(e, t):
                    low = col.lower()
                    for f in _FORBIDDEN:
                        assert f not in low, f"forbidden {f!r} in {t}.{col}"
        finally:
            await e.dispose()


# -- Downgrade / Re-upgrade --------------------------------------------------

@pytest.mark.asyncio
class TestDowngradeReUpgrade:
    async def test_cycle(self) -> None:
        e = create_async_engine(_url())
        re_upgrade_failed: BaseException | None = None
        try:
            _alembic("upgrade", "head")
            for t in _P1:
                assert t in await _tables(e)
            _alembic("downgrade", "mem0_000_merge_heads")
            after = await _tables(e)
            for t in _P1:
                assert t not in after, f"{t} present after downgrade"
            for t in ("tenants", "users", "knowledge_bases"):
                assert t in after, f"legacy {t!r} lost"
        finally:
            try:
                _alembic("upgrade", "head")
                for t in _P1:
                    assert t in await _tables(e), f"{t} missing after re-upgrade"
            except BaseException as exc:  # noqa: BLE001 - surface re-upgrade failure
                re_upgrade_failed = exc
            await e.dispose()
        if re_upgrade_failed is not None:
            raise re_upgrade_failed


# -- URL safety ---------------------------------------------------------------

class TestUrlSafety:
    @pytest.mark.parametrize("url", [
        "postgresql://u:p@127.0.0.1:5432/db_test",
        "postgresql+psycopg2://u:p@127.0.0.1/db_test",
        "postgresql+asyncpg://u:p@10.0.0.1:5432/db_test",
        "postgresql+asyncpg://u:p@127.0.0.1:5432/production_db",
        "postgresql+asyncpg://u:p@prod-host:5432/db_test",
        "postgresql+asyncpg://u:p@127.0.0.1:5432/db_test?sslmode=disable",
        "",
    ])
    def test_rejects(self, url: str) -> None:
        from app.db.migration_config import validate_migration_test_database_url
        with pytest.raises(RuntimeError):
            validate_migration_test_database_url(url)

    def test_no_credential_leak(self) -> None:
        from app.db.migration_config import validate_migration_test_database_url
        with pytest.raises(RuntimeError) as ei:
            validate_migration_test_database_url(
                "postgresql+asyncpg://secret:s3cret@10.0.0.1/db_test")
        assert "secret" not in str(ei.value) and "s3cret" not in str(ei.value)

    def test_accepts_valid(self) -> None:
        from app.db.migration_config import validate_migration_test_database_url
        u = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/esragpro_p1_01_test"
        assert validate_migration_test_database_url(u) == u
