from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]
_RUNNER = _BACKEND / "scripts" / "run-p1-09-postgres-restore-drill.ps1"


def test_p1_09_runner_requires_isolated_real_restore_and_integrity_proof() -> None:
    source = _RUNNER.read_text(encoding="utf-8").replace("\\", "/").lower()

    assert "confirmisolatedrestoretest" in source
    assert "app_environment" in source and "production" in source
    assert "esrag-restoretest-" in source
    assert "docker-compose.migration-test.yml" in source
    assert "^127/.0/.0/.1:(?<port>/d+)$" in source
    assert "alembic.exe upgrade head" in source
    assert "pg_dump" in source and "-fc" in source and "$dumppath" in source
    assert "pg_restore" in source
    assert "create database" in source and "drop database" in source
    assert "alembic_version" in source
    assert "count(*)" in source and "md5(" in source
    assert "finally" in source and "down -v --remove-orphans" in source
    assert "$composestarted = $true" in source
    assert "simulated" not in source
