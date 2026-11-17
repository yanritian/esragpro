#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "minio>=7.2.0",
# ]
# ///

"""Run the isolated P1-14 cross-store backup, restore, and observed-RPO drill."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from minio import Minio

COMPOSE_FILENAME: Final = "docker-compose.p1-14-cross-store-restore-test.yml"
PROJECT_PREFIX: Final = "esrag-p1-14-cross-store-restore-"

SOURCE_DATABASE: Final = "p1_14_source"
TARGET_DATABASE: Final = "p1_14_target"
AUTHORITY_TABLE: Final = "p1_14_document_authority"
ES_INDEX: Final = "p1-14-document-projection"
ES_REPOSITORY: Final = "p1-14-checkpoint-repository"
ES_SNAPSHOT: Final = "p1-14-checkpoint"
NEO4J_LABEL: Final = "P1_14DocumentProjection"
MINIO_BUCKET: Final = "p1-14-cross-store"

ENTITY_ID: Final = "p1-14-entity-001"
TENANT_ID: Final = "p1-14-tenant-001"
KB_ID: Final = "p1-14-kb-001"
OBJECT_KEY: Final = (
    "tenant/p1-14-tenant-001/kb/p1-14-kb-001/entity/"
    "p1-14-entity-001/document.bin"
)
UPLOAD_PATH: Final = (
    "uploads/p1-14-tenant-001/p1-14-kb-001/p1-14-entity-001/document.bin"
)
ACL_SUBJECT: Final = "group:p1-14-operators"
ACL_ROLE: Final = "reader"
ACL_SCOPE: Final = "tenant"
SYNTHETIC_FILE_BYTES: Final = (
    b"esRAGPro P1-14 cross-store restore sentinel\n"
    b"This deterministic fixture is synthetic and contains no customer data.\n"
    b"entity=p1-14-entity-001\n"
)
CONTENT_SHA256: Final = hashlib.sha256(SYNTHETIC_FILE_BYTES).hexdigest()

PG_DUMP_PATH: Final = "/backups/postgres/p1-14-authority.dump"
PG_BACKUP_ARTIFACT_PATH: Final = "/backup/postgres/p1-14-authority.dump"
NEO4J_DUMP_PATH: Final = "/backup/neo4j.dump"
MINIO_BACKUP_PATH: Final = "/backup/minio/p1-14-object.bin"
UPLOADS_ARCHIVE_PATH: Final = "/backup/uploads.tar.gz"
BACKUP_MANIFEST_PATH: Final = "/backup/p1-14-backup-manifest.json"

MAX_OBSERVED_RPO_SECONDS: Final = 900.0
HASH_CHUNK_SIZE: Final = 64 * 1024
COMPOSE_TIMEOUT_SECONDS: Final = 180

PRODUCTION_ENVIRONMENT_NAMES: Final[tuple[str, ...]] = (
    "ENVIRONMENT",
    "APP_ENV",
    "APP_ENVIRONMENT",
    "ESRAGPRO_ENVIRONMENT",
    "DEPLOY_ENV",
    "NODE_ENV",
    "ASPNETCORE_ENVIRONMENT",
)

PORT_SPECS: Final[tuple[tuple[str, int, str], ...]] = (
    ("source_postgres", 5432, "P1_14_SOURCE_POSTGRES_PORT"),
    ("target_postgres", 5432, "P1_14_TARGET_POSTGRES_PORT"),
    ("source_elasticsearch", 9200, "P1_14_SOURCE_ELASTICSEARCH_PORT"),
    ("target_elasticsearch", 9200, "P1_14_TARGET_ELASTICSEARCH_PORT"),
    ("source_neo4j", 7474, "P1_14_SOURCE_NEO4J_HTTP_PORT"),
    ("source_neo4j", 7687, "P1_14_SOURCE_NEO4J_BOLT_PORT"),
    ("target_neo4j", 7474, "P1_14_TARGET_NEO4J_HTTP_PORT"),
    ("target_neo4j", 7687, "P1_14_TARGET_NEO4J_BOLT_PORT"),
    ("source_minio", 9000, "P1_14_SOURCE_MINIO_PORT"),
    ("target_minio", 9000, "P1_14_TARGET_MINIO_PORT"),
)

SOURCE_SERVICES: Final[tuple[str, ...]] = (
    "source_postgres",
    "source_elasticsearch",
    "source_neo4j",
    "source_minio",
)
TARGET_SERVICES: Final[tuple[str, ...]] = (
    "target_postgres",
    "target_elasticsearch",
    "target_neo4j",
    "target_minio",
)
RECORD_FIELDS: Final[tuple[str, ...]] = (
    "entity_id",
    "tenant_id",
    "kb_id",
    "object_key",
    "upload_path",
    "acl_subject",
    "acl_role",
    "acl_scope",
    "content_sha256",
    "content_bytes",
)


class DrillError(RuntimeError):
    """Raised when the isolated drill cannot prove its contract."""


@dataclass(frozen=True, slots=True)
class CrossStoreRecord:
    """The one authority/projection record copied across every store."""

    entity_id: str
    tenant_id: str
    kb_id: str
    object_key: str
    upload_path: str
    acl_subject: str
    acl_role: str
    acl_scope: str
    content_sha256: str
    content_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "entity_id": self.entity_id,
            "tenant_id": self.tenant_id,
            "kb_id": self.kb_id,
            "object_key": self.object_key,
            "upload_path": self.upload_path,
            "acl_subject": self.acl_subject,
            "acl_role": self.acl_role,
            "acl_scope": self.acl_scope,
            "content_sha256": self.content_sha256,
            "content_bytes": self.content_bytes,
        }


EXPECTED_RECORD: Final = CrossStoreRecord(
    entity_id=ENTITY_ID,
    tenant_id=TENANT_ID,
    kb_id=KB_ID,
    object_key=OBJECT_KEY,
    upload_path=UPLOAD_PATH,
    acl_subject=ACL_SUBJECT,
    acl_role=ACL_ROLE,
    acl_scope=ACL_SCOPE,
    content_sha256=CONTENT_SHA256,
    content_bytes=len(SYNTHETIC_FILE_BYTES),
)


def _utc_now() -> datetime:
    """Return an aware UTC timestamp for the backup-set manifest."""
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _record_sha256(record: CrossStoreRecord) -> str:
    canonical = json.dumps(record.as_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _production_environment_name() -> str | None:
    """Return the process marker that explicitly identifies production/live."""
    for name in PRODUCTION_ENVIRONMENT_NAMES:
        value = os.environ.get(name, "")
        if value.strip().casefold() in {"production", "prod", "live"}:
            return name
    return None


def _require_non_production() -> None:
    """Refuse to run if process or Compose context looks production-scoped."""
    marker_name = _production_environment_name()
    if marker_name is not None:
        raise DrillError(f"P1-14 restore drill refuses production marker {marker_name}.")

    compose_project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    if re.search(
        r"(?:^|[-_])(prod|production|live)(?:[-_]|$)",
        compose_project,
        re.IGNORECASE,
    ):
        raise DrillError("P1-14 restore drill refuses a production-scoped Compose project.")


def _synthetic_credentials() -> dict[str, str]:
    """Return short-lived credentials that are never included in proof output."""
    return {
        "P1_14_POSTGRES_PASSWORD": f"p114-{secrets.token_hex(16)}",
        "P1_14_NEO4J_PASSWORD": f"p114-{secrets.token_hex(16)}",
        "P1_14_MINIO_ACCESS_KEY": f"p114{secrets.token_hex(10)}",
        "P1_14_MINIO_SECRET_KEY": secrets.token_hex(24),
    }


def _reserve_loopback_ports() -> dict[str, int]:
    """Reserve one distinct ephemeral IPv4 loopback port for each binding."""
    listeners: list[socket.socket] = []
    ports: dict[str, int] = {}
    try:
        for _service, _container_port, environment_name in PORT_SPECS:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
            except OSError:
                listener.close()
                raise
            listeners.append(listener)
            ports[environment_name] = int(listener.getsockname()[1])
    finally:
        for listener in listeners:
            listener.close()

    if len(set(ports.values())) != len(PORT_SPECS):
        raise DrillError("Loopback port reservation returned duplicate ports.")
    return ports


def _compose_environment(
    project: str,
    credentials: Mapping[str, str],
    ports: Mapping[str, int],
) -> dict[str, str]:
    """Copy the environment and add only this drill's credentials and ports."""
    environment = os.environ.copy()
    environment.update(credentials)
    for _service, _container_port, environment_name in PORT_SPECS:
        try:
            environment[environment_name] = str(ports[environment_name])
        except KeyError as error:
            raise DrillError(f"Missing reserved port for {environment_name}.") from error
    environment["P1_14_COMPOSE_PROJECT"] = project
    return environment


