#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "bcrypt>=4.0",
#     "httpx>=0.26.0",
# ]
# ///

# --- How to run ---
# 1. Install uv (if not installed):
#      curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. Run directly (no venv, no pip install needed):
#      uv run run-g1-live-security-matrix.py --confirm-g1-live-security
# 3. Or make executable and run:
#      chmod +x run-g1-live-security-matrix.py && ./run-g1-live-security-matrix.py --confirm-g1-live-security
# -----------------------------------------------------------------

"""Run the isolated G1 tenant and knowledge-base security matrix.

The runner is deliberately explicit about the boundary it owns: one generated
Compose project, loopback-only ports, runtime-local synthetic artifacts, and an
unconditional teardown. It never reads the repository ``.env`` file and never
prints credentials, tokens, or generated private key material.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TypeAlias
from urllib.parse import urlparse
from uuid import UUID, uuid4

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

RUN_ID: Final = "run-20261010-g1-live-security"
PROJECT_PREFIX: Final = "esragpro_g1_20261010_"
CONFIRM_FLAG: Final = "--confirm-g1-live-security"
DEFAULT_HTTP_PORT: Final = 18081
DEFAULT_HTTPS_PORT: Final = 18444
TOTAL_TIMEOUT_SECONDS: Final = 45.0 * 60.0
CLEANUP_GRACE_SECONDS: Final = 5.0 * 60.0
COMMAND_TIMEOUT_SECONDS: Final = 120.0
BUILD_TIMEOUT_SECONDS: Final = 40.0 * 60.0
COMPOSE_FILENAME: Final = "docker-compose.secure.yml"
MARKER_PREFIX: Final = "g1-a-marker-"
REPORT_MARKER: Final = "G1_MODEL_REUSE_RETRY_FINAL"
LOCAL_DOCKER_CONTEXT: Final = "default"
MODEL_NAME: Final = "BAAI/bge-m3"
MODEL_REVISION: Final = "5617a9f61b028005a4858fdac845db406aefb181"
MODEL_TARGET_DIR: Final = f"BAAI--bge-m3/{MODEL_REVISION}"
MODEL_LOCK_RELATIVE_PATH: Final = Path("backend") / "model-locks" / "BAAI--bge-m3.json"
MODEL_LOCK_SHA256: Final = "c0c0fd999e31e9617f026b1ef38af7650bc66b6fb07dc5e985ddd42a9dbddd3e"
MODEL_VOLUME_KEY: Final = "embedding_model"
MODEL_VOLUME_NAME: Final = "esragpro_auth_20260921_embedding_model"
MODEL_VOLUME_PROJECT: Final = "esragpro_auth_20260921"


class RunnerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RunPaths:
    runtime_root: Path
    attempt_root: Path
    env_file: Path
    override_file: Path
    cert_dir: Path
    certbot_dir: Path
    htpasswd_file: Path
    report_file: Path
    checkpoint_file: Path


@dataclass(frozen=True, slots=True)
class SyntheticCredentials:
    values: Mapping[str, str]
    admin_username: str
    admin_password: str
    tenant_a: UUID
    kb_a: UUID
    tenant_b: UUID
    kb_b: UUID
    tenant_b_username: str
    tenant_b_password: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class DockerTarget:
    executable: str
    context: str
    environment: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    containers: tuple[str, ...]
    networks: tuple[str, ...]
    volumes: tuple[str, ...]


class ResponseLike(Protocol):
    status_code: int
    headers: Mapping[str, str]
    text: str

    def json(self) -> JsonValue: ...


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _make_paths(root: Path) -> RunPaths:
    runtime_root = root / ".agent-team" / "control-plane" / ".runtime" / RUN_ID
    attempt_root = runtime_root / f"attempt-{secrets.token_hex(6)}"
    cert_dir = attempt_root / "certs"
    return RunPaths(
        runtime_root=runtime_root,
        attempt_root=attempt_root,
        env_file=attempt_root / "synthetic.env",
        override_file=attempt_root / "docker-compose.g1.override.yml",
        cert_dir=cert_dir,
        certbot_dir=attempt_root / "certbot_www",
        htpasswd_file=attempt_root / ".htpasswd",
        report_file=runtime_root / "g1-model-reuse-retry-report.md",
        checkpoint_file=runtime_root / "g1-model-reuse-retry-checkpoint.md",
    )


def _secret(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(24)}"


def _credentials(http_port: int, https_port: int) -> SyntheticCredentials:
    tenant_a, kb_a, tenant_b, kb_b = (uuid4() for _ in range(4))
    admin_username = f"g1_admin_{secrets.token_hex(4)}"
    admin_password = _secret("G1Admin-")
    tenant_b_username = f"g1_reader_{secrets.token_hex(4)}"
    tenant_b_password = _secret("G1Reader-")
    values = {
        "ES_PASSWORD": _secret("G1Es-"),
        "DB_USER": "esragpro",
        "DB_PASSWORD": _secret("G1Db-"),
        "REDIS_PASSWORD": _secret("G1Redis-"),
        "NEO4J_PASSWORD": _secret("G1Neo4j-"),
        "MINIO_ACCESS_KEY": f"g1minio{secrets.token_hex(8)}",
        "MINIO_SECRET_KEY": _secret("G1Minio-"),
        "MINIO_BUCKET_NAME": f"g1-evidence-{secrets.token_hex(5)}",
        "MINIO_SECURE": "false",
        "LLM_PROVIDER": "zhipu",
        "LLM_API_KEY": _secret("G1Llm-"),
        "LLM_MODEL": "glm-4-flash",
        "JWT_SECRET_KEY": _secret("G1Jwt-"),
        "BOOTSTRAP_TOKEN": _secret("G1Bootstrap-"),
        "BOOTSTRAP_TENANT_ID": str(tenant_a),
        "BOOTSTRAP_KNOWLEDGE_BASE_ID": str(kb_a),
        "CORS_ALLOWED_ORIGINS": f"https://127.0.0.1:{https_port}",
        "JWT_ISSUER": "https://g1-auth.esragpro.test",
        "JWT_AUDIENCE": "https://g1-api.esragpro.test",
        "HTTP_PORT": str(http_port),
        "HTTPS_PORT": str(https_port),
        "NEO4J_ENABLED": "false",
        "WORLD_MEMORY_CANDIDATE_SOURCES_ENABLED": "false",
        "WORLD_MEMORY_EVENT_HISTORY_ENABLED": "false",
        "UVICORN_WORKERS": "1",
        "EMBEDDING_PROVIDER": "local",
        "EMBEDDING_MODEL": MODEL_NAME,
        "EMBEDDING_MODEL_REVISION": MODEL_REVISION,
        "EMBEDDING_MODEL_PATH": f"/opt/esragpro/models/{MODEL_TARGET_DIR}",
        "EMBEDDING_LOCAL_FILES_ONLY": "true",
        "EMBEDDING_OFFLINE": "true",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    return SyntheticCredentials(
        values=values,
        admin_username=admin_username,
        admin_password=admin_password,
        tenant_a=tenant_a,
        kb_a=kb_a,
        tenant_b=tenant_b,
        kb_b=kb_b,
        tenant_b_username=tenant_b_username,
        tenant_b_password=tenant_b_password,
    )


def _redact(value: str, credentials: SyntheticCredentials | None = None) -> str:
    redacted = value
    if credentials is not None:
        candidates = (
            *credentials.values.values(),
            credentials.admin_password,
            credentials.tenant_b_password,
        )
        for secret in sorted((item for item in candidates if item), key=len, reverse=True):
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


def _reserve_loopback_ports() -> tuple[int, int]:
    sockets: list[socket.socket] = []
    try:
        for _ in range(2):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            sockets.append(listener)
        ports = tuple(listener.getsockname()[1] for listener in sockets)
    finally:
        for listener in sockets:
            listener.close()
    if len(set(ports)) != 2 or any(port in {80, 443} for port in ports):
        raise RunnerError("failed to reserve two distinct non-default loopback ports")
    return ports[0], ports[1]


def _write_env(paths: RunPaths, credentials: SyntheticCredentials) -> None:
    paths.attempt_root.mkdir(parents=True, exist_ok=False)
    paths.cert_dir.mkdir()
    paths.certbot_dir.mkdir()
    content = "".join(f"{key}={value}\n" for key, value in sorted(credentials.values.items()))
    paths.env_file.write_text(content, encoding="utf-8", newline="\n")


def _run_command(
    argv: Sequence[str],
    *,
    deadline: float,
    timeout: float,
    input_text: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RunnerError("hard deadline elapsed before command execution")
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            input=input_text,
            timeout=min(timeout, remaining),
            check=False,
            env=None if environment is None else dict(environment),
        )
    except FileNotFoundError as error:
        raise RunnerError(f"required executable is unavailable: {argv[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise RunnerError(f"command timed out: {argv[0]}") from error
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _is_local_docker_host(value: str) -> bool:
    parsed = urlparse(value)
    scheme = parsed.scheme.casefold()
    if scheme in {"unix", "npipe"}:
        return bool(parsed.path)
    if scheme != "tcp":
        return False
    return (parsed.hostname or "").casefold() in {"127.0.0.1", "localhost", "::1"}


def _assert_local_docker_target(docker: str, deadline: float) -> DockerTarget:
    docker_host = os.environ.get("DOCKER_HOST", "").strip()
    if docker_host and not _is_local_docker_host(docker_host):
        raise RunnerError("refusing potentially nonlocal Docker target from DOCKER_HOST")
    docker_context = os.environ.get("DOCKER_CONTEXT", "").strip()
    if docker_context and docker_context != LOCAL_DOCKER_CONTEXT:
        raise RunnerError("refusing potentially nonlocal Docker context from DOCKER_CONTEXT")

    environment = os.environ.copy()
    environment.pop("DOCKER_HOST", None)
    environment.pop("DOCKER_CONTEXT", None)
    selected = _run_command(
        [docker, "context", "show"],
        deadline=deadline,
        timeout=COMMAND_TIMEOUT_SECONDS,
        environment=environment,
    )
    if selected.code != 0 or selected.stdout.strip() != LOCAL_DOCKER_CONTEXT:
        raise RunnerError("refusing potentially nonlocal active Docker context")

    inspected = _run_command(
        [
            docker,
            "context",
            "inspect",
            LOCAL_DOCKER_CONTEXT,
            "--format",
            "{{json .Endpoints.docker.Host}}",
        ],
        deadline=deadline,
        timeout=COMMAND_TIMEOUT_SECONDS,
        environment=environment,
    )
    if inspected.code != 0:
        raise RunnerError("Docker local-context endpoint inspection failed")
    try:
        endpoint = json.loads(inspected.stdout.strip())
    except json.JSONDecodeError as error:
        raise RunnerError("Docker local-context endpoint was not valid JSON") from error
    if not isinstance(endpoint, str) or not _is_local_docker_host(endpoint):
        raise RunnerError("Docker default context does not use a local endpoint")
    return DockerTarget(docker, LOCAL_DOCKER_CONTEXT, environment)


def _docker_args(target: DockerTarget, *arguments: str) -> list[str]:
    return [target.executable, "--context", target.context, *arguments]


def _cleanup_deadline(work_deadline: float) -> float:
    return max(work_deadline, time.monotonic()) + CLEANUP_GRACE_SECONDS


def _checked_command(
    argv: Sequence[str],
    *,
    deadline: float,
    timeout: float,
    credentials: SyntheticCredentials,
    input_text: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    result = _run_command(
        argv,
        deadline=deadline,
        timeout=timeout,
        input_text=input_text,
        environment=environment,
    )
    if result.code != 0:
        detail = _redact((result.stderr or result.stdout).strip()[-2000:], credentials)
        raise RunnerError(f"command failed with exit {result.code}: {detail}")
    return result


def _write_synthetic_tls(
    paths: RunPaths,
    credentials: SyntheticCredentials,
    deadline: float,
) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        raise RunnerError("openssl is required to generate runtime-local synthetic TLS")
    cert = paths.cert_dir / "fullchain.pem"
    key = paths.cert_dir / "privkey.pem"
    _checked_command(
        [
            openssl,
            "req",
            "-x509",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        deadline=deadline,
        timeout=COMMAND_TIMEOUT_SECONDS,
        credentials=credentials,
    )
    hash_result = _checked_command(
        [openssl, "passwd", "-apr1", "-stdin"],
        deadline=deadline,
        timeout=COMMAND_TIMEOUT_SECONDS,
        credentials=credentials,
        input_text=credentials.admin_password,
    )
    digest = hash_result.stdout.strip()
    if not digest.startswith("$apr1$"):
        raise RunnerError("openssl did not return an Apache htpasswd hash")
    paths.htpasswd_file.write_text(
        f"g1basic:{digest}\n",
        encoding="utf-8",
        newline="\n",
    )


def _compose_path(path: Path) -> str:
    return path.resolve().as_posix()


def _verify_model_lock(root: Path) -> None:
    lock_path = root / MODEL_LOCK_RELATIVE_PATH
    try:
        lock_bytes = lock_path.read_bytes()
        lock = json.loads(lock_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RunnerError("expected embedding model lock could not be read") from error
    if not isinstance(lock, dict):
        raise RunnerError("expected embedding model lock has an invalid shape")
    if hashlib.sha256(lock_bytes).hexdigest() != MODEL_LOCK_SHA256:
        raise RunnerError("embedding model lock digest does not match the expected lock")
    if (
        lock.get("model") != MODEL_NAME
        or lock.get("revision") != MODEL_REVISION
        or lock.get("target_dir") != MODEL_TARGET_DIR
    ):
        raise RunnerError("embedding model lock does not match the expected model revision")


def _verify_external_model_volume(
    target: DockerTarget,
    root: Path,
    deadline: float,
) -> None:
    _verify_model_lock(root)
    inspected = _run_command(
        [
            *_docker_args(target, "volume", "inspect", "--format", "{{json .}}", MODEL_VOLUME_NAME),
        ],
        deadline=deadline,
        timeout=COMMAND_TIMEOUT_SECONDS,
        environment=target.environment,
    )
    if inspected.code != 0:
        raise RunnerError("expected external embedding model volume is unavailable")
    try:
        volume = json.loads(inspected.stdout.strip())
    except json.JSONDecodeError as error:
        raise RunnerError(
            "external embedding model volume inspection was not valid JSON"
        ) from error
    if not isinstance(volume, dict):
        raise RunnerError("external embedding model volume inspection has an invalid shape")
    labels = volume.get("Labels")
    if not isinstance(labels, dict):
        raise RunnerError("external embedding model volume has no Compose labels")
    if (
        volume.get("Name") != MODEL_VOLUME_NAME
        or labels.get("com.docker.compose.project") != MODEL_VOLUME_PROJECT
        or labels.get("com.docker.compose.volume") != MODEL_VOLUME_KEY
    ):
        raise RunnerError(
            "external embedding model volume name/project labels did not match expected identity"
        )


def _write_override(
    paths: RunPaths,
    project: str,
    http_port: int,
    https_port: int,
) -> None:
    prefix = f"{project}-"
    cert_dir = _compose_path(paths.cert_dir)
    htpasswd = _compose_path(paths.htpasswd_file)
    certbot = _compose_path(paths.certbot_dir)
    lines = [
        "services:",
        "  embedding-model-init:",
        "    command:",
        "      - verify",
        "      - --lock",
        "      - /app/model-locks/BAAI--bge-m3.json",
        "      - --model-root",
        "      - /models",
        "    volumes:",
        "      - type: volume",
        f"        source: {MODEL_VOLUME_KEY}",
        "        target: /models",
        "        read_only: true",
        "  backend:",
        f"    container_name: {prefix}backend",
        "    environment:",
        '      BOOTSTRAP_TOKEN: "${BOOTSTRAP_TOKEN:?BOOTSTRAP_TOKEN must be set}"',
        '      BOOTSTRAP_TENANT_ID: "${BOOTSTRAP_TENANT_ID:?BOOTSTRAP_TENANT_ID must be set}"',
        '      BOOTSTRAP_KNOWLEDGE_BASE_ID: "${BOOTSTRAP_KNOWLEDGE_BASE_ID:?BOOTSTRAP_KNOWLEDGE_BASE_ID must be set}"',
        '      CORS_ALLOWED_ORIGINS: "${CORS_ALLOWED_ORIGINS:?CORS_ALLOWED_ORIGINS must be set}"',
        '      JWT_ISSUER: "${JWT_ISSUER:?JWT_ISSUER must be set}"',
        '      JWT_AUDIENCE: "${JWT_AUDIENCE:?JWT_AUDIENCE must be set}"',
        "    volumes:",
        "      - type: volume",
        f"        source: {MODEL_VOLUME_KEY}",
        "        target: /opt/esragpro/models",
        "        read_only: true",
        "  elasticsearch:",
        f"    container_name: {prefix}elasticsearch",
        "  postgres:",
        f"    container_name: {prefix}postgres",
        "  redis:",
        f"    container_name: {prefix}redis",
        "  neo4j:",
        f"    container_name: {prefix}neo4j",
        "  minio:",
        f"    container_name: {prefix}minio",
        "  migration:",
        f"    container_name: {prefix}migration",
        "  minio-worker:",
        f"    container_name: {prefix}minio-worker",
        "  frontend:",
        f"    container_name: {prefix}frontend",
        "    ports: !override",
        f'      - "127.0.0.1:{http_port}:80"',
        f'      - "127.0.0.1:{https_port}:443"',
        "    volumes:",
        "      - type: bind",
        f'        source: "{cert_dir}"',
        "        target: /etc/nginx/certs",
        "        read_only: true",
        "      - type: bind",
        f'        source: "{htpasswd}"',
        "        target: /etc/nginx/.htpasswd",
        "        read_only: true",
        "      - type: bind",
        f'        source: "{certbot}"',
        "        target: /var/www/certbot",
        "        read_only: true",
        "    healthcheck:",
        '      test: ["CMD-SHELL", "wget --no-check-certificate -q -O - https://127.0.0.1/health | grep -q healthy"]',
        "",
        "volumes:",
    ]
    for name in (
        "es_data",
        "postgres_data",
        "redis_data",
        "neo4j_data",
        "neo4j_logs",
        "minio_data",
        "uploads_data",
    ):
        lines.extend((f"  {name}:", f"    name: {project}_{name}"))
    lines.extend(
        (
            f"  {MODEL_VOLUME_KEY}:",
            "    external: true",
            f"    name: {MODEL_VOLUME_NAME}",
        )
    )
    lines.extend(("", "networks:", "  esragpro-internal:", f"    name: {project}_internal"))
    lines.extend(("  esragpro-frontend:", f"    name: {project}_frontend", ""))
    paths.override_file.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _compose_args(
    target: DockerTarget,
    paths: RunPaths,
    project: str,
) -> list[str]:
    return [
        *_docker_args(target, "compose"),
        "--project-name",
        project,
        "--env-file",
        str(paths.env_file),
        "-f",
        str(_repo_root() / COMPOSE_FILENAME),
        "-f",
        str(paths.override_file),
    ]


def _compose_command(
    target: DockerTarget,
    paths: RunPaths,
    project: str,
    arguments: Sequence[str],
    *,
    deadline: float,
    timeout: float,
    credentials: SyntheticCredentials,
) -> CommandResult:
    return _checked_command(
        [*_compose_args(target, paths, project), *arguments],
        deadline=deadline,
        timeout=timeout,
        credentials=credentials,
        environment=target.environment,
    )


def _resource_lines(result: CommandResult) -> tuple[str, ...]:
    return tuple(sorted(line.strip() for line in result.stdout.splitlines() if line.strip()))


def _resource_snapshot(target: DockerTarget, deadline: float) -> ResourceSnapshot:
    common = ["--filter", "label=com.docker.compose.project"]
    containers = _run_command(
        [
            *_docker_args(
                target,
                "ps",
                "--all",
                *common,
                "--format",
                '{{.Label "com.docker.compose.project"}}',
            )
        ],
        deadline=deadline,
        timeout=COMMAND_TIMEOUT_SECONDS,
        environment=target.environment,
    )
    networks = _run_command(
        [
            *_docker_args(
                target,
                "network",
                "ls",
                *common,
                "--format",
                '{{.Label "com.docker.compose.project"}}',
            )
        ],
        deadline=deadline,
        timeout=COMMAND_TIMEOUT_SECONDS,
        environment=target.environment,
    )
    volumes = _run_command(
        [
            *_docker_args(
                target,
                "volume",
                "ls",
                *common,
                "--format",
                '{{.Label "com.docker.compose.project"}}',
            )
        ],
        deadline=deadline,
        timeout=COMMAND_TIMEOUT_SECONDS,
        environment=target.environment,
    )
    for label, result in (("containers", containers), ("networks", networks), ("volumes", volumes)):
        if result.code != 0:
            raise RunnerError(f"Docker preflight {label} inventory failed")
    return ResourceSnapshot(
        _resource_lines(containers),
        _resource_lines(networks),
        _resource_lines(volumes),
    )


def _assert_no_side_effects(
    before: ResourceSnapshot,
    after: ResourceSnapshot,
    project: str,
) -> None:
    if project in before.containers or project in before.networks or project in before.volumes:
        raise RunnerError("generated Compose project label was already present")
    for label, old, new in (
        ("containers", before.containers, after.containers),
        ("networks", before.networks, after.networks),
        ("volumes", before.volumes, after.volumes),
    ):
        if not set(old).issubset(new):
            raise RunnerError(f"preexisting {label} changed during isolated run")
        if project in new:
            raise RunnerError(f"owned Compose project remains after cleanup: {label}")


def _rendered_json(
    target: DockerTarget,
    paths: RunPaths,
    project: str,
    credentials: SyntheticCredentials,
    deadline: float,
) -> dict[str, JsonValue]:
    result = _compose_command(
        target,
        paths,
        project,
        ("config", "--format", "json"),
        deadline=deadline,
        timeout=COMMAND_TIMEOUT_SECONDS,
        credentials=credentials,
    )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RunnerError("merged secure Compose configuration was not JSON") from error
    if not isinstance(parsed, dict):
        raise RunnerError("merged secure Compose configuration has an invalid shape")
    return parsed


def _as_mapping(value: JsonValue, label: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, dict):
        raise RunnerError(f"Compose {label} is not an object")
    return value


def _validate_topology(
    rendered: Mapping[str, JsonValue],
    root: Path,
    project: str,
    http_port: int,
    https_port: int,
) -> None:
    services = _as_mapping(rendered.get("services"), "services")
    volumes = _as_mapping(rendered.get("volumes"), "volumes")
    model_volume = _as_mapping(volumes.get(MODEL_VOLUME_KEY), "embedding model volume")
    if model_volume.get("external") is not True or model_volume.get("name") != MODEL_VOLUME_NAME:
        raise RunnerError("secure Compose must reuse the exact external embedding model volume")
    for service in ("backend", "frontend", "migration", "embedding-model-init"):
        if service not in services:
            raise RunnerError(f"secure Compose is missing required service: {service}")
    for service, expected in (("backend", root / "backend"), ("frontend", root / "frontend")):
        config = _as_mapping(services[service], f"service {service}")
        build = _as_mapping(config.get("build"), f"service {service}.build")
        context = str(build.get("context", "")).replace("\\", "/").rstrip("/").casefold()
        if context != expected.resolve().as_posix().casefold():
            raise RunnerError(f"{service} build does not use current source context")
    frontend = _as_mapping(services["frontend"], "service frontend")
    ports = frontend.get("ports")
    if not isinstance(ports, list) or len(ports) != 2:
        raise RunnerError("frontend must publish exactly two isolated ports")
    rendered_ports = " ".join(str(port) for port in ports)
    if f"{http_port}" not in rendered_ports or f"{https_port}" not in rendered_ports:
        raise RunnerError("frontend port override did not survive Compose rendering")
    if http_port in {80, 443} or https_port in {80, 443}:
        raise RunnerError("default web ports are forbidden")
    init = _as_mapping(services["embedding-model-init"], "service embedding-model-init")
    command = init.get("command")
    if not isinstance(command, list) or not command or command[0] != "verify":
        raise RunnerError("embedding-model-init must verify the reused model volume")
    for service, target in (
        ("embedding-model-init", "/models"),
        ("backend", "/opt/esragpro/models"),
    ):
        config = _as_mapping(services[service], f"service {service}")
        mounts = config.get("volumes")
        if not isinstance(mounts, list):
            raise RunnerError(f"{service} has no model volume mount")
        for mount in mounts:
            if (
                isinstance(mount, dict)
                and mount.get("source") == MODEL_VOLUME_KEY
                and mount.get("target") == target
            ):
                if mount.get("read_only") is not True:
                    raise RunnerError(f"{service} model volume mount must be read-only")
                break
        else:
            raise RunnerError(f"{service} is missing the expected model volume mount")
    for service, config_value in services.items():
        config = _as_mapping(config_value, f"service {service}")
        container_name = config.get("container_name")
        if not isinstance(container_name, str) or not container_name.startswith(f"{project}-"):
            raise RunnerError(f"service {service} is not uniquely named")


def _compose_state(result: CommandResult) -> dict[str, Mapping[str, JsonValue]]:
    raw = result.stdout.strip()
    if not raw:
        raise RunnerError("Compose returned no service state")
    values: list[JsonValue]
    try:
        parsed = json.loads(raw)
        values = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        try:
            values = [json.loads(line) for line in raw.splitlines() if line.strip()]
        except json.JSONDecodeError as error:
            raise RunnerError("Compose service state was not valid JSON") from error
    states: dict[str, Mapping[str, JsonValue]] = {}
    for value in values:
        if isinstance(value, dict):
            service = value.get("Service") or value.get("service")
            if isinstance(service, str):
                states[service] = value
    return states


def _require_healthy_stack(
    target: DockerTarget,
    paths: RunPaths,
    project: str,
    credentials: SyntheticCredentials,
    deadline: float,
) -> dict[str, Mapping[str, JsonValue]]:
    _compose_command(
        target,
        paths,
        project,
        ("up", "-d", "--build", "--wait", "--wait-timeout", "2400"),
        deadline=deadline,
        timeout=BUILD_TIMEOUT_SECONDS,
        credentials=credentials,
    )
    result = _compose_command(
        target,
        paths,
        project,
        ("ps", "--all", "--format", "json"),
        deadline=deadline,
        timeout=COMMAND_TIMEOUT_SECONDS,
        credentials=credentials,
    )
    states = _compose_state(result)
    for service in (
        "elasticsearch",
        "postgres",
        "redis",
        "minio",
        "backend",
        "frontend",
    ):
        state = states.get(service)
        if state is None or str(state.get("State", state.get("state", ""))) != "running":
            raise RunnerError(f"required service is not running: {service}")
        health = str(state.get("Health", state.get("health", "")))
        if health and health != "healthy":
            raise RunnerError(f"required service is not healthy: {service}")
    for service in ("migration", "embedding-model-init"):
        state = states.get(service)
        if state is None or str(state.get("State", state.get("state", ""))) != "exited":
            raise RunnerError(f"one-shot service did not exit: {service}")
        if str(state.get("ExitCode", state.get("exit_code", "1"))) != "0":
            raise RunnerError(f"one-shot service failed: {service}")
    worker = states.get("minio-worker")
    if worker is None or str(worker.get("State", worker.get("state", ""))) != "running":
        raise RunnerError("minio-worker is not running")
    return states


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _bcrypt_hash(password: str) -> str:
    try:
        import bcrypt
    except ModuleNotFoundError as error:
        raise RunnerError("bcrypt is required to create synthetic tenant B credentials") from error
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def _seed_tenant_b(
    target: DockerTarget,
    paths: RunPaths,
    project: str,
    credentials: SyntheticCredentials,
    deadline: float,
) -> None:
    password_hash = _bcrypt_hash(credentials.tenant_b_password)
    query = """
