from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = BACKEND_ROOT / "docker-compose.p1-13-uploads-restore-test.yml"
DRILL_SCRIPT = BACKEND_ROOT / "scripts" / "run-p1-13-uploads-restore-drill.ps1"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_compose_uses_distinct_named_source_target_and_backup_volumes() -> None:
    compose = _read(COMPOSE_FILE)

    assert "restore-drill:" in compose
    assert "- uploads_data_source:/source:rw" in compose
    assert "- uploads_data_target:/target:rw" in compose
    assert "- uploads_data_backup:/backup:rw" in compose

    assert 'name: "${P1_13_UPLOADS_SOURCE_VOLUME:?' in compose
    assert 'name: "${P1_13_UPLOADS_TARGET_VOLUME:?' in compose
    assert 'name: "${P1_13_UPLOADS_BACKUP_VOLUME:?' in compose
    assert compose.index("P1_13_UPLOADS_SOURCE_VOLUME") < compose.index(
        "P1_13_UPLOADS_TARGET_VOLUME"
    )


def test_drill_is_guarded_unique_and_cleans_only_owned_resources() -> None:
    script = _read(DRILL_SCRIPT)

    assert "Assert-NotProduction" in script
    assert '"production"' in script
    assert '"live"' in script
    assert "Refusing to run" in script
    assert "[Guid]::NewGuid()" in script
    assert '"p1-13-uploads-restore-' in script
    assert "--project-name" in script
    assert "down --volumes --remove-orphans" in script
    assert "volume rm -f $volume" in script
    assert "docker system prune" not in script.lower()


def test_drill_proves_bytes_and_rejects_unsafe_archive_paths() -> None:
    script = _read(DRILL_SCRIPT)

    for fragment in (
        "tar -czf /backup/uploads-data.tar.gz",
        "tar -tzf /backup/uploads-data.tar.gz",
        "tar -xzf /backup/uploads-data.tar.gz",
        "sha256sum",
        "wc -c",
        "Assert-SafeArchiveEntries",
        "Assert-SafeRelativePath",
        'StartsWith("/")',
        '-match "^[A-Za-z]:"',
        "Compare-Manifests",
    ):
        assert fragment in script

    for fixture_path in (
        "/source/incoming/2026/09/nested/manifest.csv",
        "/source/incoming/2026/09/nested/empty.bin",
        "/source/teams/platform/assets/schema.json",
        "/source/teams/platform/specs/retention-policy.txt",
    ):
        assert fixture_path in script


def test_drill_restores_environment_after_a_run() -> None:
    script = _read(DRILL_SCRIPT)

    assert "environmentSnapshotTaken" in script
    assert "restored process-scoped volume environment variables" in script
    assert "finally" in script