def _compose_command(
    docker_executable: str,
    compose_file: Path,
    project: str,
    arguments: Sequence[str],
) -> list[str]:
    return [
        docker_executable,
        "compose",
        "--progress",
        "quiet",
        "-p",
        project,
        "-f",
        str(compose_file),
        *arguments,
    ]


def _run_compose(
    docker_executable: str,
    compose_file: Path,
    project: str,
    arguments: Sequence[str],
    environment: Mapping[str, str],
    *,
    operation: str,
    input_bytes: bytes | None = None,
) -> bytes:
    """Run one scoped Compose command without forwarding output or secrets."""
    command = _compose_command(docker_executable, compose_file, project, arguments)
    if input_bytes is None:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            timeout=COMPOSE_TIMEOUT_SECONDS,
        )
    else:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            env=dict(environment),
            input=input_bytes,
            timeout=COMPOSE_TIMEOUT_SECONDS,
        )
    if result.returncode != 0:
        details = _redacted_compose_error(result.stdout, result.stderr, environment)
        suffix = f": {details}" if details else ""
        raise DrillError(
            f"Docker Compose operation '{operation}' failed with exit code {result.returncode}{suffix}"
        )
    return bytes(result.stdout)


def _compose_port(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    service: str,
    container_port: int,
    expected_port: int,
) -> int:
    """Require one exact IPv4 loopback binding for a started service."""
    output = _run_compose(
        docker_executable,
        compose_file,
        project,
        ("port", service, str(container_port)),
        environment,
        operation=f"discover {service} loopback port {container_port}",
    )
    bindings = tuple(
        line.strip() for line in output.decode("utf-8").splitlines() if line.strip()
    )
    if len(bindings) != 1:
        raise DrillError(f"Expected one {service} port binding.")
    match = re.fullmatch(r"127\.0\.0\.1:(?P<port>[0-9]+)", bindings[0])
    if match is None:
        raise DrillError(f"The {service} port is not loopback-only.")
    actual_port = int(match.group("port"))
    if actual_port != expected_port:
        raise DrillError(f"The {service} port did not match its reserved port.")
    return actual_port


def _compose_exec(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    service: str,
    command: Sequence[str],
    *,
    operation: str,
    user: str | None = None,
    input_bytes: bytes | None = None,
) -> bytes:
    arguments: list[str] = ["exec", "-T"]
    if user is not None:
        arguments.extend(("--user", user))
    arguments.extend((service, *command))
    return _run_compose(
        docker_executable,
        compose_file,
        project,
        arguments,
        environment,
        operation=operation,
        input_bytes=input_bytes,
    )


def _compose_run(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    service: str,
    command: Sequence[str],
    *,
    operation: str,
    user: str | None = None,
) -> bytes:
    arguments: list[str] = ["run", "--rm", "--no-deps", "-T"]
    if user is not None:
        arguments.extend(("--user", user))
    arguments.extend((service, *command))
    return _run_compose(
        docker_executable,
        compose_file,
        project,
        arguments,
        environment,
        operation=operation,
    )


def _decode(output: bytes) -> str:
    return output.decode("utf-8", errors="replace").strip()


