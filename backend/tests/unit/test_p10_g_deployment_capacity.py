"""P10-G focused deployment/capacity boundary tests.

The P10-G seam makes the backend Uvicorn worker count an explicit
deployment parameter with a safe default of 1 (the long-standing
single-worker behavior). These tests pin the seam in the Dockerfile, the
ordinary compose override, the secure compose single-worker pin, and the
environment template so the capacity contract cannot silently regress.

They are pure text checks, mirroring the existing
``test_p6_g2_world_memory_candidate_config_compose`` pattern; they do not
require Docker or a live container.
"""

from __future__ import annotations

from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_ROOT = _REPOSITORY_ROOT / "backend"
_DOCKERFILE = _BACKEND_ROOT / "Dockerfile"
_ENV_EXAMPLE = _REPOSITORY_ROOT / ".env.example"

_COMPOSE = _REPOSITORY_ROOT / "docker-compose.yml"
_SECURE_COMPOSE = _REPOSITORY_ROOT / "docker-compose.secure.yml"


def _backend_service_block(compose_path: Path) -> str:
    compose = compose_path.read_text(encoding="utf-8")
    backend = compose.split("  backend:", 1)[1]
    if "  minio-worker:" in backend:
        backend = backend.split("  minio-worker:", 1)[0]
    return backend


def test_dockerfile_cmd_preserves_one_worker_default_and_exposes_seam() -> None:
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    cmd = dockerfile.split("CMD", 1)[1]

    # The default must stay 1: the shell form expands ${UVICORN_WORKERS:-1}.
    # (The CMD payload is JSON-escaped, so the quotes around the expansion
    # carry backslashes in the file; the bare expansion is what matters.)
    assert "${UVICORN_WORKERS:-1}" in cmd
    assert "--workers" in cmd
    # exec keeps the uvicorn master as PID 1 so shutdown signals are
    # delivered correctly even with a shell-form CMD.
    assert "exec uvicorn app.main:app" in cmd
    assert "--host" in cmd and "--port" in cmd


def test_compose_backend_service_exposes_uvicorn_workers_override_seam() -> None:
    backend = _backend_service_block(_COMPOSE)
    assert "UVICORN_WORKERS=${UVICORN_WORKERS:-1}" in backend


def test_secure_compose_backend_pins_one_uvicorn_worker_without_override() -> None:
    backend = _backend_service_block(_SECURE_COMPOSE)
    worker_entries = [
        line.strip()
        for line in backend.splitlines()
        if line.strip().startswith("- UVICORN_WORKERS=")
    ]
    assert worker_entries == ["- UVICORN_WORKERS=1"]
    assert "UVICORN_WORKERS=${" not in backend


def test_env_example_documents_uvicorn_workers_default() -> None:
    env_example = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "UVICORN_WORKERS=1" in env_example
    assert "P10-G" in env_example
    assert "The default stays 1" in env_example
    assert "Secure Compose enforces one worker" in env_example
