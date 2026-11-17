from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]
_SCRIPTS = _BACKEND / "scripts"
_RELEASE_GATE = _SCRIPTS / "run-backend-release-gate.ps1"
_P1_05 = _SCRIPTS / "run-p1-05-redis-hot-state-test.ps1"
_P1_06 = _SCRIPTS / "run-p1-06-elasticsearch-test.ps1"
_P1_07 = _SCRIPTS / "run-p1-07-neo4j-test.ps1"
_P1_08 = _SCRIPTS / "run-p1-08-minio-test.ps1"

_REQUIRED_RUNNERS = tuple(
    f"run-p1-{phase:02d}-{suffix}.ps1"
    for phase, suffix in (
        (1, "migration-test"),
        (2, "repository-test"),
        (3, "outbox-test"),
        (4, "worker-test"),
        (5, "redis-hot-state-test"),
        (6, "elasticsearch-test"),
        (7, "neo4j-test"),
        (8, "minio-test"),
    )
)


def test_backend_release_gate_has_isolated_fail_closed_execution_contract() -> None:
    source = _RELEASE_GATE.read_text(encoding="utf-8")
    normalized = source.replace("\\", "/").lower()

    assert "tests/unit" in normalized
    assert "tests/api" in normalized
    assert source.count("-m pytest") == 1
    assert "-noProfile".lower() in normalized
    assert "-executionpolicy bypass" in normalized
    assert "-file $runner" in normalized
    assert "if ($lastexitcode -ne 0)" in normalized
    assert "foreach ($runner" in normalized
    assert "test-path -literalpath $runner" in normalized
    assert "docker" not in normalized
    assert "compose down" not in normalized

    for runner_name in _REQUIRED_RUNNERS:
        assert runner_name.lower() in normalized


def test_p1_08_runner_owns_only_its_phase_and_cleanup() -> None:
    source = _P1_08.read_text(encoding="utf-8")
    normalized = source.replace("\\", "/").lower()

    assert "tests/integration/p1_08_minio/" in normalized
    assert "full backend suite" not in normalized
    for phase in range(2, 8):
        assert f"tests/integration/p1_{phase:02d}_" not in normalized


def test_p1_05_runner_owns_only_its_phase_and_cleanup() -> None:
    source = _P1_05.read_text(encoding="utf-8")
    normalized = source.replace("\\", "/").lower()

    assert "tests/unit/test_redis_hot_state.py" in normalized
    assert "tests/integration/p1_05_redis_hot/" in normalized
    assert "invoke-cleanup" in normalized
    assert "verify-zeroresources" in normalized
    assert "down -v --remove-orphans" in normalized
    assert "full backend suite" not in normalized
    for phase in (2, 3, 4):
        assert f"tests/integration/p1_{phase:02d}_" not in normalized


def test_p1_06_runner_owns_only_its_phase_and_cleanup() -> None:
    source = _P1_06.read_text(encoding="utf-8")
    normalized = source.replace("\\", "/").lower()

    assert "tests/unit/test_elasticsearch_projection.py" in normalized
    assert "tests/integration/p1_06_elasticsearch/" in normalized
    assert "invoke-cleanup" in normalized
    assert "verify-zeroresources" in normalized
    assert "down -v --remove-orphans" in normalized
    assert "full backend suite" not in normalized
    for phase in range(2, 6):
        assert f"tests/integration/p1_{phase:02d}_" not in normalized


def test_p1_07_runner_owns_only_its_phase_and_cleanup() -> None:
    source = _P1_07.read_text(encoding="utf-8")
    normalized = source.replace("\\", "/").lower()

    assert "tests/unit/test_neo4j_projection_payload.py" in normalized
    assert "tests/integration/p1_07_neo4j/" in normalized
    assert "existing neo4j claim-projector regression" in normalized
    assert "tests/unit/test_neo4j_projector.py" in normalized
    assert "invoke-cleanup" in normalized
    assert "verify-zeroresources" in normalized
    assert "down -v --remove-orphans" in normalized
    assert "full backend suite" not in normalized
    for phase in range(2, 7):
        assert f"tests/integration/p1_{phase:02d}_" not in normalized
