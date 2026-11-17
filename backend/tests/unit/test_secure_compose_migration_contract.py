from pathlib import Path

import yaml

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_PATH = _REPOSITORY_ROOT / "docker-compose.secure.yml"


def _compose() -> dict[str, object]:
    payload = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _service(compose: dict[str, object], name: str) -> dict[str, object]:
    services = compose["services"]
    assert isinstance(services, dict)
    service = services[name]
    assert isinstance(service, dict)
    return service


def test_secure_compose_runs_migration_before_backend_startup() -> None:
    compose = _compose()
    backend = _service(compose, "backend")
    migration = _service(compose, "migration")

    assert migration["build"] == backend["build"]
    assert migration["command"] == ["alembic", "upgrade", "head"]
    assert migration["restart"] == "no"
    assert "ports" not in migration
    assert migration["networks"] == ["esragpro-internal"]

    migration_dependencies = migration["depends_on"]
    assert isinstance(migration_dependencies, dict)
    assert migration_dependencies["postgres"]["condition"] == "service_healthy"

    migration_environment = dict(item.split("=", 1) for item in migration["environment"])
    assert migration_environment["DATABASE_URL"] == (
        "postgresql+asyncpg://${DB_USER:-esragpro}:${DB_PASSWORD:?DB_PASSWORD must be set}"
        "@postgres:5432/esragpro"
    )

    backend_dependencies = backend["depends_on"]
    assert isinstance(backend_dependencies, dict)
    assert backend_dependencies["migration"]["condition"] == "service_completed_successfully"