BEGIN;
INSERT INTO tenants (id, name, slug, is_active) VALUES ({tenant}, 'G1 Tenant B', {slug}, TRUE);
INSERT INTO knowledge_bases (id, tenant_id, name, slug, is_active)
VALUES ({kb}, {tenant}, 'G1 Knowledge Base B', {kb_slug}, TRUE);
INSERT INTO users (id, tenant_id, username, email, password_hash, role, is_active)
VALUES ({user}, {tenant}, {username}, NULL, {password_hash}, 'user', TRUE);
INSERT INTO memberships (id, user_id, knowledge_base_id, role)
VALUES ({membership}, {user}, {kb}, 'reader');
COMMIT;
""".format(
        tenant=_sql_literal(str(credentials.tenant_b)),
        slug=_sql_literal(f"g1-tenant-b-{credentials.tenant_b.hex[:8]}"),
        kb=_sql_literal(str(credentials.kb_b)),
        kb_slug=_sql_literal(f"g1-kb-b-{credentials.kb_b.hex[:8]}"),
        user=_sql_literal(str(uuid4())),
        username=_sql_literal(credentials.tenant_b_username),
        password_hash=_sql_literal(password_hash),
        membership=_sql_literal(str(uuid4())),
    )
    _compose_command(
        target,
        paths,
        project,
        (
            "exec",
            "-T",
            "postgres",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            credentials.values["DB_USER"],
            "-d",
            "esragpro",
            "-c",
            query,
        ),
        deadline=deadline,
        timeout=COMMAND_TIMEOUT_SECONDS,
        credentials=credentials,
    )


def _response_json(response: ResponseLike) -> dict[str, JsonValue]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise RunnerError("application response was not a JSON object")
    return payload


def _expect_status(response: ResponseLike, expected: int | tuple[int, ...], label: str) -> None:
    statuses = (expected,) if isinstance(expected, int) else expected
    status_code = getattr(response, "status_code", None)
    if status_code not in statuses:
        raise RunnerError(f"{label} returned unexpected HTTP status {status_code}")


def _token(payload: Mapping[str, JsonValue], label: str) -> str:
    token = payload.get("access_token")
    if not isinstance(token, str) or not token or token.count(".") != 2:
        raise RunnerError(f"{label} did not return a usable bearer token; fail closed")
    return token


def _documents(payload: Mapping[str, JsonValue]) -> list[Mapping[str, JsonValue]]:
    values = payload.get("documents")
    if not isinstance(values, list):
        raise RunnerError("document list response has no documents array")
    return [value for value in values if isinstance(value, dict)]


def _run_http_matrix(
    https_port: int,
    http_port: int,
    credentials: SyntheticCredentials,
    deadline: float,
) -> dict[str, str]:
    try:
        import httpx
    except ModuleNotFoundError as error:
        raise RunnerError("project httpx is required for the secure HTTP matrix") from error
    if time.monotonic() >= deadline:
        raise RunnerError("hard deadline elapsed before HTTP matrix")
    https_url = f"https://127.0.0.1:{https_port}"
    http_url = f"http://127.0.0.1:{http_port}"
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0)
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0)
    transport = httpx.HTTPTransport(
        http2=False,
        retries=2,
        limits=limits,
        socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)],
    )
    marker = f"{MARKER_PREFIX}{secrets.token_hex(8)}"
    content = f"tenant A synthetic security marker {marker}\n".encode()
    evidence: dict[str, str] = {}
    with httpx.Client(
        base_url=https_url,
        transport=transport,
        timeout=timeout,
        verify=False,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        redirect = client.get(f"{http_url}/health")
        _expect_status(redirect, 301, "HTTP health redirect")
        if not str(redirect.headers.get("location", "")).startswith("https://"):
            raise RunnerError("HTTP health did not redirect to HTTPS")
        evidence["http_redirect"] = str(redirect.status_code)

        health = client.get("/health")
        _expect_status(health, 200, "HTTPS health")
        if "healthy" not in health.text:
            raise RunnerError("HTTPS health body was not healthy")
        frontend = client.get("/")
        _expect_status(frontend, 200, "frontend index")
        api_health = client.get("/api/v1/kb/health")
        _expect_status(api_health, 200, "KB health")
        evidence["https_health"] = str(health.status_code)
        evidence["frontend"] = str(frontend.status_code)
        evidence["kb_health"] = str(api_health.status_code)

        cors = client.options(
            "/api/v1/kb/health",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        _expect_status(cors, 400, "untrusted CORS preflight")
        if cors.headers.get("access-control-allow-origin"):
            raise RunnerError("untrusted CORS origin was allowed")
        evidence["untrusted_cors"] = str(cors.status_code)

        basic = client.get("/api/v1/users")
        _expect_status(basic, 401, "anonymous basic-auth protected path")
        if "WWW-Authenticate" not in basic.headers:
            raise RunnerError("basic-auth protected path did not challenge")
        evidence["basic_auth"] = str(basic.status_code)

        anonymous = client.get("/api/v1/kb/documents")
        _expect_status(anonymous, 401, "anonymous KB list")
        anonymous_search = client.post("/api/v1/kb/search/raw", json={"query": marker})
        _expect_status(anonymous_search, 401, "anonymous KB search")
        anonymous_upload = client.post(
            "/api/v1/kb/upload",
            files={"file": ("anonymous.txt", content, "text/plain")},
        )
        _expect_status(anonymous_upload, 401, "anonymous KB upload")
        evidence["anonymous_protected"] = "401"

        bootstrap = client.post(
            "/api/v1/auth/create-admin",
            headers={"X-Bootstrap-Token": credentials.values["BOOTSTRAP_TOKEN"]},
            json={
                "username": credentials.admin_username,
                "password": credentials.admin_password,
                "email": "g1-admin@example.test",
            },
        )
        _expect_status(bootstrap, 200, "tenant A bootstrap")
        login_a = client.post(
            "/api/v1/auth/login",
            json={"username": credentials.admin_username, "password": credentials.admin_password},
        )
        _expect_status(login_a, 200, "tenant A login")
        token_a = _token(_response_json(login_a), "tenant A login")
        auth_a = {"Authorization": f"Bearer {token_a}"}
        me = client.get("/api/v1/auth/me", headers=auth_a)
        _expect_status(me, 200, "tenant A authenticated read")
        if _response_json(me).get("username") != credentials.admin_username:
            raise RunnerError("tenant A authenticated read returned the wrong principal")

        login_b = client.post(
            "/api/v1/auth/login",
            json={
                "username": credentials.tenant_b_username,
                "password": credentials.tenant_b_password,
            },
        )
        _expect_status(login_b, 200, "tenant B login")
        token_b = _token(_response_json(login_b), "tenant B login")
        auth_b = {"Authorization": f"Bearer {token_b}"}

        upload_a = client.post(
            "/api/v1/kb/upload",
            headers=auth_a,
            files={"file": ("g1-tenant-a.txt", content, "text/plain")},
            data={"knowledge_base_id": str(credentials.kb_a)},
        )
        _expect_status(upload_a, 200, "tenant A upload")
        docs_a = _documents(_response_json(client.get("/api/v1/kb/documents", headers=auth_a)))
        doc_a = next(
            (doc for doc in docs_a if doc.get("source") == "g1-tenant-a.txt"),
            None,
        )
        if doc_a is None or not isinstance(doc_a.get("doc_id"), str):
            raise RunnerError("tenant A upload was not readable from the tenant A document list")
        doc_id = str(doc_a["doc_id"])
        search_a = client.post(
            "/api/v1/kb/search/raw",
            headers=auth_a,
            json={"query": marker},
        )
        _expect_status(search_a, 200, "tenant A search")
        results = _response_json(search_a).get("results")
        if not isinstance(results, list) or not any(
            isinstance(item, dict) and marker in str(item.get("content", "")) for item in results
        ):
            raise RunnerError("tenant A search did not return its synthetic document")
        evidence["tenant_a_upload_list_search_read"] = "passed"

        cross_b_to_a = client.post(
            "/api/v1/kb/upload",
            headers=auth_b,
            files={"file": ("cross-tenant.txt", content, "text/plain")},
            data={"knowledge_base_id": str(credentials.kb_a)},
        )
        _expect_status(cross_b_to_a, 403, "tenant B upload to tenant A KB")
        cross_a_to_b = client.post(
            "/api/v1/kb/upload",
            headers=auth_a,
            files={"file": ("cross-kb.txt", content, "text/plain")},
            data={"knowledge_base_id": str(credentials.kb_b)},
        )
        _expect_status(cross_a_to_b, 403, "tenant A upload to tenant B KB")
        docs_b = _documents(_response_json(client.get("/api/v1/kb/documents", headers=auth_b)))
        if any(
            doc.get("doc_id") == doc_id or doc.get("source") == "g1-tenant-a.txt" for doc in docs_b
        ):
            raise RunnerError("tenant B document list exposed tenant A data")
        search_b = client.post(
            "/api/v1/kb/search/raw",
            headers=auth_b,
            json={"query": marker},
        )
        _expect_status(search_b, 200, "tenant B cross-tenant search")
        results_b = _response_json(search_b).get("results")
        if not isinstance(results_b, list):
            raise RunnerError("tenant B search response results was not a list; fail closed")
        if any(
            isinstance(item, dict) and marker in str(item.get("content", "")) for item in results_b
        ):
            raise RunnerError("tenant B search exposed tenant A content")
        delete_b = client.delete(f"/api/v1/kb/documents/{doc_id}", headers=auth_b)
        _expect_status(delete_b, 404, "tenant B cross-tenant delete")
        evidence["tenant_b_cross_tenant_kb_negatives"] = "passed"

        delete_a = client.delete(f"/api/v1/kb/documents/{doc_id}", headers=auth_a)
        _expect_status(delete_a, 200, "tenant A delete")
        evidence["tenant_a_delete"] = "passed"
    return evidence


def _cleanup(
    target: DockerTarget,
    paths: RunPaths,
    project: str,
    credentials: SyntheticCredentials,
    deadline: float,
) -> ResourceSnapshot:
    if not project.startswith(PROJECT_PREFIX):
        raise RunnerError("refusing cleanup for a project outside the owned prefix")
    result = _compose_command(
        target,
        paths,
        project,
        ("down", "--volumes", "--remove-orphans"),
        deadline=deadline,
        timeout=COMMAND_TIMEOUT_SECONDS,
        credentials=credentials,
    )
    if result.code != 0:
        raise RunnerError("owned Compose cleanup failed")
    snapshot = _resource_snapshot(target, deadline)
    if (
        project in snapshot.containers
        or project in snapshot.networks
        or project in snapshot.volumes
    ):
        raise RunnerError("owned Compose resources remain after cleanup")
    return snapshot


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_report(
    paths: RunPaths,
    *,
    status: str,
    blocker: str,
    project: str,
    evidence: Mapping[str, str],
    cleanup: ResourceSnapshot | None,
    credentials: SyntheticCredentials | None,
) -> None:
    artifact_lines = []
    for path in (
        paths.env_file,
        paths.override_file,
        paths.cert_dir / "fullchain.pem",
        paths.htpasswd_file,
    ):
        if path.is_file():
            artifact_lines.append(f"- `{path.name}` sha256 `{_hash_file(path)}`")
    lines = [
        f"# G1 model-cache reuse report ({RUN_ID})",
        "",
        f"- Compose project: `{project}`",
        f"- Exact external model volume: `{MODEL_VOLUME_NAME}`",
        f"- Exact model lock: `{MODEL_NAME}` revision `{MODEL_REVISION}`",
        "- The model volume was inspected for its exact Docker Compose project and volume labels.",
        "- `embedding-model-init` uses provisioner `verify` only; the model volume is read-only in both services.",
        "- Synthetic credentials and TLS artifacts were runtime-local and are not printed.",
        "- Repository `.env` was not read; only the generated `synthetic.env` was passed with `--env-file`.",
        "- The matrix requires real tenant A/B bearer tokens and fails closed on any auth mismatch.",
        "",
        "## Evidence",
        *(f"- {key}: `{value}`" for key, value in sorted(evidence.items())),
        "",
        "## Runtime artifacts",
        *artifact_lines,
        "",
        "## Cleanup",
        (
            "- Owned containers, networks, and volumes: zero"
            if cleanup is not None
            else "- Cleanup evidence unavailable"
        ),
        "",
        "## Changed files",
        "- `backend/scripts/run-g1-live-security-matrix.py`",
        "- `backend/tests/unit/test_g1_live_security_matrix_contract.py`",
        "- `.agent-team/control-plane/.runtime/run-20261010-g1-live-security/g1-model-reuse-retry-checkpoint.md`",
        "",
        "## Next live command",
        "- `uv run backend/scripts/run-g1-live-security-matrix.py --confirm-g1-live-security`",
        "",
        f"Blocker: {blocker or 'none'}",
        f"{REPORT_MARKER} status={status} blocker={blocker or 'none'}",
    ]
    paths.report_file.write_text(_redact("\n".join(lines) + "\n", credentials), encoding="utf-8")


def run_matrix() -> int:
    root = _repo_root()
    paths = _make_paths(root)
    paths.runtime_root.mkdir(parents=True, exist_ok=True)
    project = f"{PROJECT_PREFIX}{secrets.token_hex(6)}"
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
    docker = shutil.which("docker")
    docker_target: DockerTarget | None = None
    evidence: dict[str, str] = {}
    operation_error: RunnerError | None = None
    cleanup_error: RunnerError | None = None
    cleanup_snapshot: ResourceSnapshot | None = None
    before: ResourceSnapshot | None = None
    credentials: SyntheticCredentials | None = None
    project_claimed = False
    try:
        if docker is None:
            raise RunnerError("docker executable was not found in PATH")
        _assert_no_production_environment()
        docker_target = _assert_local_docker_target(docker, deadline)
        _verify_external_model_volume(docker_target, root, deadline)
        evidence["embedding_model_reuse"] = "verified_external_read_only"
        http_port, https_port = _reserve_loopback_ports()
        credentials = _credentials(http_port, https_port)
        _write_env(paths, credentials)
        _write_synthetic_tls(paths, credentials, deadline)
        _write_override(paths, project, http_port, https_port)
        before = _resource_snapshot(docker_target, deadline)
        if project in before.containers or project in before.networks or project in before.volumes:
            raise RunnerError("generated Compose project label was already present")
        rendered = _rendered_json(docker_target, paths, project, credentials, deadline)
        _validate_topology(rendered, root, project, http_port, https_port)
        evidence["compose_render"] = "passed"
        project_claimed = True
        _require_healthy_stack(docker_target, paths, project, credentials, deadline)
        evidence["secure_stack_health"] = "passed"
        _seed_tenant_b(docker_target, paths, project, credentials, deadline)
        evidence.update(_run_http_matrix(https_port, http_port, credentials, deadline))
    except RunnerError as error:
        operation_error = error
    except Exception as error:  # noqa: BLE001 - fail closed at the runner boundary
        operation_error = RunnerError(f"live security matrix failed closed: {type(error).__name__}")
    finally:
        if docker_target is not None and credentials is not None and project_claimed:
            try:
                cleanup_deadline = _cleanup_deadline(deadline)
                cleanup_snapshot = _cleanup(
                    docker_target,
                    paths,
                    project,
                    credentials,
                    cleanup_deadline,
                )
            except (OSError, RunnerError) as error:
                cleanup_error = RunnerError(f"owned Compose cleanup failed: {type(error).__name__}")
    if before is not None and docker_target is not None and cleanup_snapshot is not None:
        try:
            _assert_no_side_effects(before, cleanup_snapshot, project)
            evidence["preexisting_projects"] = "unchanged"
        except RunnerError as error:
            cleanup_error = error
    blocker = ""
    if operation_error is not None:
        blocker = _redact(str(operation_error), credentials)
    if cleanup_error is not None:
        blocker = _redact(str(cleanup_error), credentials)
    status = "passed" if not blocker else "blocked"
    _write_report(
        paths,
        status=status,
        blocker=blocker,
        project=project,
        evidence=evidence,
        cleanup=cleanup_snapshot,
        credentials=credentials,
    )
    print(f"{REPORT_MARKER} status={status} blocker={blocker or 'none'}", flush=True)
    return 0 if status == "passed" else 1


def _assert_no_production_environment() -> None:
    for key in ("ENVIRONMENT", "APP_ENV", "APP_ENVIRONMENT", "NODE_ENV"):
        if os.environ.get(key, "").strip().casefold() in {"prod", "production", "live"}:
            raise RunnerError(f"refusing to run with production environment marker: {key}")


def main(arguments: Sequence[str] | None = None) -> int:
    """Require an explicit confirmation flag before any Docker operation."""
    provided = tuple(sys.argv[1:] if arguments is None else arguments)
    if provided != (CONFIRM_FLAG,):
        print(f"Pass {CONFIRM_FLAG} to run the isolated G1 live security matrix.", file=sys.stderr)
        return 2
    try:
        return run_matrix()
    except (OSError, RunnerError) as error:
        print(f"{REPORT_MARKER} status=blocked blocker={type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
