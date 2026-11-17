#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

# ─── How to run ───
# 1. Install uv (if not installed):
#      curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. Run directly (no venv, no pip install needed):
#      uv run run-p1-15-upgrade-rollback-drill.py --confirm-isolated-upgrade-rollback-test
# 3. Or make executable and run:
#      chmod +x run-p1-15-upgrade-rollback-drill.py && ./run-p1-15-upgrade-rollback-drill.py --confirm-isolated-upgrade-rollback-test
# ──────────────────

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, TypeAlias

COMPOSE_FILENAME: Final = "docker-compose.p1-15-upgrade-rollback-test.yml"
OLD_IMAGE: Final = "esragpro_auth_20260921-backend:latest"
DATABASE: Final = "p1_15_upgrade"
SENTINEL_TABLE: Final = "p1_15_upgrade_rollback_sentinel"
SENTINEL_VALUE: Final = "p1-15-upgrade-rollback-sentinel"
CONFIRM_FLAG: Final = "--confirm-isolated-upgrade-rollback-test"
COMMAND_TIMEOUT_SECONDS: Final = 180.0
BUILD_TIMEOUT_SECONDS: Final = 1800.0
SHA256_DIGEST: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
PRODUCTION_NAMES: Final = (
    "ENVIRONMENT",
    "APP_ENV",
    "APP_ENVIRONMENT",
    "ESRAGPRO_ENVIRONMENT",
    "NODE_ENV",
)


class DrillError(RuntimeError):
    pass


JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class DrillContext:
    docker: str
    compose_file: Path
    project: str
    root: Path
    environment: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PhaseEvidence:
    name: str
    image: str
    image_digest: str
    expected_heads: tuple[str, ...]
    applied_heads: tuple[str, ...]
    data_hash: str
    health_status: str
    ready_status: str
    auth_negative_status: int
    elapsed_seconds: float


def _require_non_production() -> None:
    for name in PRODUCTION_NAMES:
        if os.environ.get(name, "").strip().casefold() in {"prod", "production", "live"}:
            raise DrillError(f"P1-15 drill refuses production marker {name}.")


