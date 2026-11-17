from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = PROJECT_ROOT / "backend" / "docker-compose.p1-15-upgrade-rollback-test.yml"
SCRIPT_FILE = PROJECT_ROOT / "backend" / "scripts" / "run-p1-15-upgrade-rollback-drill.py"


def _runner_module():
    spec = importlib.util.spec_from_file_location("p1_15_upgrade_rollback_runner", SCRIPT_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_compose_keeps_the_upgrade_rollback_stack_private_and_dynamic() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {"postgres", "elasticsearch", "redis", "backend"}
    assert "${P1_15_BACKEND_IMAGE" in services["backend"]["image"]
    assert set(compose["volumes"]) == {"p1_15_postgres_data", "p1_15_elasticsearch_data"}

    for service in services.values():
        assert "ports" not in service
        assert "container_name" not in service
        assert "healthcheck" in service


def test_runner_records_binary_migration_data_and_cleanup_evidence() -> None:
    source = SCRIPT_FILE.read_text(encoding="utf-8").lower()

    for fragment in (
        "--confirm-isolated-upgrade-rollback-test",
        "esragpro_auth_20260921-backend:latest",
        "docker build",
        '"alembic", "upgrade", "head"',
        "/health",
        "/ready",
        "/api/v1/auth/login",
        "sha256",
        "data_hash",
        "elapsed_seconds",
        '"down", "-v", "--remove-orphans"',
        '"image", "rm", "-f"',
        "valid immutable sha256 digest",
        "candidate_migration_heads",
        '"strategy": "retain-candidate-schema"',
        "cleanup_verified",
        "timed out after",
    ):
        assert fragment in source, f"missing P1-15 drill contract fragment: {fragment}"

    assert "ports:" not in Path(COMPOSE_FILE).read_text(encoding="utf-8")
    assert "container_name:" not in Path(COMPOSE_FILE).read_text(encoding="utf-8")


def test_digest_requires_a_full_immutable_image_id(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner_module()
    context = runner.DrillContext("docker", COMPOSE_FILE, "p1-15-test", PROJECT_ROOT, {})
    expected = "sha256:" + "a" * 64

    monkeypatch.setattr(runner, "_run", lambda *_args, **_kwargs: (expected + "\n").encode())

    assert runner._digest(context, "old-image") == expected

    monkeypatch.setattr(runner, "_run", lambda *_args, **_kwargs: b"sha256:short\n")
    with pytest.raises(runner.DrillError, match="valid immutable sha256 digest"):
        runner._digest(context, "old-image")


def test_run_reports_timeout_as_a_failed_command(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner_module()
    context = runner.DrillContext("docker", COMPOSE_FILE, "p1-15-test", PROJECT_ROOT, {})

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["docker"], 2.0)

    monkeypatch.setattr(runner.subprocess, "run", timeout)
    with pytest.raises(runner.DrillError, match="timed out after 2.0s"):
        runner._run(context, ("image", "inspect"), "image inspect", 2.0)


def test_cleanup_is_only_verified_when_owned_resources_are_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner_module()
    context = runner.DrillContext("docker", COMPOSE_FILE, "p1-15-test", PROJECT_ROOT, {})
    calls = iter(
        (
            {"containers": ["owned-container"], "volumes": ["owned-volume"], "networks": ["owned-network"]},
            {"containers": [], "volumes": [], "networks": []},
        )
    )
    monkeypatch.setattr(runner, "_resources", lambda _context: next(calls))
    monkeypatch.setattr(runner, "_compose", lambda *_args, **_kwargs: b"")
    monkeypatch.setattr(runner, "_run", lambda *_args, **_kwargs: b"")

    evidence = runner._cleanup(context, "candidate:latest")

    assert evidence["candidate_image_removed"] is True
    assert evidence["cleanup_verified"] is True
