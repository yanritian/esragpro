"""Shared fixtures for P1-08 MinIO projection integration tests.

Provides isolated PostgreSQL and MinIO connections for testing
real worker projection, exact round-trip, duplicate/conflict handling,
outage/recovery, persistence restart, and composition preflight.

No ACL/permission/tenant fields anywhere.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from app.db.migration_config import validate_migration_test_database_url


def _database_url() -> Optional[str]:
    if os.environ.get("ESRAGPRO_MIGRATION_TEST_MODE") != "1":
        return None
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        return None
    return validate_migration_test_database_url(raw)


def _minio_endpoint() -> Optional[str]:
    if os.environ.get("ESRAGPRO_MIGRATION_TEST_MODE") != "1":
        return None
    return os.environ.get("MINIO_ENDPOINT")


def _minio_access_key() -> str:
    return os.environ.get("MINIO_ACCESS_KEY", "minioadmin")


def _minio_secret_key() -> str:
    return os.environ.get("MINIO_SECRET_KEY", "minioadmin")


@pytest.fixture
async def session_factory():
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL not set (run via run-p1-08-minio-test.ps1)")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
async def db_engine():
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL not set")
    engine = create_async_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def minio_endpoint():
    endpoint = _minio_endpoint()
    if not endpoint:
        pytest.skip("MINIO_ENDPOINT not set (run via run-p1-08-minio-test.ps1)")
    return endpoint


@pytest.fixture
def minio_access_key() -> str:
    return _minio_access_key()


@pytest.fixture
def minio_secret_key() -> str:
    return _minio_secret_key()


@pytest.fixture(autouse=True)
async def _isolate_tables(session_factory):
    """Truncate memory tables before and after each test for isolation."""
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


@pytest.fixture
def docker_minio():
    """Provide real Docker stop/start for the MinIO container.

    Reads P1_08_DOCKER_EXE, P1_08_COMPOSE_FILE, and P1_08_COMPOSE_PROJECT
    from the environment (set by run-p1-08-minio-test.ps1).
    Skips when Docker control is not available.

    Teardown fails loudly if MinIO cannot be restored: the captured
    command output is included in the pytest failure message so a
    broken fixture cannot silently poison subsequent tests.
    """
    docker_exe = os.environ.get("P1_08_DOCKER_EXE")
    compose_file = os.environ.get("P1_08_COMPOSE_FILE")
    compose_project = os.environ.get("P1_08_COMPOSE_PROJECT")
    minio_ep = os.environ.get("MINIO_ENDPOINT")
    if not all([docker_exe, compose_file, compose_project, minio_ep]):
        pytest.skip("Docker control env not set (run via test runner script)")
    yield {
        "docker_exe": docker_exe,
        "compose_file": compose_file,
        "compose_project": compose_project,
        "minio_endpoint": minio_ep,
    }
    # Always ensure MinIO is running after the test (cleanup guarantee).
    restart_result = subprocess.run(
        [
            docker_exe,
            "compose",
            "-p",
            compose_project,
            "-f",
            compose_file,
            "up",
            "-d",
            "--wait",
            "minio",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if restart_result.returncode != 0:
        pytest.fail(
            "docker_minio fixture teardown: failed to restore MinIO container. "
            f"returncode={restart_result.returncode} "
            f"stdout={restart_result.stdout!r} stderr={restart_result.stderr!r}"
        )
    try:
        _wait_for_minio_healthy(minio_ep, timeout_s=60)
    except TimeoutError as exc:
        pytest.fail(
            "docker_minio fixture teardown: MinIO container reported running "
            f"but API port never became healthy: {exc}"
        )


def _wait_for_minio_healthy(
    endpoint: str,
    timeout_s: float = 60.0,
) -> None:
    """Block until MinIO API port is accepting connections, or timeout."""
    # Parse host:port from endpoint like "127.0.0.1:9000"
    if ":" in endpoint:
        host, port_str = endpoint.rsplit(":", 1)
        port = int(port_str)
    else:
        host = endpoint
        port = 9000

    deadline = time.monotonic() + timeout_s
    last_error: Optional[str] = None

    while time.monotonic() < deadline:
        try:
            sock = socket.create_connection((host, port), timeout=3)
            sock.close()
            return
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    raise TimeoutError(
        f"MinIO at {endpoint} did not become healthy within {timeout_s}s (last error: {last_error})"
    )
