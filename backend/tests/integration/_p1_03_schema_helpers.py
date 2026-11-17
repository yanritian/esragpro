"""Schema-inspection helpers shared across P1-03 schema test modules."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text


async def cols(engine, table: str) -> list[str]:
    async with engine.connect() as c:
        return [r[0] for r in await c.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"
        ), {"t": table})]


async def col_info(engine, table: str, column: str) -> dict[str, Any]:
    async with engine.connect() as c:
        r = (await c.execute(text(
            "SELECT data_type, is_nullable, column_default, udt_name "
            "FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
        ), {"t": table, "c": column})).first()
        if r is None:
            return {}
        return {"data_type": r[0], "is_nullable": r[1],
                "column_default": r[2], "udt_name": r[3]}


async def pk(engine, table: str) -> list[str]:
    async with engine.connect() as c:
        return [r[0] for r in await c.execute(text(
            "SELECT a.attname FROM pg_index ix "
            "JOIN pg_class t ON t.oid=ix.indrelid "
            "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(ix.indkey) "
            "WHERE t.relname=:t AND ix.indisprimary "
            "ORDER BY array_position(ix.indkey, a.attnum)"
        ), {"t": table})]


async def unique_idxs(engine, table: str) -> list[list[str]]:
    async with engine.connect() as c:
        rows = await c.execute(text(
            "SELECT i.relname, a.attname FROM pg_index ix "
            "JOIN pg_class t ON t.oid=ix.indrelid "
            "JOIN pg_class i ON i.oid=ix.indexrelid "
            "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(ix.indkey) "
            "WHERE t.relname=:t AND ix.indisunique AND NOT ix.indisprimary "
            "ORDER BY i.relname, array_position(ix.indkey, a.attnum)"
        ), {"t": table})
        g: dict[str, list[str]] = {}
        for n, col in rows:
            g.setdefault(n, []).append(col)
        return list(g.values())


async def indexes(engine, table: str) -> dict[str, list[str]]:
    async with engine.connect() as c:
        rows = await c.execute(text(
            "SELECT i.relname, a.attname FROM pg_index ix "
            "JOIN pg_class t ON t.oid=ix.indrelid "
            "JOIN pg_class i ON i.oid=ix.indexrelid "
            "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(ix.indkey) "
            "WHERE t.relname=:t ORDER BY i.relname, array_position(ix.indkey, a.attnum)"
        ), {"t": table})
        r: dict[str, list[str]] = {}
        for n, col in rows:
            r.setdefault(n, []).append(col)
        return r


async def checks(engine, table: str) -> dict[str, str]:
    async with engine.connect() as c:
        rows = await c.execute(text(
            "SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid=c.conrelid WHERE t.relname=:t AND c.contype='c'"
        ), {"t": table})
        return {r[0]: r[1] for r in rows}


async def has_fk(engine, table: str, column: str) -> bool:
    async with engine.connect() as c:
        rows = await c.execute(text(
            "SELECT COUNT(*) FROM information_schema.key_column_usage kcu "
            "WHERE kcu.table_schema='public' AND kcu.table_name=:t "
            "AND kcu.column_name=:c"
        ), {"t": table, "c": column})
        return int(rows.scalar_one()) > 0