def _redacted_compose_error(
    stdout: bytes,
    stderr: bytes,
    environment: Mapping[str, str],
) -> str:
    """Return a short Compose diagnostic with drill credentials removed."""
    details = _decode(stderr or stdout)
    for name in (
        "P1_14_POSTGRES_PASSWORD",
        "P1_14_NEO4J_PASSWORD",
        "P1_14_MINIO_ACCESS_KEY",
        "P1_14_MINIO_SECRET_KEY",
    ):
        value = environment.get(name)
        if value:
            details = details.replace(value, "<redacted>")
    return details[:800]


def _backup_file_bytes(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    path: str,
) -> bytes:
    return _compose_exec(
        docker_executable,
        compose_file,
        project,
        environment,
        "restore_tool",
        ("cat", path),
        operation=f"read backup artifact {path}",
    )


def _write_backup_file(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    path: str,
    payload: bytes,
) -> None:
    script = f"set -eu; cat > {shlex.quote(path)}"
    _compose_exec(
        docker_executable,
        compose_file,
        project,
        environment,
        "restore_tool",
        ("sh", "-ec", script),
        operation=f"write backup artifact {path}",
        input_bytes=payload,
    )


def _prepare_shared_volumes(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
) -> None:
    script = """
set -eu
    mkdir -p /backup/postgres /backup/minio
    rm -rf /backup/postgres/* /backup/minio/* /snapshots/*
rm -f /backup/neo4j.dump /backup/uploads.tar.gz /backup/p1-14-backup-manifest.json
chmod -R 0777 /backup /source /target
"""
    _compose_exec(
        docker_executable,
        compose_file,
        project,
        environment,
        "restore_tool",
        ("sh", "-ec", script),
        operation="prepare owned backup and uploads volumes",
    )


def _safe_relative_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or "\x00" in normalized
    ):
        raise DrillError("Synthetic upload path is not a safe relative path.")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise DrillError("Synthetic upload path contains an unsafe path component.")
    return normalized


def _write_source_upload(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
) -> None:
    relative_path = _safe_relative_path(UPLOAD_PATH)
    absolute_path = f"/source/{relative_path}"
    directory = posixpath.dirname(absolute_path)
    script = (
        f"set -eu; mkdir -p {shlex.quote(directory)}; "
        f"cat > {shlex.quote(absolute_path)}"
    )
    _compose_exec(
        docker_executable,
        compose_file,
        project,
        environment,
        "restore_tool",
        ("sh", "-ec", script),
        operation="write synthetic source upload",
        input_bytes=SYNTHETIC_FILE_BYTES,
    )


def _read_upload(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    root: str,
) -> bytes:
    relative_path = _safe_relative_path(UPLOAD_PATH)
    return _compose_exec(
        docker_executable,
        compose_file,
        project,
        environment,
        "restore_tool",
        ("cat", f"/{root}/{relative_path}"),
        operation=f"read {root} upload bytes",
    )


def _validate_archive_entries(entries: Sequence[str]) -> None:
    safe_files: set[str] = set()
    for raw_entry in entries:
        normalized = raw_entry.strip().replace("\\", "/")
        if not normalized:
            continue
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            raise DrillError("Uploads archive contains an absolute path.")
        relative = re.sub(r"^(?:\./)+", "", normalized)
        if not relative:
            continue
        safe = _safe_relative_path(relative.rstrip("/"))
        if not normalized.endswith("/"):
            safe_files.add(safe)
    if safe_files != {UPLOAD_PATH}:
        raise DrillError("Uploads archive entries do not match the one synthetic upload path.")


def _capture_upload_archive(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
) -> bytes:
    backup_command = """
set -eu
rm -f /backup/uploads.tar.gz
tar -czf /backup/uploads.tar.gz -C /source .
test -s /backup/uploads.tar.gz
"""
    _compose_exec(
        docker_executable,
        compose_file,
        project,
        environment,
        "restore_tool",
        ("sh", "-ec", backup_command),
        operation="capture uploads tar backup",
    )
    listing = _decode(
        _compose_exec(
            docker_executable,
            compose_file,
            project,
            environment,
            "restore_tool",
            ("sh", "-ec", "tar -tzf /backup/uploads.tar.gz"),
            operation="validate uploads tar entries",
        )
    )
    _validate_archive_entries(listing.splitlines())
    return _backup_file_bytes(
        docker_executable,
        compose_file,
        project,
        environment,
        UPLOADS_ARCHIVE_PATH,
    )


def _restore_upload_archive(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
) -> None:
    restore_command = """
set -eu
for entry in /target/* /target/.[!.]* /target/..?*; do
  [ -e "$entry" ] || [ -L "$entry" ] || continue
  rm -rf "$entry"
done
tar -xzf /backup/uploads.tar.gz -C /target
"""
    _compose_exec(
        docker_executable,
        compose_file,
        project,
        environment,
        "restore_tool",
        ("sh", "-ec", restore_command),
        operation="restore uploads tar backup",
    )


def _sha256_bytes(payload: bytes) -> str:
    digest = hashlib.sha256()
    for offset in range(0, len(payload), HASH_CHUNK_SIZE):
        digest.update(payload[offset : offset + HASH_CHUNK_SIZE])
    return digest.hexdigest()


def _artifact_info(payload: bytes) -> dict[str, object]:
    return {"bytes": len(payload), "sha256": _sha256_bytes(payload)}


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _pg_query(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    database: str,
    query: str,
) -> str:
    service = "source_postgres" if database == SOURCE_DATABASE else "target_postgres"
    return _decode(
        _compose_exec(
            docker_executable,
            compose_file,
            project,
            environment,
            service,
            (
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-d",
                database,
                "-At",
                "-F",
                "\t",
                "-c",
                query,
            ),
            operation=f"query {database} authority",
            user="postgres",
        )
    )


