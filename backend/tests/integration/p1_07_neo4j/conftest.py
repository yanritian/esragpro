"""Shared fixtures for P1-07 Neo4j projection integration tests.

Provides isolated PostgreSQL and Neo4j connections for testing
real worker projection, graph query, duplicate/conflict handling,
outage/recovery, persistence restart, and composition preflight.

No ACL/permission/tenant fields anywhere.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Optional

import pytest
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


def _neo4j_uri() -> Optional[str]:
    if os.environ.get("ESRAGPRO_MIGRATION_TEST_MODE") != "1":
        return None
    return os.environ.get("NEO4J_URI")


def _neo4j_user() -> Optional[str]:
    return os.environ.get("NEO4J_USER", "neo4j")


def _neo4j_password() -> Optional[str]:
    return os.environ.get("NEO4J_PASSWORD", "esragpro_p1_07_test")


@pytest.fixture
async def session_factory():
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL not set (run via run-p1-07-neo4j-test.ps1)")
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
def neo4j_uri():
    uri = _neo4j_uri()
    if not uri:
        pytest.skip("NEO4J_URI not set")
    return uri


@pytest.fixture
def neo4j_user():
    return _neo4j_user()


@pytest.fixture
def neo4j_password():
    return _neo4j_password()


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


@pytest.fixture
def docker_neo4j():
    """Provide real Docker stop/start for the Neo4j container.

    Reads P1_07_DOCKER_EXE, P1_07_COMPOSE_FILE, and P1_07_COMPOSE_PROJECT
    from the environment (set by run-p1-07-neo4j-test.ps1).
    Skips when Docker control is not available.

    Teardown fails loudly if Neo4j cannot be restored: the captured
    command output is included in the pytest failure message so a
    broken fixture cannot silently poison subsequent tests.
    """
    docker_exe = os.environ.get("P1_07_DOCKER_EXE")
    compose_file = os.environ.get("P1_07_COMPOSE_FILE")
    compose_project = os.environ.get("P1_07_COMPOSE_PROJECT")
    neo4j_bolt = os.environ.get("NEO4J_URI")
    if not all([docker_exe, compose_file, compose_project, neo4j_bolt]):
        pytest.skip("Docker control env not set (run via test runner script)")
    yield {
        "docker_exe": docker_exe,
        "compose_file": compose_file,
        "compose_project": compose_project,
        "neo4j_bolt": neo4j_bolt,
    }
    # Always ensure Neo4j is running after the test (cleanup guarantee).
    # If the restart fails, surface it as a pytest failure rather than
    # hiding it -- a dead Neo4j fixture poisons every subsequent test
    # in the session with misleading connection errors.
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
            "neo4j",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if restart_result.returncode != 0:
        pytest.fail(
            "docker_neo4j fixture teardown: failed to restore Neo4j container. "
            f"returncode={restart_result.returncode} "
            f"stdout={restart_result.stdout!r} stderr={restart_result.stderr!r}"
        )
    try:
        _wait_for_neo4j_healthy(neo4j_bolt, timeout_s=60)
    except TimeoutError as exc:
        pytest.fail(
            "docker_neo4j fixture teardown: Neo4j container reported running "
            f"but Bolt port never became healthy: {exc}"
        )


def _wait_for_neo4j_healthy(
    bolt_uri: str,
    timeout_s: float = 60.0,
) -> None:
    """Block until Neo4j Bolt port is accepting connections, or timeout."""
    import socket

    # Parse bolt://host:port
    if bolt_uri.startswith("bolt://"):
        host_port = bolt_uri[7:]
    elif bolt_uri.startswith("bolt+s://"):
        host_port = bolt_uri[9:]
    else:
        host_port = bolt_uri

    if ":" in host_port:
        host, port_str = host_port.rsplit(":", 1)
        port = int(port_str)
    else:
        host = host_port
        port = 7687

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
        f"Neo4j at {bolt_uri} did not become healthy within {timeout_s}s (last error: {last_error})"
    )
