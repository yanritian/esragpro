"""Shared fixtures for P1-05 Redis hot-state integration tests."""

from __future__ import annotations

import os
from typing import Optional

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.migration_config import validate_migration_test_database_url


def _database_url() -> Optional[str]:
    if os.environ.get("ESRAGPRO_MIGRATION_TEST_MODE") != "1":
        return None
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        return None
    return validate_migration_test_database_url(raw)


def _redis_url() -> Optional[str]:
    if os.environ.get("ESRAGPRO_MIGRATION_TEST_MODE") != "1":
        return None
    raw = os.environ.get("REDIS_URL")
    return raw


def _auth_redis_url() -> Optional[str]:
    if os.environ.get("ESRAGPRO_MIGRATION_TEST_MODE") != "1":
        return None
    raw = os.environ.get("AUTH_REDIS_URL")
    return raw


@pytest.fixture
async def session_factory():
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL not set (run via run-p1-05-redis-hot-state-test.ps1)")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
async def redis_client():
    url = _redis_url()
    if not url:
        pytest.skip("REDIS_URL not set")
    client = Redis.from_url(url, decode_responses=False)
    try:
        await client.ping()
        await client.flushdb()
        yield client
    finally:
        try:
            await client.flushdb()
        finally:
            await client.aclose()


@pytest.fixture
async def auth_redis_url():
    url = _auth_redis_url()
    if not url:
        pytest.skip("AUTH_REDIS_URL not set")
    return url


@pytest.fixture(autouse=True)
async def _isolate_tables(session_factory):
    async with session_factory() as session:
        await session.execute(
            text("TRUNCATE memory_projection_outbox, evidence_refs, memory_items")
        )
        await session.commit()
    yield
    async with session_factory() as session:
        await session.execute(
            text("TRUNCATE memory_projection_outbox, evidence_refs, memory_items")
        )
        await session.commit()