def _run(context: DrillContext, command: Sequence[str], stage: str, timeout: float) -> bytes:
    if timeout <= 0:
        raise DrillError(f"{stage} has an invalid timeout.")
    try:
        result = subprocess.run(
            [context.docker, *command],
            check=False,
            capture_output=True,
            env=dict(context.environment),
            cwd=context.root,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise DrillError(f"{stage} timed out after {timeout:.1f}s.") from error
    except OSError as error:
        raise DrillError(f"{stage} could not start: {type(error).__name__}.") from error
    if result.returncode != 0:
        raise DrillError(f"{stage} failed with exit code {result.returncode}.")
    return result.stdout


def _compose(context: DrillContext, arguments: Sequence[str], stage: str) -> bytes:
    prefix = ("compose", "--progress", "quiet", "--project-name", context.project, "--file", str(context.compose_file))
    return _run(context, (*prefix, *arguments), stage, COMMAND_TIMEOUT_SECONDS)


def _with_image(context: DrillContext, image: str) -> DrillContext:
    environment = dict(context.environment)
    environment["P1_15_BACKEND_IMAGE"] = image
    return DrillContext(context.docker, context.compose_file, context.project, context.root, environment)


def _digest(context: DrillContext, image: str) -> str:
    output = _run(context, ("image", "inspect", image, "--format", "{{.Id}}"), "image inspect", 30.0)
    digest = output.decode().strip()
    if SHA256_DIGEST.fullmatch(digest) is None:
        raise DrillError(f"Image {image} did not return a valid immutable sha256 digest.")
    return digest


def _build_candidate(context: DrillContext, image: str) -> str:
    _run(context, ("build", "--file", "backend/Dockerfile", "--tag", image, "backend"), "docker build candidate image", BUILD_TIMEOUT_SECONDS)
    return _digest(context, image)


def _heads(context: DrillContext) -> tuple[str, ...]:
    output = _compose(context, ("run", "--rm", "--no-deps", "backend", "alembic", "heads"), "Alembic heads").decode(errors="replace")
    heads = tuple(sorted(set(re.findall(r"(?m)^([A-Za-z0-9_.-]+)\s+\(head\)\s*$", output))))
    if not heads:
        raise DrillError("Alembic returned no migration heads.")
    return heads


def _applied_heads(context: DrillContext) -> tuple[str, ...]:
    output = _psql(context, "SELECT version_num FROM alembic_version ORDER BY version_num;")
    return tuple(sorted(filter(None, output.splitlines())))


def _psql(context: DrillContext, query: str) -> str:
    args = ("exec", "--no-TTY", "postgres", "psql", "-U", "postgres", "-d", DATABASE, "-v", "ON_ERROR_STOP=1", "-At", "-c", query)
    output = _compose(context, args, "PostgreSQL probe")
    return output.decode(errors="replace").strip()


def _seed_sentinel(context: DrillContext) -> None:
    query = f"CREATE TABLE IF NOT EXISTS public.{SENTINEL_TABLE} (id integer PRIMARY KEY, payload text NOT NULL); TRUNCATE public.{SENTINEL_TABLE}; INSERT INTO public.{SENTINEL_TABLE} (id, payload) VALUES (1, '{SENTINEL_VALUE}');"
    _psql(context, query)


def _data_hash(context: DrillContext) -> str:
    value = _psql(context, f"SELECT id::text || ':' || payload FROM public.{SENTINEL_TABLE} ORDER BY id;")
    if value != f"1:{SENTINEL_VALUE}":
        raise DrillError("Sentinel PostgreSQL record changed during the binary handoff.")
    return hashlib.sha256(value.encode()).hexdigest()


def _http_status(context: DrillContext, path: str) -> str:
    args = ("exec", "--no-TTY", "backend", "curl", "--silent", "--show-error", f"http://127.0.0.1:8000{path}")
    output = _compose(context, args, f"HTTP {path}")
    try:
        payload = json.loads(output.decode(errors="replace"))
    except json.JSONDecodeError as error:
        raise DrillError(f"HTTP {path} returned invalid JSON.") from error
    if not isinstance(payload, dict):
        raise DrillError(f"HTTP {path} returned a non-object payload.")
    status = payload.get("status")
    if not isinstance(status, str):
        raise DrillError(f"HTTP {path} returned no status field.")
    return status


def _auth_negative(context: DrillContext) -> int:
    args = ("exec", "--no-TTY", "backend", "curl", "--silent", "--show-error", "--output", "/dev/null", "--write-out", "%{http_code}", "--header", "Content-Type: application/json", "--data", '{"username":"p1-15-missing-user","password":"wrong-password"}', "http://127.0.0.1:8000/api/v1/auth/login")
    output = _compose(context, args, "negative auth probe")
    try:
        status = int(output.decode().strip())
    except ValueError as error:
        raise DrillError("Negative auth probe did not return an HTTP status.") from error
    if status != 401:
        raise DrillError(f"Negative auth probe returned HTTP {status}, expected 401.")
    return status


def _observe(context: DrillContext, name: str, image: str, digest: str, expected: tuple[str, ...]) -> PhaseEvidence:
    started = time.perf_counter()
    _compose(context, ("up", "--detach", "--wait", "backend"), f"start {name} backend")
    health_status = _http_status(context, "/health")
    applied = _applied_heads(context)
    ready_status = _http_status(context, "/ready")
    if health_status != "healthy" or ready_status != "ready":
        raise DrillError(
            f"{name} backend readiness was not healthy and ready "
            f"(health={health_status!r}, ready={ready_status!r}, applied={applied!r})."
        )
    if applied != expected:
        raise DrillError(
            f"{name} applied migration heads {applied!r} do not match expected {expected!r}."
        )
    data_hash = _data_hash(context)
    auth_status = _auth_negative(context)
    return PhaseEvidence(name, image, digest, expected, applied, data_hash, str(health_status), str(ready_status), auth_status, time.perf_counter() - started)


def _resources(context: DrillContext) -> dict[str, list[str]]:
    containers = list(filter(None, _compose(context, ("ps", "--all", "--quiet"), "list containers").decode().splitlines()))
    volumes = list(filter(None, _run(context, ("volume", "ls", "--filter", f"label=com.docker.compose.project={context.project}", "--format", "{{.Name}}"), "list volumes", 30.0).decode().splitlines()))
    networks = list(filter(None, _run(context, ("network", "ls", "--filter", f"label=com.docker.compose.project={context.project}", "--format", "{{.Name}}"), "list networks", 30.0).decode().splitlines()))
    return {"containers": containers, "volumes": volumes, "networks": networks}


def _cleanup(context: DrillContext, candidate: str) -> dict[str, JsonValue]:
    evidence: dict[str, JsonValue] = {
        "before": {},
        "after": {},
        "compose_down": False,
        "candidate_image_removed": False,
        "cleanup_verified": False,
    }
    try:
        evidence["before"] = _resources(context)
    except DrillError as error:
        evidence["before_error"] = str(error)
    try:
        _compose(context, ("down", "-v", "--remove-orphans"), "owned-resource cleanup")
        evidence["compose_down"] = True
    except DrillError as error:
        evidence["cleanup_error"] = str(error)
    try:
        _run(context, ("image", "rm", "-f", candidate), "candidate image cleanup", 60.0)
        evidence["candidate_image_removed"] = True
    except DrillError as error:
        evidence["image_cleanup_error"] = str(error)
    try:
        evidence["after"] = _resources(context)
    except DrillError as error:
        evidence["after_error"] = str(error)
    before = evidence.get("before")
    after = evidence.get("after")
    evidence["cleanup_verified"] = (
        evidence["compose_down"] is True
        and evidence["candidate_image_removed"] is True
        and isinstance(before, dict)
        and isinstance(after, dict)
        and all(before.get(name) is not None for name in ("containers", "volumes", "networks"))
        and all(after.get(name) == [] for name in ("containers", "volumes", "networks"))
    )
    return evidence


def run_drill() -> int:
    _require_non_production()
    docker = shutil.which("docker")
    if docker is None:
        raise DrillError("docker executable was not found in PATH.")
    root = Path(__file__).resolve().parents[2]
    compose_file = root / "backend" / COMPOSE_FILENAME
    if not compose_file.is_file():
        raise DrillError(f"Missing drill Compose file: {compose_file.name}.")
    token = uuid.uuid4().hex[:12]
    project = f"esrag-p1-15-upgrade-rollback-{token}"
    candidate = f"esragpro-p1-15-candidate-{token}:latest"
    environment = os.environ.copy()
    environment.update({
        "P1_15_BACKEND_IMAGE": OLD_IMAGE,
        "P1_15_POSTGRES_PASSWORD": f"p115-{secrets.token_hex(16)}",
        "P1_15_JWT_SECRET": secrets.token_hex(32),
    })
    context = DrillContext(docker, compose_file, project, root, environment)
    evidence: dict[str, JsonValue] = {"project": project, "old_image": OLD_IMAGE, "candidate_image": candidate}
    phases: list[PhaseEvidence] = []
    try:
        old_digest = _digest(context, OLD_IMAGE)
        evidence["old_digest"] = old_digest
        candidate_digest = _build_candidate(context, candidate)
        evidence["candidate_digest"] = candidate_digest
        _compose(context, ("up", "--detach", "--wait", "postgres", "elasticsearch", "redis"), "start dependencies")
        old_context = _with_image(context, old_digest)
        old_heads = _heads(old_context)
        evidence["old_migration_heads"] = list(old_heads)
        _compose(old_context, ("run", "--rm", "--no-deps", "backend", "alembic", "upgrade", "head"), "old migration upgrade")
        _seed_sentinel(old_context)
        phases.append(_observe(old_context, "old-before-upgrade", OLD_IMAGE, old_digest, old_heads))
        _compose(old_context, ("stop", "backend"), "stop old backend")
        _compose(old_context, ("rm", "--force", "backend"), "remove old backend")
        candidate_context = _with_image(context, candidate_digest)
        candidate_heads = _heads(candidate_context)
        evidence["candidate_migration_heads"] = list(candidate_heads)
        evidence["rollback_schema"] = {
            "expected_heads": list(candidate_heads),
            "strategy": "retain-candidate-schema",
        }
        _compose(candidate_context, ("run", "--rm", "--no-deps", "backend", "alembic", "upgrade", "head"), "candidate migration upgrade")
        phases.append(_observe(candidate_context, "candidate-after-upgrade", candidate, candidate_digest, candidate_heads))
        _compose(candidate_context, ("stop", "backend"), "stop candidate backend")
        _compose(candidate_context, ("rm", "--force", "backend"), "remove candidate backend")
        rollback_context = _with_image(context, old_digest)
        phases.append(_observe(rollback_context, "old-after-rollback", OLD_IMAGE, old_digest, candidate_heads))
        evidence["rollback_schema_compatible"] = True
        evidence["result"] = "PASS"
    except DrillError as error:
        evidence["result"] = "FAIL"
        evidence["error"] = str(error)
    finally:
        evidence["phases"] = [asdict(phase) for phase in phases]
        cleanup = _cleanup(context, candidate)
        evidence["cleanup"] = cleanup
        if cleanup.get("cleanup_verified") is not True:
            evidence["result"] = "FAIL"
            evidence.setdefault("error", "Owned resources and candidate image cleanup were not proven complete.")
    evidence_path = Path(tempfile.gettempdir()) / f"{project}-evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print(f"P1-15 upgrade/rollback drill {evidence['result']}; evidence={evidence_path}")
    return 0 if evidence["result"] == "PASS" else 1


def main(arguments: Sequence[str] | None = None) -> int:
    provided = tuple(sys.argv[1:] if arguments is None else arguments)
    if provided != (CONFIRM_FLAG,):
        print(f"Pass {CONFIRM_FLAG} to run the isolated upgrade/rollback drill.", file=sys.stderr)
        return 2
    try:
        return run_drill()
    except DrillError as error:
        print(f"P1-15 upgrade/rollback drill failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
