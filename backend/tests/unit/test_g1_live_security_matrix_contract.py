from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

RUNNER = Path(__file__).resolve().parents[2] / "scripts" / "run-g1-live-security-matrix.py"


def _source() -> str:
    return RUNNER.read_text(encoding="utf-8")


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("g1_live_security_matrix_contract", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runner_has_explicit_isolation_and_runtime_artifact_contract() -> None:
    source = _source()

    assert "PROJECT_PREFIX" in source
    assert "reserve_loopback_ports" in source
    assert '"127.0.0.1"' in source
    assert "HTTP_PORT" in source
    assert "HTTPS_PORT" in source
    assert "synthetic.env" in source
    assert "certs" in source
    assert "htpasswd" in source
    assert "redact" in source.lower()


def test_runner_forbids_repository_env_reads_and_checks_current_source_builds() -> None:
    source = _source()

    assert "--env-file" in source
    assert "COMPOSE_FILENAME" in source
    assert "_validate_topology" in source
    assert "current source context" in source
    assert "Path('.env')" not in source
    assert 'root / ".env"' not in source


def test_runner_contains_fail_closed_auth_and_tenant_kb_matrix() -> None:
    source = _source()

    for marker in (
        "/api/v1/auth/create-admin",
        "/api/v1/auth/login",
        "/api/v1/auth/me",
        "/api/v1/kb/upload",
        "/api/v1/kb/documents",
        "/api/v1/kb/search/raw",
        "/api/v1/kb/documents/",
        "X-Bootstrap-Token",
        "tenant_b",
        "untrusted.example",
        "Access-Control-Request-Method",
        "fail closed",
    ):
        assert marker in source


def test_runner_uses_project_httpx_and_rejects_malformed_tenant_b_search() -> None:
    source = _source()

    assert '"httpx>=0.26.0"' in source
    assert "import httpx" in source
    assert "httpx2" not in source
    assert "http2=False" in source
    assert "if not isinstance(results_b, list):" in source
    assert "tenant B search response results was not a list; fail closed" in source


def test_runner_always_cleans_only_owned_compose_project() -> None:
    source = _source()

    assert "finally:" in source
    assert "down" in source
    assert "--volumes" in source
    assert "--remove-orphans" in source
    assert "com.docker.compose.project" in source
    assert "project.startswith(PROJECT_PREFIX)" in source
    assert "project_claimed" in source
    assert "generated Compose project label was already present" in source
    assert "preexisting" in source.lower()


def test_runner_requires_explicit_confirmation_and_never_claims_live_pass() -> None:
    source = _source()

    assert "--confirm-g1-live-security" in source
    assert "REPORT_MARKER" in source
    assert "status={status} blocker={blocker or 'none'}" in source
    assert "next live command" in source.lower()


def test_runner_rejects_nonlocal_docker_host_before_any_runtime_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()

    def credentials_must_not_be_read(
        *_args: str | float | bool | None,
        **_kwargs: str | float | bool | None,
    ) -> str | None:
        pytest.fail("Docker target validation must precede synthetic credential creation")

    monkeypatch.setattr(runner, "_credentials", credentials_must_not_be_read)
    monkeypatch.setenv("DOCKER_HOST", "ssh://remote.example")
    with pytest.raises(runner.RunnerError, match="DOCKER_HOST"):
        runner._assert_local_docker_target("docker", time.monotonic() + 5.0)


def test_runner_rejects_nonlocal_docker_context_before_docker_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-context")

    def docker_must_not_be_inspected(*_args: str, **_kwargs: str) -> runner.CommandResult:
        pytest.fail("a selected nonlocal context must fail before Docker inspection")

    monkeypatch.setattr(runner, "_run_command", docker_must_not_be_inspected)
    with pytest.raises(runner.RunnerError, match="DOCKER_CONTEXT"):
        runner._assert_local_docker_target("docker", time.monotonic() + 5.0)


def test_runner_rejects_selected_remote_context_and_reuses_explicit_local_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)

    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(
        argv: list[str],
        *,
        deadline: float,
        timeout: float,
        environment: dict[str, str],
    ) -> runner.CommandResult:
        del deadline, timeout
        calls.append((argv, environment))
        return runner.CommandResult(0, "remote-context\n", "")

    monkeypatch.setattr(runner, "_run_command", fake_run)
    with pytest.raises(runner.RunnerError, match="active Docker context"):
        runner._assert_local_docker_target("docker", time.monotonic() + 5.0)
    assert len(calls) == 1
    assert calls[0][0] == ["docker", "context", "show"]
    assert "DOCKER_HOST" not in calls[0][1]
    assert "DOCKER_CONTEXT" not in calls[0][1]


def test_runner_shares_one_explicit_local_target_for_inventory_and_compose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)

    def fake_run(
        argv: list[str],
        *,
        deadline: float,
        timeout: float,
        environment: dict[str, str],
    ) -> runner.CommandResult:
        del deadline, timeout, environment
        if argv[1:] == ["context", "show"]:
            return runner.CommandResult(0, "default\n", "")
        return runner.CommandResult(0, '"unix:///var/run/docker.sock"\n', "")

    monkeypatch.setattr(runner, "_run_command", fake_run)
    target = runner._assert_local_docker_target("docker", time.monotonic() + 5.0)
    paths = runner.RunPaths(
        runtime_root=tmp_path,
        attempt_root=tmp_path,
        env_file=tmp_path / "synthetic.env",
        override_file=tmp_path / "override.yml",
        cert_dir=tmp_path / "certs",
        certbot_dir=tmp_path / "certbot",
        htpasswd_file=tmp_path / ".htpasswd",
        report_file=tmp_path / "report.md",
        checkpoint_file=tmp_path / "checkpoint.md",
    )
    inventory = runner._docker_args(target, "ps")
    compose = runner._compose_args(target, paths, "esragpro_g1_20261010_test")
    assert inventory[:3] == ["docker", "--context", "default"]
    assert compose[:3] == inventory[:3]
    assert "DOCKER_HOST" not in target.environment
    assert "DOCKER_CONTEXT" not in target.environment