def _seed_postgres(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
) -> None:
    values = EXPECTED_RECORD
    query = f"""
CREATE TABLE IF NOT EXISTS public.{AUTHORITY_TABLE} (
    entity_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    kb_id text NOT NULL,
    object_key text NOT NULL UNIQUE,
    upload_path text NOT NULL UNIQUE,
    acl_subject text NOT NULL,
    acl_role text NOT NULL,
    acl_scope text NOT NULL,
    content_sha256 text NOT NULL,
    content_bytes bigint NOT NULL
);
TRUNCATE public.{AUTHORITY_TABLE};
INSERT INTO public.{AUTHORITY_TABLE} (
    entity_id, tenant_id, kb_id, object_key, upload_path,
    acl_subject, acl_role, acl_scope, content_sha256, content_bytes
) VALUES (
    {_sql_literal(values.entity_id)},
    {_sql_literal(values.tenant_id)},
    {_sql_literal(values.kb_id)},
    {_sql_literal(values.object_key)},
    {_sql_literal(values.upload_path)},
    {_sql_literal(values.acl_subject)},
    {_sql_literal(values.acl_role)},
    {_sql_literal(values.acl_scope)},
    {_sql_literal(values.content_sha256)},
    {values.content_bytes}
);
"""
    _compose_exec(
        docker_executable,
        compose_file,
        project,
        environment,
        "source_postgres",
        (
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            SOURCE_DATABASE,
            "-c",
            query,
        ),
        operation="seed PostgreSQL authority",
        user="postgres",
    )


def _record_from_values(values: Sequence[str]) -> CrossStoreRecord:
    if len(values) != len(RECORD_FIELDS):
        raise DrillError("A store returned an unexpected cross-store record shape.")
    try:
        return CrossStoreRecord(
            entity_id=values[0],
            tenant_id=values[1],
            kb_id=values[2],
            object_key=values[3],
            upload_path=values[4],
            acl_subject=values[5],
            acl_role=values[6],
            acl_scope=values[7],
            content_sha256=values[8],
            content_bytes=int(values[9]),
        )
    except (TypeError, ValueError) as error:
        raise DrillError("A store returned an invalid cross-store record.") from error


def _get_pg_record(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    database: str,
) -> CrossStoreRecord:
    count = _pg_query(
        docker_executable,
        compose_file,
        project,
        environment,
        database,
        f"SELECT COUNT(*) FROM public.{AUTHORITY_TABLE};",
    )
    if count != "1":
        raise DrillError("PostgreSQL authority count is not exactly one.")
    line = _pg_query(
        docker_executable,
        compose_file,
        project,
        environment,
        database,
        (
            "SELECT "
            + ", ".join(RECORD_FIELDS)
            + f" FROM public.{AUTHORITY_TABLE} ORDER BY entity_id;"
        ),
    )
    rows = [row for row in line.splitlines() if row.strip()]
    if len(rows) != 1:
        raise DrillError("PostgreSQL authority IDs are not deterministic.")
    return _record_from_values(rows[0].split("\t"))


def _capture_postgres_dump(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
) -> bytes:
    _compose_exec(
        docker_executable,
        compose_file,
        project,
        environment,
        "source_postgres",
        (
            "pg_dump",
            "-U",
            "postgres",
            "--no-owner",
            "--no-acl",
            "-Fc",
            "-f",
            PG_DUMP_PATH,
            SOURCE_DATABASE,
        ),
        operation="capture PostgreSQL custom-format dump",
        user="postgres",
    )
    payload = _backup_file_bytes(
        docker_executable,
        compose_file,
        project,
        environment,
        PG_BACKUP_ARTIFACT_PATH,
    )
    if not payload:
        raise DrillError("PostgreSQL backup artifact is empty.")
    return payload


def _restore_postgres_dump(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    dump_bytes: bytes,
) -> None:
    _compose_exec(
        docker_executable,
        compose_file,
        project,
        environment,
        "target_postgres",
        (
            "pg_restore",
            "-U",
            "postgres",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            "-d",
            TARGET_DATABASE,
        ),
        operation="restore PostgreSQL custom-format dump",
        user="postgres",
        input_bytes=dump_bytes,
    )


