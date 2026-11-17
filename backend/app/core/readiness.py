"""Bounded, fail-closed probes for application readiness.

PostgreSQL, Elasticsearch, and Redis are the application-owned baseline. Neo4j
and MinIO are checked only when an initialized app-state probe is supplied;
the worker-only MinIO process is never inferred to be an application client.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol, TypedDict, runtime_checkable

import anyio
import asyncpg
from anyio import to_thread
from redis.asyncio import Redis

DEFAULT_READINESS_TIMEOUT_S: Final[float] = 2.0
_ALEMBIC_CONFIG_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "alembic.ini"
_ALEMBIC_SCRIPT_PATH: Final[Path] = _ALEMBIC_CONFIG_PATH.parent / "alembic"
_READINESS_ERRORS: Final = (Exception,)

ReadinessStatus = Literal["ready", "not_ready", "skipped"]


class ComponentPayload(TypedDict):
    status: ReadinessStatus
    reason: str | None


class ReadinessPayload(TypedDict):
    status: Literal["ready", "not_ready"]
    components: dict[str, ComponentPayload]


@runtime_checkable
class ReadinessProbe(Protocol):
    def __call__(self) -> Awaitable[None]: ...


class DatabaseConnection(Protocol):
    async def execute(self, query: str) -> str: ...

    async def fetch(self, query: str) -> Sequence[Mapping[str, str]]: ...


class DatabasePool(Protocol):
    def acquire(self) -> AbstractAsyncContextManager[DatabaseConnection]: ...


DatabasePoolFactory = Callable[[], Awaitable[DatabasePool | asyncpg.Pool]]
MigrationHeadsProvider = Callable[[], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class ReadinessDependencies:
    database_pool_factory: DatabasePoolFactory
    elasticsearch_probe: ReadinessProbe
    redis_probe: ReadinessProbe
    migration_heads_provider: MigrationHeadsProvider
    neo4j_enabled: bool = False
    neo4j_probe: ReadinessProbe | None = None
    minio_enabled: bool = False
    minio_probe: ReadinessProbe | None = None
    timeout_s: float = DEFAULT_READINESS_TIMEOUT_S
    embedding_model_probe: ReadinessProbe | None = None


@dataclass(frozen=True, slots=True)
class ComponentReadiness:
    status: ReadinessStatus
    reason: str | None = None

    @property
    def ready(self) -> bool:
        return self.status != "not_ready"


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    components: Mapping[str, ComponentReadiness]

    @property
    def ready(self) -> bool:
        return all(component.ready for component in self.components.values())

    def to_payload(self) -> ReadinessPayload:
        payload_components: dict[str, ComponentPayload] = {}
        for name, component in self.components.items():
            payload_components[name] = {
                "status": component.status,
                "reason": component.reason,
            }
        return {
            "status": "ready" if self.ready else "not_ready",
            "components": payload_components,
        }


async def _run_probe(probe: ReadinessProbe, timeout_s: float) -> bool:
    try:
        with anyio.fail_after(timeout_s):
            await probe()
    except _READINESS_ERRORS:
        return False
    return True


async def _check_probe(
    name: str,
    probe: ReadinessProbe,
    timeout_s: float,
) -> ComponentReadiness:
    if await _run_probe(probe, timeout_s):
        return ComponentReadiness("ready")
    return ComponentReadiness("not_ready", f"{name}_unavailable")


async def _check_optional_probe(
    name: str,
    enabled: bool,
    probe: ReadinessProbe | None,
    timeout_s: float,
) -> ComponentReadiness:
    if not enabled:
        return ComponentReadiness("skipped", "not_enabled")
    if probe is None:
        return ComponentReadiness("not_ready", f"{name}_probe_unavailable")
    return await _check_probe(name, probe, timeout_s)


async def _check_required_probe(
    name: str,
    probe: ReadinessProbe | None,
    timeout_s: float,
) -> ComponentReadiness:
    if probe is None:
        return ComponentReadiness("not_ready", f"{name}_unavailable")
    return await _check_probe(name, probe, timeout_s)


async def _check_database(
    dependencies: ReadinessDependencies,
) -> tuple[ComponentReadiness, ComponentReadiness]:
    try:
        with anyio.fail_after(dependencies.timeout_s):
            pool = await dependencies.database_pool_factory()
            async with pool.acquire() as connection:
                await connection.execute("SELECT 1")
                rows = await connection.fetch("SELECT version_num FROM alembic_version")
    except _READINESS_ERRORS:
        return (
            ComponentReadiness("not_ready", "postgres_unavailable"),
            ComponentReadiness("not_ready", "migration_database_unavailable"),
        )

    try:
        applied_heads: list[str] = []
        for row in rows:
            version = row.get("version_num")
            if not isinstance(version, str) or not version.strip():
                return (
                    ComponentReadiness("ready"),
                    ComponentReadiness("not_ready", "migration_state_invalid"),
                )
            applied_heads.append(version.strip())
    except _READINESS_ERRORS:
        return (
            ComponentReadiness("ready"),
            ComponentReadiness("not_ready", "migration_state_invalid"),
        )

    try:
        with anyio.fail_after(dependencies.timeout_s):
            expected_heads = await to_thread.run_sync(
                dependencies.migration_heads_provider,
                abandon_on_cancel=True,
            )
    except _READINESS_ERRORS:
        return (
            ComponentReadiness("ready"),
            ComponentReadiness("not_ready", "migration_heads_unavailable"),
        )

    if not expected_heads:
        return (
            ComponentReadiness("ready"),
            ComponentReadiness("not_ready", "migration_heads_unavailable"),
        )
    try:
        heads_match = tuple(sorted(applied_heads)) == tuple(sorted(expected_heads))
    except _READINESS_ERRORS:
        return (
            ComponentReadiness("ready"),
            ComponentReadiness("not_ready", "migration_heads_unavailable"),
        )
    if not heads_match:
        return (
            ComponentReadiness("ready"),
            ComponentReadiness("not_ready", "migration_head_mismatch"),
        )
    return ComponentReadiness("ready"), ComponentReadiness("ready")


@contextmanager
def _external_alembic_imports() -> Iterator[None]:
    original_path = sys.path[:]
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "alembic" or name.startswith("alembic.")
    }
    for name in saved_modules:
        sys.modules.pop(name, None)
    backend_root = _ALEMBIC_CONFIG_PATH.parent
    sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != backend_root]
    try:
        yield
    finally:
        for name in tuple(sys.modules):
            if name == "alembic" or name.startswith("alembic."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.path[:] = original_path


def get_migration_heads() -> tuple[str, ...]:
    with _external_alembic_imports():
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config(str(_ALEMBIC_CONFIG_PATH))
        config.set_main_option("script_location", str(_ALEMBIC_SCRIPT_PATH))
        script_directory = ScriptDirectory.from_config(config)
        return tuple(sorted(script_directory.get_heads()))


def create_redis_probe(
    redis_url: str,
    timeout_s: float = DEFAULT_READINESS_TIMEOUT_S,
) -> ReadinessProbe:
    async def probe() -> None:
        client = Redis.from_url(
            redis_url,
            decode_responses=False,
            socket_connect_timeout=timeout_s,
            socket_timeout=timeout_s,
        )
        try:
            await client.ping()
        finally:
            await client.aclose()

    return probe


async def assess_application_readiness(
    dependencies: ReadinessDependencies,
) -> ReadinessReport:
    postgres, migrations = await _check_database(dependencies)
    components: dict[str, ComponentReadiness] = {
        "postgres": postgres,
        "migrations": migrations,
        "embedding_model": await _check_required_probe(
            "embedding_model",
            dependencies.embedding_model_probe,
            dependencies.timeout_s,
        ),
        "elasticsearch": await _check_probe(
            "elasticsearch",
            dependencies.elasticsearch_probe,
            dependencies.timeout_s,
        ),
        "redis": await _check_probe(
            "redis",
            dependencies.redis_probe,
            dependencies.timeout_s,
        ),
        "neo4j": await _check_optional_probe(
            "neo4j",
            dependencies.neo4j_enabled,
            dependencies.neo4j_probe,
            dependencies.timeout_s,
        ),
        "minio": await _check_optional_probe(
            "minio",
            dependencies.minio_enabled,
            dependencies.minio_probe,
            dependencies.timeout_s,
        ),
    }
    return ReadinessReport(components)


__all__ = [
    "ComponentPayload",
    "ComponentReadiness",
    "DatabaseConnection",
    "DatabasePool",
    "DatabasePoolFactory",
    "MigrationHeadsProvider",
    "ReadinessDependencies",
    "ReadinessPayload",
    "ReadinessProbe",
    "ReadinessReport",
    "ReadinessStatus",
    "assess_application_readiness",
    "create_redis_probe",
    "get_migration_heads",
]