def test_runner_disables_proxy_environment_and_reserves_cleanup_grace() -> None:
    source = _source()

    assert "trust_env=False" in source
    runner = _load_runner()
    work_deadline = 100.0
    runner_time = runner.time.monotonic
    runner.time.monotonic = lambda: 200.0
    try:
        assert runner._cleanup_deadline(work_deadline) == 200.0 + runner.CLEANUP_GRACE_SECONDS
        assert runner._cleanup_deadline(300.0) == 300.0 + runner.CLEANUP_GRACE_SECONDS
    finally:
        runner.time.monotonic = runner_time
    assert "cleanup_deadline = _cleanup_deadline(deadline)" in source
    assert "cleanup_snapshot = _cleanup(" in source
    assert "owned Compose cleanup failed" in source
    assert "G1_MODEL_REUSE_RETRY_FINAL" in source


def test_runner_reuses_only_the_pinned_external_model_volume_with_verify_and_read_only_mounts(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    paths = runner.RunPaths(
        runtime_root=tmp_path,
        attempt_root=tmp_path,
        env_file=tmp_path / "synthetic.env",
        override_file=tmp_path / "override.yml",
        cert_dir=tmp_path / "certs",
        certbot_dir=tmp_path / "certbot",
        htpasswd_file=tmp_path / ".htpasswd",
        report_file=tmp_path / "report.md",
        checkpoint_file=tmp_path / "checkpoint.md",
    )
    runner._write_override(paths, "esragpro_g1_20261010_test", 18081, 18444)
    override = paths.override_file.read_text(encoding="utf-8")

    assert "      - verify\n" in override
    assert "      - acquire\n" not in override
    assert "    external: true\n" in override
    assert f"    name: {runner.MODEL_VOLUME_NAME}\n" in override
    assert "        source: embedding_model\n" in override
    assert "        target: /models\n        read_only: true\n" in override
    assert "        target: /opt/esragpro/models\n        read_only: true\n" in override
    assert "name: esragpro_g1_20261010_test_embedding_model" not in override


def test_runner_verifies_exact_model_lock_and_external_volume_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        *,
        deadline: float,
        timeout: float,
        environment: dict[str, str],
    ) -> runner.CommandResult:
        del deadline, timeout, environment
        calls.append(argv)
        return runner.CommandResult(
            0,
            json.dumps(
                {
                    "Name": runner.MODEL_VOLUME_NAME,
                    "Labels": {
                        "com.docker.compose.project": runner.MODEL_VOLUME_PROJECT,
                        "com.docker.compose.volume": runner.MODEL_VOLUME_KEY,
                    },
                }
            ),
            "",
        )

    monkeypatch.setattr(runner, "_run_command", fake_run)
    target = runner.DockerTarget("docker", "default", {})
    runner._verify_external_model_volume(target, runner._repo_root(), time.monotonic() + 5.0)

    assert calls == [
        [
            "docker",
            "--context",
            "default",
            "volume",
            "inspect",
            "--format",
            "{{json .}}",
            runner.MODEL_VOLUME_NAME,
        ]
    ]


def test_runner_rejects_model_volume_label_drift_before_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_assert_no_production_environment", lambda: None)
    target = runner.DockerTarget("docker", "default", {})
    monkeypatch.setattr(runner, "_assert_local_docker_target", lambda *_args, **_kwargs: target)

    def reuse_must_fail(*_args: object, **_kwargs: object) -> None:
        raise runner.RunnerError(
            "external embedding model volume name/project labels did not match expected identity"
        )

    def credentials_must_not_be_created(
        *_args: object, **_kwargs: object
    ) -> runner.SyntheticCredentials:
        pytest.fail("model-volume identity must be verified before credentials are created")

    monkeypatch.setattr(runner, "_verify_external_model_volume", reuse_must_fail)
    monkeypatch.setattr(runner, "_credentials", credentials_must_not_be_created)

    assert runner.run_matrix() == 1


def test_run_matrix_remote_target_rejection_never_creates_credentials_or_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    monkeypatch.setenv("DOCKER_HOST", "ssh://remote.example")
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_assert_no_production_environment", lambda: None)

    def credentials_must_not_be_created(
        *_args: int,
        **_kwargs: int,
    ) -> runner.SyntheticCredentials:
        pytest.fail("remote Docker target rejection must not create fallback credentials")

    def inventory_must_not_run(
        *_args: str | float,
        **_kwargs: str | float,
    ) -> runner.ResourceSnapshot:
        pytest.fail("remote Docker target rejection must not inventory Docker resources")

    monkeypatch.setattr(runner, "_credentials", credentials_must_not_be_created)
    monkeypatch.setattr(runner, "_resource_snapshot", inventory_must_not_run)
    assert runner.run_matrix() == 1
