#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "minio>=7.2.0",
# ]
# ///

# ─── How to run ───
# 1. Install uv (if not installed):
#      curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. Run directly (no venv, no pip install needed):
#      uv run run-p1-12-minio-restore-drill.py --confirm-isolated-restore-test
# 3. Or make executable and run:
#      chmod +x run-p1-12-minio-restore-drill.py && ./run-p1-12-minio-restore-drill.py --confirm-isolated-restore-test
# ──────────────────

from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from minio import Minio

COMPOSE_FILENAME: Final = "docker-compose.p1-12-minio-restore-test.yml"
BUCKET_NAME: Final = "p1-12-restore-drill"
OBJECT_KEY: Final = "synthetic/p1-12-object.bin"
SYNTHETIC_OBJECT_BYTES: Final = (
    b"esRAGPro P1-12 MinIO restore sentinel\n"
    b"This fixture is synthetic and contains no uploaded-file data.\n"
)
PRODUCTION_ENVIRONMENT_NAMES: Final[tuple[str, ...]] = (
    "ENVIRONMENT",
    "APP_ENV",
    "APP_ENVIRONMENT",
    "ESRAGPRO_ENVIRONMENT",
    "NODE_ENV",
)
HASH_CHUNK_SIZE: Final = 64 * 1024


class DrillError(RuntimeError):
    """Raised when the isolated restore drill cannot prove its contract."""


@dataclass(frozen=True, slots=True)
class ObjectInventory:
    """The observable inventory of the drill bucket."""

    count: int
    key: str
    size: int


def _production_environment_name() -> str | None:
    """Return the production marker name, if the process is production-like."""
    for name in PRODUCTION_ENVIRONMENT_NAMES:
        value = os.environ.get(name)
        if value and value.strip().casefold() in {"production", "prod"}:
            return name
    return None


def _require_non_production() -> None:
    """Reject execution when a known environment marker says production."""
    marker_name = _production_environment_name()
    if marker_name is not None:
        raise DrillError(
            f"P1-12 restore drill refuses production environment marker {marker_name}."
        )


def _synthetic_credentials() -> tuple[str, str]:
    """Create credentials that are valid only for this ephemeral drill."""
    return f"p1_{secrets.token_hex(12)}", secrets.token_hex(24)


def _reserve_loopback_ports() -> tuple[int, int]:
    """Reserve two distinct ephemeral ports on the IPv4 loopback interface."""
    reserved_sockets: list[socket.socket] = []
    try:
        for _ in range(2):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind(("127.0.0.1", 0))
            except OSError:
                listener.close()
                raise
            reserved_sockets.append(listener)

        source_port, target_port = (int(listener.getsockname()[1]) for listener in reserved_sockets)
        if source_port == target_port:
            raise DrillError("Could not reserve distinct source and target MinIO ports.")
        return source_port, target_port
    finally:
        for listener in reserved_sockets:
            listener.close()


def _compose_environment(
    access_key: str,
    secret_key: str,
    source_port: int,
    target_port: int,
) -> dict[str, str]:
    """Copy the process environment and add only drill-scoped settings."""
    environment = os.environ.copy()
    environment["P1_12_MINIO_ACCESS_KEY"] = access_key
    environment["P1_12_MINIO_SECRET_KEY"] = secret_key
    environment["P1_12_SOURCE_MINIO_PORT"] = str(source_port)
    environment["P1_12_TARGET_MINIO_PORT"] = str(target_port)
    return environment


def _run_compose(
    docker_executable: str,
    compose_file: Path,
    project: str,
    arguments: Sequence[str],
    environment: Mapping[str, str],
) -> None:
    """Run a Compose command without exposing its captured output."""
    command = [docker_executable, "compose", "-p", project, "-f", compose_file, *arguments]
    result = subprocess.run(
        command, check=False, capture_output=True, text=True, env=dict(environment)
    )
    if result.returncode != 0:
        action = arguments[0] if arguments else "compose"
        raise DrillError(f"Docker Compose {action} failed with exit code {result.returncode}.")


def _compose_port(
    docker_executable: str,
    compose_file: Path,
    project: str,
    service: str,
    environment: Mapping[str, str],
    expected_port: int,
) -> int:
    """Read one Compose binding and require the expected IPv4 loopback port."""
    command = [
        docker_executable,
        "compose",
        "-p",
        project,
        "-f",
        compose_file,
        "port",
        service,
        "9000",
    ]
    result = subprocess.run(
        command, check=False, capture_output=True, text=True, env=dict(environment)
    )
    if result.returncode != 0:
        raise DrillError(f"Could not discover the {service} MinIO port.")
    bindings = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
    if len(bindings) != 1:
        raise DrillError(f"Expected one {service} MinIO port binding.")
    match = re.fullmatch(r"127\.0\.0\.1:(?P<port>[0-9]+)", bindings[0])
    if match is None:
        raise DrillError(f"The {service} MinIO port is not loopback-only.")
    port = int(match.group("port"))
    if port != expected_port:
        raise DrillError(
            f"The {service} MinIO port was published on {port}, not reserved port {expected_port}."
        )
    return port


def _sha256_file(path: Path) -> tuple[int, str]:
    """Return the byte count and SHA-256 digest of a local artifact."""
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as artifact:
        while chunk := artifact.read(HASH_CHUNK_SIZE):
            byte_count += len(chunk)
            digest.update(chunk)
    return byte_count, digest.hexdigest()


