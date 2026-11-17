from __future__ import annotations

from pathlib import Path

import yaml

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_PATH = _REPOSITORY_ROOT / "docker-compose.secure.yml"
_EMBEDDING_MODEL = "BAAI/bge-m3"
_EMBEDDING_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
_EMBEDDING_TARGET = f"/opt/esragpro/models/BAAI--bge-m3/{_EMBEDDING_REVISION}"


def _compose() -> dict[str, object]:
    payload = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _service(name: str) -> dict[str, object]:
    services = _compose()["services"]
    assert isinstance(services, dict)
    service = services[name]
    assert isinstance(service, dict)
    return service


def _environment(service: dict[str, object]) -> dict[str, str]:
    raw_environment = service.get("environment", {})
    if isinstance(raw_environment, dict):
        return {str(key): str(value) for key, value in raw_environment.items()}
    assert isinstance(raw_environment, list)
    environment: dict[str, str] = {}
    for entry in raw_environment:
        assert isinstance(entry, str)
        key, value = entry.split("=", 1)
        environment[key] = value
    return environment


def _string_list(mapping: dict[str, object], key: str) -> list[str]:
    value = mapping[key]
    assert isinstance(value, list)
    return [str(item) for item in value]


def test_secure_frontend_selects_secure_config_and_publishes_matching_ports() -> None:
    frontend = _service("frontend")
    build = frontend["build"]
    assert isinstance(build, dict)
    args = build["args"]
    assert isinstance(args, dict)
    assert args["NGINX_CONFIG"] == "nginx-secure.conf"
    assert frontend["ports"] == ["${HTTP_PORT:-80}:80", "${HTTPS_PORT:-443}:443"]

    dockerfile = (_REPOSITORY_ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")
    assert "ARG NGINX_CONFIG=nginx.conf" in dockerfile
    assert "COPY ${NGINX_CONFIG} /etc/nginx/conf.d/default.conf" in dockerfile
    assert "EXPOSE 80 443" in dockerfile

    nginx = (_REPOSITORY_ROOT / "frontend/nginx-secure.conf").read_text(encoding="utf-8")
    assert "listen 80;" in nginx
    assert "listen 443 ssl http2;" in nginx
    assert "ssl_certificate /etc/nginx/certs/fullchain.pem;" in nginx
    assert "ssl_certificate_key /etc/nginx/certs/privkey.pem;" in nginx
    assert "auth_basic_user_file /etc/nginx/.htpasswd;" in nginx


def test_secure_datastores_are_auth_checked_internal_and_health_gated() -> None:
    compose = _compose()
    services = compose["services"]
    assert isinstance(services, dict)

    for name in ("elasticsearch", "postgres", "redis", "neo4j", "minio"):
        service = _service(name)
        assert "ports" not in service, f"{name} must remain internal"
        assert service["restart"] == "unless-stopped"
        assert "healthcheck" in service
        assert "esragpro-internal" in _string_list(service, "networks")

    elasticsearch = _service("elasticsearch")
    es_healthcheck = elasticsearch["healthcheck"]
    assert isinstance(es_healthcheck, dict)
    es_test = " ".join(_string_list(es_healthcheck, "test"))
    assert "curl" in es_test and "-u" in es_test
    assert "$${ELASTIC_PASSWORD}" in es_test

    postgres = _service("postgres")
    postgres_healthcheck = postgres["healthcheck"]
    assert isinstance(postgres_healthcheck, dict)
    assert "pg_isready" in " ".join(_string_list(postgres_healthcheck, "test"))

    redis = _service("redis")
    redis_environment = _environment(redis)
    assert redis_environment["REDIS_PASSWORD"] == "${REDIS_PASSWORD:?REDIS_PASSWORD must be set}"
    redis_healthcheck = redis["healthcheck"]
    assert isinstance(redis_healthcheck, dict)
    redis_healthcheck_test = " ".join(_string_list(redis_healthcheck, "test"))
    assert "redis-cli" in redis_healthcheck_test
    assert "$${REDIS_PASSWORD}" in redis_healthcheck_test

    backend = _service("backend")
    depends_on = backend["depends_on"]
    assert isinstance(depends_on, dict)
    for name in ("elasticsearch", "postgres", "redis", "neo4j", "minio"):
        assert depends_on[name]["condition"] == "service_healthy"

    backend_environment = _environment(backend)
    assert backend_environment["LLM_MODEL"] == "${LLM_MODEL}"
    assert "LLLM_MODEL" not in backend_environment["LLM_MODEL"]
    for key in ("ES_PASSWORD", "NEO4J_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"):
        assert ":?" in backend_environment[key]
    assert ":-changeme" not in "\n".join(backend_environment.values())


def test_secure_topology_provisions_neo4j_minio_and_security_artifacts() -> None:
    compose = _compose()
    services = compose["services"]
    assert isinstance(services, dict)
    assert "neo4j" in services
    assert "minio" in services
    assert "minio-worker" in services

    neo4j_environment = _environment(_service("neo4j"))
    assert ":?" in neo4j_environment["NEO4J_AUTH"]
    neo4j_healthcheck = _service("neo4j")["healthcheck"]
    assert isinstance(neo4j_healthcheck, dict)
    assert "cypher-shell" in " ".join(_string_list(neo4j_healthcheck, "test"))

    minio_environment = _environment(_service("minio"))
    assert ":?" in minio_environment["MINIO_ROOT_USER"]
    assert ":?" in minio_environment["MINIO_ROOT_PASSWORD"]
    minio_worker = _service("minio-worker")
    minio_worker_dependencies = minio_worker["depends_on"]
    assert isinstance(minio_worker_dependencies, dict)
    assert minio_worker_dependencies["minio"]["condition"] == "service_healthy"

    frontend_volumes = _service("frontend")["volumes"]
    assert isinstance(frontend_volumes, list)
    assert "./certs:/etc/nginx/certs:ro" in frontend_volumes
    assert "./.htpasswd:/etc/nginx/.htpasswd:ro" in frontend_volumes

    setup_security = (_REPOSITORY_ROOT / "setup-security.sh").read_text(encoding="utf-8")
    assert 'HTPASSWD_FILE="$PROJECT_DIR/.htpasswd"' in setup_security
    assert 'CERTS_DIR="$PROJECT_DIR/certs"' in setup_security


def test_install_waits_on_compose_health_instead_of_hidden_datastore_port() -> None:
    install = (_REPOSITORY_ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'COMPOSE_FILE="docker-compose.secure.yml"' in install
    assert "docker inspect" in install
    assert "State.Health.Status" in install
    assert "curl -s http://localhost:9200" not in install
    assert 'PUBLIC_SCHEME="https"' in install


def test_secure_stack_runs_migrations_before_backend_and_worker_readiness() -> None:
    dockerfile = (_REPOSITORY_ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    assert "COPY alembic.ini ./" in dockerfile
    assert "COPY alembic ./alembic" in dockerfile

    migration = _service("migration")
    assert " ".join(_string_list(migration, "command")) == "alembic upgrade head"
    assert migration["restart"] == "no"
    migration_dependencies = migration["depends_on"]
    assert isinstance(migration_dependencies, dict)
    assert migration_dependencies["postgres"]["condition"] == "service_healthy"
    migration_environment = _environment(migration)
    assert migration_environment["DATABASE_URL"] == (
        "postgresql+asyncpg://${DB_USER:-esragpro}:${DB_PASSWORD:?DB_PASSWORD must be set}"
        "@postgres:5432/esragpro"
    )

    for service_name in ("backend", "minio-worker"):
        dependencies = _service(service_name)["depends_on"]
        assert isinstance(dependencies, dict)
        assert dependencies["migration"]["condition"] == "service_completed_successfully"

    backend_healthcheck = _service("backend")["healthcheck"]
    assert isinstance(backend_healthcheck, dict)
    healthcheck = " ".join(_string_list(backend_healthcheck, "test"))
    assert "/ready" in healthcheck
    assert "/health" not in healthcheck

    frontend_dependencies = _service("frontend")["depends_on"]
    assert isinstance(frontend_dependencies, dict)
    assert frontend_dependencies["backend"]["condition"] == "service_healthy"


def test_secure_compose_materializes_pinned_embedding_volume_before_backend() -> None:
    compose = _compose()
    volumes = compose["volumes"]
    assert isinstance(volumes, dict)
    assert "embedding_model" in volumes

    init = _service("embedding-model-init")
    build = init["build"]
    assert isinstance(build, dict)
    assert build["context"] == "./backend"
    assert build["dockerfile"] == "Dockerfile"
    assert init["entrypoint"] == ["python", "/app/scripts/provision_embedding_model.py"]
    command = init["command"]
    assert isinstance(command, list)
    assert command == [
        "verify",
        "--lock",
        "/app/model-locks/BAAI--bge-m3.json",
        "--model-root",
        "/models",
    ]
    assert init["restart"] == "no"
    init_volumes = init["volumes"]
    assert isinstance(init_volumes, list)
    assert init_volumes == ["embedding_model:/models"]
    assert "ports" not in init

    backend = _service("backend")
    dependencies = backend["depends_on"]
    assert isinstance(dependencies, dict)
    assert dependencies["embedding-model-init"]["condition"] == "service_completed_successfully"
    backend_volumes = backend["volumes"]
    assert isinstance(backend_volumes, list)
    assert "embedding_model:/opt/esragpro/models:ro" in backend_volumes
    assert all(
        not str(volume).startswith("./")
        for volume in (init_volumes + ["embedding_model:/opt/esragpro/models:ro"])
    )

    backend_environment = _environment(backend)
    assert backend_environment["EMBEDDING_PROVIDER"] == "local"
    assert backend_environment["EMBEDDING_MODEL"] == _EMBEDDING_MODEL
    assert backend_environment["EMBEDDING_MODEL_REVISION"] == _EMBEDDING_REVISION
    assert backend_environment["EMBEDDING_MODEL_PATH"] == _EMBEDDING_TARGET
    assert backend_environment["EMBEDDING_DIMENSION"] == "1024"
    assert backend_environment["EMBEDDING_LOCAL_FILES_ONLY"] == "true"
    assert backend_environment["EMBEDDING_OFFLINE"] == "true"
    assert backend_environment["HF_HUB_OFFLINE"] == "1"
    assert backend_environment["TRANSFORMERS_OFFLINE"] == "1"
    assert backend_environment["UVICORN_WORKERS"] == "1"


def test_secure_backend_resources_and_readiness_allow_local_model_startup() -> None:
    backend = _service("backend")
    healthcheck = backend["healthcheck"]
    assert isinstance(healthcheck, dict)
    assert healthcheck["start_period"] == "180s"
    healthcheck_command = " ".join(_string_list(healthcheck, "test"))
    assert "/ready" in healthcheck_command
    assert "/health" not in healthcheck_command

    deploy = backend["deploy"]
    assert isinstance(deploy, dict)
    resources = deploy["resources"]
    assert isinstance(resources, dict)
    limits = resources["limits"]
    reservations = resources["reservations"]
    assert isinstance(limits, dict)
    assert isinstance(reservations, dict)
    assert limits["memory"] == "6G"
    assert reservations["memory"] == "3G"


def test_secure_install_builds_and_provisions_before_starting_stack() -> None:
    install = (_REPOSITORY_ROOT / "install.sh").read_text(encoding="utf-8")
    build = 'docker compose -f "$COMPOSE_FILE" build embedding-model-init'
    verify = "scripts/provision-embedding-model.sh verify --secure"
    acquire = "scripts/provision-embedding-model.sh acquire --secure"
    import_command = (
        'scripts/provision-embedding-model.sh import --secure --source "$EMBEDDING_MODEL_SOURCE"'
    )
    up = 'docker compose -f "$COMPOSE_FILE" up -d --build'
    assert build in install
    assert verify in install
    assert acquire in install
    assert import_command in install
    assert install.index(build) < install.index(verify) < install.index(up)
    assert install.index(acquire) < install.index(up)
    assert install.index(import_command) < install.index(up)
    assert "EMBEDDING_MODEL_SOURCE" in install
    assert "Using non-secure compose; embedding model provisioning is disabled." in install
    assert 'echo "$EMBEDDING_MODEL_SOURCE"' not in install


def test_embedding_environment_templates_document_secure_model_contract() -> None:
    for path in (_REPOSITORY_ROOT / ".env.example", _REPOSITORY_ROOT / "backend/.env.example"):
        source = path.read_text(encoding="utf-8")
        assert _EMBEDDING_MODEL in source
        assert _EMBEDDING_REVISION in source
        assert "1024" in source
        assert "/opt/esragpro/models/BAAI--bge-m3" in source
        assert "EMBEDDING_MODEL_SOURCE" in source
        assert "EMBEDDING_LOCAL_FILES_ONLY" in source
        assert "EMBEDDING_OFFLINE" in source
        assert "UVICORN_WORKERS=1" in source
        assert "6G" in source and "3G" in source