def _es_request(
    base_url: str,
    method: str,
    path: str,
    body: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        error.close()
        raise DrillError(f"Elasticsearch {method} request returned HTTP {error.code}.") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DrillError(
            f"Elasticsearch {method} request could not reach the isolated store."
        ) from error
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DrillError("Elasticsearch returned invalid JSON.") from error
    if not isinstance(parsed, dict):
        raise DrillError("Elasticsearch returned a non-object JSON response.")
    return parsed


def _wait_for_es(base_url: str) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            health = _es_request(base_url, "GET", "/_cluster/health")
            if str(health.get("status", "")) in {"green", "yellow"}:
                return
        except DrillError:
            pass
        time.sleep(2)
    raise DrillError("Elasticsearch did not become ready within the drill timeout.")


def _configure_es_source(source_url: str) -> None:
    _es_request(
        source_url,
        "PUT",
        f"/_snapshot/{ES_REPOSITORY}?verify=true",
        {
            "type": "fs",
            "settings": {"location": "/snapshots", "compress": True},
        },
    )


def _seed_es(source_url: str) -> None:
    properties = {
        field: {"type": "long" if field == "content_bytes" else "keyword"}
        for field in RECORD_FIELDS
    }
    _es_request(
        source_url,
        "PUT",
        f"/{ES_INDEX}",
        {"mappings": {"properties": properties}},
    )
    _es_request(
        source_url,
        "PUT",
        f"/{ES_INDEX}/_doc/{ENTITY_ID}?refresh=true",
        EXPECTED_RECORD.as_dict(),
    )


def _get_es_record(base_url: str) -> CrossStoreRecord:
    result = _es_request(
        base_url,
        "POST",
        f"/{ES_INDEX}/_search?size=10&track_total_hits=true",
        {"query": {"match_all": {}}, "sort": [{"entity_id": "asc"}]},
    )
    hits = result.get("hits")
    if not isinstance(hits, dict):
        raise DrillError("Elasticsearch response omitted hits.")
    total = hits.get("total")
    count = int(total.get("value", -1)) if isinstance(total, dict) else int(total or -1)
    records = hits.get("hits")
    if count != 1 or not isinstance(records, list) or len(records) != 1:
        raise DrillError("Elasticsearch projection count is not exactly one.")
    hit = records[0]
    if not isinstance(hit, dict) or str(hit.get("_id")) != ENTITY_ID:
        raise DrillError("Elasticsearch projection ID does not match the deterministic entity.")
    source = hit.get("_source")
    if not isinstance(source, dict):
        raise DrillError("Elasticsearch projection omitted _source.")
    values = [str(source.get(field, "")) for field in RECORD_FIELDS]
    return _record_from_values(values)


def _capture_es_snapshot(source_url: str) -> None:
    result = _es_request(
        source_url,
        "PUT",
        f"/_snapshot/{ES_REPOSITORY}/{ES_SNAPSHOT}?wait_for_completion=true",
        {"indices": ES_INDEX, "include_global_state": False},
    )
    snapshots = result.get("snapshots")
    snapshot = result.get("snapshot")
    snapshot_state = snapshot.get("state") if isinstance(snapshot, dict) else None
    if isinstance(snapshots, list) and snapshots and isinstance(snapshots[0], dict):
        snapshot_state = snapshots[0].get("state", snapshot_state)
    if str(snapshot_state) != "SUCCESS":
        raise DrillError("Elasticsearch filesystem snapshot did not complete successfully.")


def _restore_es_snapshot(target_url: str) -> None:
    _es_request(
        target_url,
        "PUT",
        f"/_snapshot/{ES_REPOSITORY}?verify=false",
        {
            "type": "fs",
            "settings": {"location": "/snapshots", "readonly": True},
        },
    )
    _es_request(
        target_url,
        "POST",
        f"/_snapshot/{ES_REPOSITORY}/{ES_SNAPSHOT}/_restore?wait_for_completion=true",
        {
            "indices": ES_INDEX,
            "include_global_state": False,
            "index_settings": {"index.number_of_replicas": 0},
        },
    )


def _cypher(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    service: str,
    password: str,
    query: str,
    *,
    operation: str,
) -> str:
    output = _compose_exec(
        docker_executable,
        compose_file,
        project,
        environment,
        service,
        (
            "cypher-shell",
            "--format",
            "plain",
            "--username",
            "neo4j",
            "--password",
            password,
            query,
        ),
        operation=operation,
    )
    return _decode(output)


def _parse_cypher_plain_rows(output: str, fields: Sequence[str]) -> list[dict[str, str]]:
    lines = [line for line in output.splitlines() if line.strip()]
    required = set(fields)
    for header_index, line in enumerate(lines):
        headers = [
            header.strip()
            for header in next(csv.reader([line], skipinitialspace=True))
        ]
        if not required.issubset(headers):
            continue
        reader = csv.DictReader(
            io.StringIO("\n".join(lines[header_index:])),
            skipinitialspace=True,
        )
        return [
            {str(key).strip(): (value or "").strip() for key, value in row.items()}
            for row in reader
        ]
    raise DrillError("Neo4j returned invalid plain output.")


def _get_neo4j_record(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    service: str,
    password: str,
) -> CrossStoreRecord:
    query = """
MATCH (entity:P1_14DocumentProjection)
RETURN entity.entity_id AS entity_id,
       entity.tenant_id AS tenant_id,
       entity.kb_id AS kb_id,
       entity.object_key AS object_key,
       entity.upload_path AS upload_path,
       entity.acl_subject AS acl_subject,
       entity.acl_role AS acl_role,
       entity.acl_scope AS acl_scope,
       entity.content_sha256 AS content_sha256,
       entity.content_bytes AS content_bytes
ORDER BY entity.entity_id
"""
    output = _cypher(
        docker_executable,
        compose_file,
        project,
        environment,
        service,
        password,
        query,
        operation=f"read {service} Neo4j projection",
    )
    rows = _parse_cypher_plain_rows(output, RECORD_FIELDS)
    if len(rows) != 1:
        raise DrillError("Neo4j projection count is not exactly one.")
    return _record_from_values([rows[0].get(field, "") for field in RECORD_FIELDS])


def _seed_neo4j(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    password: str,
) -> None:
    values = EXPECTED_RECORD
    query = f"""
MATCH (old:P1_14DocumentProjection) DETACH DELETE old;
CREATE (entity:P1_14DocumentProjection {{
    entity_id: {_sql_literal(values.entity_id)},
    tenant_id: {_sql_literal(values.tenant_id)},
    kb_id: {_sql_literal(values.kb_id)},
    object_key: {_sql_literal(values.object_key)},
    upload_path: {_sql_literal(values.upload_path)},
    acl_subject: {_sql_literal(values.acl_subject)},
    acl_role: {_sql_literal(values.acl_role)},
    acl_scope: {_sql_literal(values.acl_scope)},
    content_sha256: {_sql_literal(values.content_sha256)},
    content_bytes: {values.content_bytes}
}});
"""
    _cypher(
        docker_executable,
        compose_file,
        project,
        environment,
        "source_neo4j",
        password,
        query,
        operation="seed Neo4j projection",
    )


def _capture_neo4j_dump(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
) -> bytes:
    _compose_run(
        docker_executable,
        compose_file,
        project,
        environment,
        "source_neo4j",
        (
            "neo4j-admin",
            "database",
            "dump",
            "neo4j",
            "--to-path=/var/lib/neo4j/import",
        ),
        operation="offline Neo4j database dump",
        user="neo4j",
    )
    payload = _backup_file_bytes(
        docker_executable,
        compose_file,
        project,
        environment,
        "/backup/neo4j.dump",
    )
    if not payload:
        raise DrillError("Neo4j offline dump artifact is empty.")
    return payload


def _restore_neo4j_dump(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
) -> None:
    _compose_run(
        docker_executable,
        compose_file,
        project,
        environment,
        "target_neo4j",
        (
            "neo4j-admin",
            "database",
            "load",
            "--from-path=/var/lib/neo4j/import",
            "neo4j",
            "--overwrite-destination=true",
        ),
        operation="offline Neo4j database load",
        user="neo4j",
    )


def _minio_inventory(client: Minio) -> tuple[int, str, int]:
    objects = tuple(client.list_objects(MINIO_BUCKET, recursive=True))
    if len(objects) != 1:
        raise DrillError("MinIO object count is not exactly one.")
    item = objects[0]
    if item.object_name != OBJECT_KEY or item.size is None:
        raise DrillError("MinIO object key or size does not match the deterministic fixture.")
    return len(objects), item.object_name, int(item.size)


def _read_minio_object(client: Minio) -> bytes:
    response = client.get_object(MINIO_BUCKET, OBJECT_KEY)
    chunks: list[bytes] = []
    try:
        while chunk := response.read(HASH_CHUNK_SIZE):
            chunks.append(chunk)
    finally:
        response.close()
        response.release_conn()
    return b"".join(chunks)


def _seed_minio(client: Minio) -> None:
    client.make_bucket(MINIO_BUCKET)
    client.put_object(
        MINIO_BUCKET,
        OBJECT_KEY,
        io.BytesIO(SYNTHETIC_FILE_BYTES),
        len(SYNTHETIC_FILE_BYTES),
        content_type="application/octet-stream",
    )


def _restore_minio(client: Minio, payload: bytes) -> None:
    client.make_bucket(MINIO_BUCKET)
    client.put_object(
        MINIO_BUCKET,
        OBJECT_KEY,
        io.BytesIO(payload),
        len(payload),
        content_type="application/octet-stream",
    )


def _wait_for_postgres(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    service: str,
    database: str,
) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            _compose_exec(
                docker_executable,
                compose_file,
                project,
                environment,
                service,
                ("pg_isready", "-U", "postgres", "-d", database),
                operation=f"wait for {service}",
            )
            return
        except DrillError:
            time.sleep(2)
    raise DrillError(f"{service} did not become ready within the drill timeout.")


def _wait_for_neo4j(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    service: str,
    password: str,
) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            _cypher(
                docker_executable,
                compose_file,
                project,
                environment,
                service,
                password,
                "RETURN 1 AS ready",
                operation=f"wait for {service}",
            )
            return
        except DrillError:
            time.sleep(3)
    raise DrillError(f"{service} did not become ready within the drill timeout.")


def _wait_for_minio(client: Minio) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            client.list_buckets()
            return
        except Exception:  # noqa: BLE001 - readiness retries intentionally hide client details
            time.sleep(2)
    raise DrillError("MinIO did not become ready within the drill timeout.")


def _validate_record(label: str, actual: CrossStoreRecord, expected: CrossStoreRecord) -> None:
    actual_values = actual.as_dict()
    expected_values = expected.as_dict()
    mismatched_fields = [
        field for field in RECORD_FIELDS if actual_values[field] != expected_values[field]
    ]
    if mismatched_fields:
        raise DrillError(f"{label} mismatched synthetic fields: {', '.join(mismatched_fields)}.")


def _validate_bytes(label: str, actual: bytes, expected: bytes) -> None:
    if len(actual) != len(expected) or _sha256_bytes(actual) != _sha256_bytes(expected):
        raise DrillError(f"{label} bytes or SHA-256 differ from the source fixture.")


def _observed_rpo_seconds(
    checkpoint: datetime,
    artifact_times: Mapping[str, datetime],
) -> float:
    if not artifact_times:
        raise DrillError("Backup set has no timestamped artifacts.")
    ages = [
        (captured_at - checkpoint).total_seconds()
        for captured_at in artifact_times.values()
    ]
    if any(age < 0 for age in ages):
        raise DrillError("Backup artifact timestamp preceded the quiesced checkpoint.")
    return round(max(ages), 3)


def _validate_loopback_bindings(
    docker_executable: str,
    compose_file: Path,
    project: str,
    environment: Mapping[str, str],
    services: Sequence[str],
) -> None:
    for service, container_port, environment_name in PORT_SPECS:
        if service not in services:
            continue
        _compose_port(
            docker_executable,
            compose_file,
            project,
            environment,
            service,
            container_port,
            int(environment[environment_name]),
        )


def run_drill() -> tuple[str, ...]:
    """Run the complete isolated source-to-target cross-store drill."""
    _require_non_production()
    docker_executable = shutil.which("docker")
    if docker_executable is None:
        raise DrillError("docker executable was not found in PATH.")

    compose_file = Path(__file__).resolve().parents[1] / COMPOSE_FILENAME
    if not compose_file.is_file():
        raise DrillError(f"Missing drill Compose file: {compose_file.name}.")

    project = f"{PROJECT_PREFIX}{uuid.uuid4().hex[:12]}"
    credentials = _synthetic_credentials()
    ports = _reserve_loopback_ports()
    environment = _compose_environment(project, credentials, ports)
    compose_started = False
    operation_error: Exception | None = None
    cleanup_error: Exception | None = None

    try:
        _run_compose(
            docker_executable,
            compose_file,
            project,
            ("config", "--quiet"),
            environment,
            operation="validate isolated Compose configuration",
        )
        compose_started = True
        _run_compose(
            docker_executable,
            compose_file,
            project,
            ("up", "-d", "--wait", "restore_tool", *SOURCE_SERVICES),
            environment,
            operation="start isolated source stores",
        )
        _validate_loopback_bindings(
            docker_executable, compose_file, project, environment, SOURCE_SERVICES
        )
        _prepare_shared_volumes(docker_executable, compose_file, project, environment)

        source_es_port = int(environment["P1_14_SOURCE_ELASTICSEARCH_PORT"])
        source_es_url = f"http://127.0.0.1:{source_es_port}"
        source_minio_port = int(environment["P1_14_SOURCE_MINIO_PORT"])
        source_minio = Minio(
            f"127.0.0.1:{source_minio_port}",
            credentials["P1_14_MINIO_ACCESS_KEY"],
            credentials["P1_14_MINIO_SECRET_KEY"],
            secure=False,
        )

        _wait_for_postgres(
            docker_executable,
            compose_file,
            project,
            environment,
            "source_postgres",
            SOURCE_DATABASE,
        )
        _wait_for_es(source_es_url)
        _wait_for_neo4j(
            docker_executable,
            compose_file,
            project,
            environment,
            "source_neo4j",
            credentials["P1_14_NEO4J_PASSWORD"],
        )
        _wait_for_minio(source_minio)

        _seed_postgres(docker_executable, compose_file, project, environment)
        _configure_es_source(source_es_url)
        _seed_es(source_es_url)
        _seed_neo4j(
            docker_executable,
            compose_file,
            project,
            environment,
            credentials["P1_14_NEO4J_PASSWORD"],
        )
        _write_source_upload(docker_executable, compose_file, project, environment)
        _seed_minio(source_minio)

        source_pg_record = _get_pg_record(
            docker_executable, compose_file, project, environment, SOURCE_DATABASE
        )
        source_es_record = _get_es_record(source_es_url)
        source_neo4j_record = _get_neo4j_record(
            docker_executable,
            compose_file,
            project,
            environment,
            "source_neo4j",
            credentials["P1_14_NEO4J_PASSWORD"],
        )
        for label, record in (
            ("source PostgreSQL", source_pg_record),
            ("source Elasticsearch", source_es_record),
            ("source Neo4j", source_neo4j_record),
        ):
            _validate_record(label, record, EXPECTED_RECORD)

        source_object_bytes = _read_minio_object(source_minio)
        source_upload_bytes = _read_upload(
            docker_executable, compose_file, project, environment, "source"
        )
        _validate_bytes("source MinIO object", source_object_bytes, SYNTHETIC_FILE_BYTES)
        _validate_bytes("source upload", source_upload_bytes, SYNTHETIC_FILE_BYTES)
        object_count, object_key, object_size = _minio_inventory(source_minio)
        if object_count != 1 or object_key != OBJECT_KEY or object_size != len(SYNTHETIC_FILE_BYTES):
            raise DrillError("Source MinIO inventory does not match the deterministic fixture.")

        # No source writes occur after this checkpoint.  The store-native artifacts
        # therefore represent one coherent synthetic backup set.
        checkpoint_at = _utc_now()
        backup_started_at = _utc_now()
        artifact_times: dict[str, datetime] = {}

        postgres_dump_bytes = _capture_postgres_dump(
            docker_executable, compose_file, project, environment
        )
        artifact_times["postgres_dump"] = _utc_now()

        _capture_es_snapshot(source_es_url)
        artifact_times["elasticsearch_snapshot"] = _utc_now()
        _compose_exec(
            docker_executable,
            compose_file,
            project,
            environment,
            "restore_tool",
            (
                "sh",
                "-ec",
                (
                    "test -d /snapshots && "
                    "find /snapshots -type f | grep -q ."
                ),
            ),
            operation="verify Elasticsearch snapshot artifact",
        )

        _write_backup_file(
            docker_executable,
            compose_file,
            project,
            environment,
            MINIO_BACKUP_PATH,
            source_object_bytes,
        )
        minio_backup_bytes = _backup_file_bytes(
            docker_executable, compose_file, project, environment, MINIO_BACKUP_PATH
        )
        _validate_bytes("MinIO object backup", minio_backup_bytes, source_object_bytes)
        artifact_times["minio_object"] = _utc_now()

        uploads_archive_bytes = _capture_upload_archive(
            docker_executable, compose_file, project, environment
        )
        artifact_times["uploads_tar"] = _utc_now()

        # Neo4j's native dump is offline.  Stop all source stores before taking it
        # and before starting any target store to keep memory bounded.
        _run_compose(
            docker_executable,
            compose_file,
            project,
            ("stop", *SOURCE_SERVICES),
            environment,
            operation="quiesce and stop source stores",
        )
        neo4j_dump_bytes = _capture_neo4j_dump(
            docker_executable, compose_file, project, environment
        )
        artifact_times["neo4j_dump"] = _utc_now()

        backup_completed_at = _utc_now()
        artifact_times["backup_manifest"] = backup_completed_at
        checkpoint_to_backup_seconds = round(
            (backup_completed_at - checkpoint_at).total_seconds(), 3
        )
        observed_restore_rpo_seconds = _observed_rpo_seconds(checkpoint_at, artifact_times)
        if observed_restore_rpo_seconds > MAX_OBSERVED_RPO_SECONDS:
            raise DrillError("Observed checkpoint-to-backup RPO exceeded 900 seconds.")

        manifest = {
            "format": "esragpro-p1-14-cross-store-backup-v1",
            "synthetic_fixture": True,
            "checkpoint_at": _timestamp(checkpoint_at),
            "backup_started_at": _timestamp(backup_started_at),
            "backup_completed_at": _timestamp(backup_completed_at),
            "observed_checkpoint_to_backup_seconds": checkpoint_to_backup_seconds,
            "observed_restore_rpo_seconds": observed_restore_rpo_seconds,
            "rpo_limit_seconds": MAX_OBSERVED_RPO_SECONDS,
            "record_sha256": _record_sha256(EXPECTED_RECORD),
            "entity": EXPECTED_RECORD.as_dict(),
            "artifacts": {
                "postgres_dump": {
                    "path": PG_BACKUP_ARTIFACT_PATH,
                    **_artifact_info(postgres_dump_bytes),
                },
                "elasticsearch_snapshot": {
                    "repository": ES_REPOSITORY,
                    "snapshot": ES_SNAPSHOT,
                    "path": "/snapshots",
                },
                "neo4j_dump": {"path": "/backup/neo4j.dump", **_artifact_info(neo4j_dump_bytes)},
                "minio_object": {
                    "path": MINIO_BACKUP_PATH,
                    **_artifact_info(minio_backup_bytes),
                },
                "uploads_tar": {
                    "path": UPLOADS_ARCHIVE_PATH,
                    **_artifact_info(uploads_archive_bytes),
                },
            },
        }
        manifest_bytes = json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        _write_backup_file(
            docker_executable,
            compose_file,
            project,
            environment,
            BACKUP_MANIFEST_PATH,
            manifest_bytes,
        )
        if _backup_file_bytes(
            docker_executable, compose_file, project, environment, BACKUP_MANIFEST_PATH
        ) != manifest_bytes:
            raise DrillError("Backup-set manifest did not round-trip through the owned volume.")

        # Initialize the target Neo4j data volume while all sources are already
        # stopped, then load the offline database before starting the final target set.
        _run_compose(
            docker_executable,
            compose_file,
            project,
            ("up", "-d", "target_neo4j"),
            environment,
            operation="initialize target Neo4j data volume",
        )
        _wait_for_neo4j(
            docker_executable,
            compose_file,
            project,
            environment,
            "target_neo4j",
            credentials["P1_14_NEO4J_PASSWORD"],
        )
        _run_compose(
            docker_executable,
            compose_file,
            project,
            ("stop", "target_neo4j"),
            environment,
            operation="stop target Neo4j for offline load",
        )
        _restore_neo4j_dump(docker_executable, compose_file, project, environment)

        _run_compose(
            docker_executable,
            compose_file,
            project,
            ("up", "-d", "--wait", *TARGET_SERVICES),
            environment,
            operation="start isolated target stores after source quiescence",
        )
        _validate_loopback_bindings(
            docker_executable, compose_file, project, environment, TARGET_SERVICES
        )

        target_es_port = int(environment["P1_14_TARGET_ELASTICSEARCH_PORT"])
        target_es_url = f"http://127.0.0.1:{target_es_port}"
        target_minio_port = int(environment["P1_14_TARGET_MINIO_PORT"])
        target_minio = Minio(
            f"127.0.0.1:{target_minio_port}",
            credentials["P1_14_MINIO_ACCESS_KEY"],
            credentials["P1_14_MINIO_SECRET_KEY"],
            secure=False,
        )
        _wait_for_postgres(
            docker_executable,
            compose_file,
            project,
            environment,
            "target_postgres",
            TARGET_DATABASE,
        )
        _wait_for_es(target_es_url)
        _wait_for_neo4j(
            docker_executable,
            compose_file,
            project,
            environment,
            "target_neo4j",
            credentials["P1_14_NEO4J_PASSWORD"],
        )
        _wait_for_minio(target_minio)

        restore_started_at = _utc_now()
        _restore_postgres_dump(
            docker_executable,
            compose_file,
            project,
            environment,
            postgres_dump_bytes,
        )
        _restore_es_snapshot(target_es_url)
        _restore_minio(target_minio, minio_backup_bytes)
        _restore_upload_archive(docker_executable, compose_file, project, environment)

        target_pg_record = _get_pg_record(
            docker_executable, compose_file, project, environment, TARGET_DATABASE
        )
        target_es_record = _get_es_record(target_es_url)
        target_neo4j_record = _get_neo4j_record(
            docker_executable,
            compose_file,
            project,
            environment,
            "target_neo4j",
            credentials["P1_14_NEO4J_PASSWORD"],
        )
        for label, record in (
            ("restored PostgreSQL", target_pg_record),
            ("restored Elasticsearch", target_es_record),
            ("restored Neo4j", target_neo4j_record),
        ):
            _validate_record(label, record, EXPECTED_RECORD)
            if _record_sha256(record) != _record_sha256(EXPECTED_RECORD):
                raise DrillError(f"{label} canonical record hash differs from the source.")

        target_object_bytes = _read_minio_object(target_minio)
        target_upload_bytes = _read_upload(
            docker_executable, compose_file, project, environment, "target"
        )
        _validate_bytes("restored MinIO object", target_object_bytes, SYNTHETIC_FILE_BYTES)
        _validate_bytes("restored upload", target_upload_bytes, SYNTHETIC_FILE_BYTES)
        target_object_count, target_object_key, target_object_size = _minio_inventory(target_minio)
        if (
            target_object_count != object_count
            or target_object_key != object_key
            or target_object_size != object_size
        ):
            raise DrillError("Restored MinIO count, key, or size differs from source.")
        _validate_bytes("restored MinIO object against source", target_object_bytes, source_object_bytes)
        _validate_bytes("restored upload against source", target_upload_bytes, source_upload_bytes)

        restore_completed_at = _utc_now()
        restore_elapsed_seconds = round(
            (restore_completed_at - restore_started_at).total_seconds(), 3
        )
        return (
            "P1-14 cross-store backup/restore integrity verified: PASS",
            f"Compose project: {project}",
            f"Synthetic entity: {ENTITY_ID}",
            f"Cross-store record count: 1; record-sha256: {_record_sha256(EXPECTED_RECORD)}",
            f"Object key and upload path matched; bytes={len(SYNTHETIC_FILE_BYTES)}; sha256={CONTENT_SHA256}",
            (
                "PG custom dump, Elasticsearch filesystem snapshot, Neo4j offline dump, "
                "MinIO object, and uploads tar restored successfully"
            ),
            f"Observed checkpoint-to-backup-seconds: {checkpoint_to_backup_seconds}",
            f"Observed restore-RPO-seconds: {observed_restore_rpo_seconds} (limit={MAX_OBSERVED_RPO_SECONDS})",
            f"Restore elapsed-seconds: {restore_elapsed_seconds}",
            "RPO observation covers this synthetic checkpoint only; it does not establish production backup cadence.",
        )
    except Exception as error:  # noqa: BLE001 - cleanup must run for every drill failure
        operation_error = error
    finally:
        if compose_started:
            try:
                _run_compose(
                    docker_executable,
                    compose_file,
                    project,
                    ("down", "-v", "--remove-orphans"),
                    environment,
                    operation="cleanup owned P1-14 Compose project",
                )
            except Exception as error:  # noqa: BLE001 - report cleanup after the attempt
                cleanup_error = error

    if cleanup_error is not None:
        if operation_error is not None:
            raise DrillError("P1-14 drill failed and owned-resource cleanup also failed.") from cleanup_error
        raise DrillError("P1-14 drill completed but owned-resource cleanup failed.") from cleanup_error
    if operation_error is not None:
        raise operation_error
    raise DrillError("P1-14 drill ended without a result.")


def main(arguments: Sequence[str] | None = None) -> int:
    """Require explicit confirmation before entering the Docker-backed drill."""
    provided_arguments = tuple(sys.argv[1:] if arguments is None else arguments)
    if provided_arguments != ("--confirm-isolated-restore-test",):
        print(
            "Pass --confirm-isolated-restore-test to run the isolated P1-14 cross-store restore drill.",
            file=sys.stderr,
        )
        return 2
    try:
        for proof_line in run_drill():
            print(proof_line, flush=True)
    except Exception as error:  # noqa: BLE001 - CLI reports a redacted failure summary
        print(
            f"P1-14 cross-store restore drill failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