def _bucket_inventory(client: Minio) -> ObjectInventory:
    """Return the single synthetic object inventory from a MinIO client."""
    objects = tuple(client.list_objects(BUCKET_NAME, recursive=True))
    if len(objects) != 1:
        raise DrillError(f"Expected one object in bucket {BUCKET_NAME}, got {len(objects)}.")
    item = objects[0]
    if item.object_name != OBJECT_KEY or item.size is None:
        raise DrillError(f"Bucket {BUCKET_NAME} does not contain the expected object inventory.")
    return ObjectInventory(count=len(objects), key=item.object_name, size=item.size)


def _read_object_sha256(client: Minio) -> tuple[int, str]:
    """Read the restored object bytes and return their count and SHA-256 digest."""
    response = client.get_object(BUCKET_NAME, OBJECT_KEY)
    digest = hashlib.sha256()
    byte_count = 0
    try:
        while chunk := response.read(HASH_CHUNK_SIZE):
            byte_count += len(chunk)
            digest.update(chunk)
    finally:
        response.close()
        response.release_conn()
    return byte_count, digest.hexdigest()


def run_drill() -> None:
    """Run the isolated source-to-target MinIO object restore drill."""
    _require_non_production()
    docker_executable = shutil.which("docker")
    if docker_executable is None:
        raise DrillError("docker executable was not found in PATH.")

    compose_file = Path(__file__).resolve().parents[1] / COMPOSE_FILENAME
    if not compose_file.is_file():
        raise DrillError(f"Missing drill Compose file: {compose_file.name}.")

    project = f"esrag-p1-12-{uuid.uuid4().hex[:12]}"
    access_key, secret_key = _synthetic_credentials()
    reserved_source_port, reserved_target_port = _reserve_loopback_ports()
    environment = _compose_environment(
        access_key,
        secret_key,
        reserved_source_port,
        reserved_target_port,
    )

    with tempfile.TemporaryDirectory(prefix="p1-12-minio-restore-") as temporary_directory:
        artifact = Path(temporary_directory) / "source-object.bin"
        try:
            _run_compose(
                docker_executable,
                compose_file,
                project,
                ("up", "-d", "--wait"),
                environment,
            )

            source_port = _compose_port(
                docker_executable,
                compose_file,
                project,
                "source_minio",
                environment,
                reserved_source_port,
            )
            target_port = _compose_port(
                docker_executable,
                compose_file,
                project,
                "target_minio",
                environment,
                reserved_target_port,
            )
            if source_port == target_port:
                raise DrillError("Source and target MinIO services share a host port.")

            source = Minio(f"127.0.0.1:{source_port}", access_key, secret_key, secure=False)
            target = Minio(f"127.0.0.1:{target_port}", access_key, secret_key, secure=False)

            source.make_bucket(BUCKET_NAME)
            source.put_object(
                BUCKET_NAME,
                OBJECT_KEY,
                io.BytesIO(SYNTHETIC_OBJECT_BYTES),
                len(SYNTHETIC_OBJECT_BYTES),
                content_type="application/octet-stream",
            )
            source_inventory = _bucket_inventory(source)
            source_stat = source.stat_object(BUCKET_NAME, OBJECT_KEY)
            if source_stat.size != len(SYNTHETIC_OBJECT_BYTES):
                raise DrillError("Source object size does not match the synthetic fixture.")

            source.fget_object(BUCKET_NAME, OBJECT_KEY, str(artifact))
            source_size, source_sha256 = _sha256_file(artifact)
            if source_size != source_stat.size:
                raise DrillError("Downloaded source bytes do not match source metadata.")

            target.make_bucket(BUCKET_NAME)
            target.fput_object(
                BUCKET_NAME,
                OBJECT_KEY,
                str(artifact),
                content_type="application/octet-stream",
            )
            target_inventory = _bucket_inventory(target)
            target_stat = target.stat_object(BUCKET_NAME, OBJECT_KEY)
            if (
                target_inventory.count != source_inventory.count
                or target_inventory.key != source_inventory.key
                or target_stat.size != source_size
            ):
                raise DrillError("Restored MinIO bucket, key, count, or size differs from source.")

            target_size, target_sha256 = _read_object_sha256(target)
            if target_size != source_size or target_sha256 != source_sha256:
                raise DrillError(
                    "Restored target bytes do not match the downloaded source SHA-256."
                )

            print(
                "P1-12 MinIO restore integrity verified: "
                f"bucket={BUCKET_NAME} key={OBJECT_KEY} count={target_inventory.count} "
                f"bytes={target_size} source-sha256={source_sha256} target-sha256={target_sha256}",
                flush=True,
            )
        finally:
            _run_compose(
                docker_executable,
                compose_file,
                project,
                ("down", "-v", "--remove-orphans"),
                environment,
            )


def main(arguments: Sequence[str] | None = None) -> int:
    """Require explicit confirmation before entering the Docker-backed drill."""
    provided_arguments = tuple(sys.argv[1:] if arguments is None else arguments)
    if provided_arguments != ("--confirm-isolated-restore-test",):
        print(
            "Pass --confirm-isolated-restore-test to run the isolated MinIO restore drill.",
            file=sys.stderr,
        )
        return 2
    try:
        run_drill()
    except Exception as error:  # noqa: BLE001, BROAD_EXCEPT_OK
        print(f"P1-12 MinIO restore drill failed: {type(error).__name__}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
