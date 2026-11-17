from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import types
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = PROJECT_ROOT / "backend" / "docker-compose.p1-14-cross-store-restore-test.yml"
SCRIPT_FILE = PROJECT_ROOT / "backend" / "scripts" / "run-p1-14-cross-store-restore-drill.py"


def _load_drill_module():
    module_name = "p1_14_cross_store_restore_contract_module"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_FILE)
    assert spec is not None and spec.loader is not None

    minio_stub = types.ModuleType("minio")
    minio_stub.Minio = object
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"minio": minio_stub}):
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_name, None)
    return module


def test_compose_isolates_all_source_target_stores_and_owned_artifacts() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {
        "backup_artifacts_init",
        "snapshot_repository_init",
        "restore_tool",
        "source_postgres",
        "target_postgres",
        "source_elasticsearch",
        "target_elasticsearch",
        "source_neo4j",
        "target_neo4j",
        "source_minio",
        "target_minio",
    }
    assert set(compose["volumes"]) == {
        "p1_14_source_postgres_data",
        "p1_14_target_postgres_data",
        "p1_14_source_elasticsearch_data",
        "p1_14_target_elasticsearch_data",
        "p1_14_source_neo4j_data",
        "p1_14_target_neo4j_data",
        "p1_14_source_minio_data",
        "p1_14_target_minio_data",
        "p1_14_source_uploads",
        "p1_14_target_uploads",
        "p1_14_backup_artifacts",
        "p1_14_snapshot_repository",
    }

    assert services["restore_tool"]["volumes"] == [
        "p1_14_source_uploads:/source:rw",
        "p1_14_target_uploads:/target:rw",
        "p1_14_backup_artifacts:/backup:rw",
        "p1_14_snapshot_repository:/snapshots:rw",
    ]
    assert services["backup_artifacts_init"]["image"] == "alpine:3.20"
    assert services["backup_artifacts_init"]["user"] == "0:0"
    assert services["backup_artifacts_init"]["volumes"] == [
        "p1_14_backup_artifacts:/backup:rw"
    ]
    assert services["snapshot_repository_init"]["image"] == "busybox:1.36"
    assert services["snapshot_repository_init"]["user"] == "0:0"
    assert "chown 1000:0 /snapshots" in services["snapshot_repository_init"]["command"][-1]
    assert "chmod 0770 /snapshots" in services["snapshot_repository_init"]["command"][-1]
    assert services["snapshot_repository_init"]["volumes"] == [
        "p1_14_snapshot_repository:/snapshots"
    ]
    assert "es-snapshots" not in COMPOSE_FILE.read_text(encoding="utf-8")
    assert services["source_postgres"]["image"].startswith("postgres:15")
    assert services["target_postgres"]["image"] == services["source_postgres"]["image"]
    assert services["source_elasticsearch"]["image"] == (
        "docker.elastic.co/elasticsearch/elasticsearch:8.12.0"
    )
    assert services["target_elasticsearch"]["image"] == services["source_elasticsearch"]["image"]
    assert services["source_neo4j"]["image"] == services["target_neo4j"]["image"] == "neo4j:5.15-community"
    assert services["source_minio"]["image"].startswith("minio/minio@sha256:")
    assert services["target_minio"]["image"] == services["source_minio"]["image"]

    for service_name, service in services.items():
        if not service_name.startswith(("source_", "target_")):
            continue
        for port in service["ports"]:
            assert port["host_ip"] == "127.0.0.1"
            assert port["protocol"] == "tcp"
            assert "P1_14_" in port["published"]

    for source_name, target_name in (
        ("source_postgres", "target_postgres"),
        ("source_elasticsearch", "target_elasticsearch"),
        ("source_neo4j", "target_neo4j"),
        ("source_minio", "target_minio"),
    ):
        source_data = services[source_name]["volumes"][0]
        target_data = services[target_name]["volumes"][0]
        assert source_data != target_data
        assert services[source_name]["volumes"][-1] == services[target_name]["volumes"][-1]
        init_service = (
            "snapshot_repository_init"
            if "elasticsearch" in source_name
            else "backup_artifacts_init"
        )
        assert (
            services[source_name]["depends_on"][init_service]["condition"]
            == "service_completed_successfully"
        )
        assert (
            services[target_name]["depends_on"][init_service]["condition"]
            == "service_completed_successfully"
        )

    for service_name in ("source_elasticsearch", "target_elasticsearch"):
        service = services[service_name]
        assert "path.repo=/snapshots" in service["environment"]
        assert service["volumes"][-1] == "p1_14_snapshot_repository:/snapshots:rw"
        assert "p1_14_backup_artifacts:/backups:rw" not in service["volumes"]


def test_runner_has_real_cross_store_backup_restore_and_safety_contract() -> None:
    source = SCRIPT_FILE.read_text(encoding="utf-8")
    lowered = source.lower()

    for fragment in (
        "--confirm-isolated-restore-test",
        "production",
        "live",
        "uuid.uuid4",
        "docker-compose.p1-14-cross-store-restore-test.yml",
        "127.0.0.1",
        "pg_dump",
        "-fc",
        "pg_restore",
        "/backups/postgres/p1-14-authority.dump",
        "/backup/postgres/p1-14-authority.dump",
        "_snapshot",
        '"location": "/snapshots"',
        "/snapshots",
        "wait_for_completion=true",
        "neo4j-admin",
        "database",
        "dump",
        "load",
        "put_object",
        "get_object",
        "list_objects",
        "tar -czf",
        "tar -xzf",
        "hashlib.sha256",
        "checkpoint_at",
        "observed_restore_rpo_seconds",
        "max_observed_rpo_seconds",
        '"stop", *source_services',
        '"down", "-v", "--remove-orphans"',
    ):
        assert fragment in lowered, f"missing runner contract fragment: {fragment}"

    assert "docker system prune" not in lowered
    assert "volume rm" not in lowered
    assert "simulated" not in lowered
    assert "/backup/es-snapshots" not in lowered

    stop_index = lowered.index('(\"stop\", *source_services)')
    target_index = lowered.index('(\"up\", \"-d\", \"--wait\", *target_services)')
    assert stop_index < target_index

    output_lines = [line for line in source.splitlines() if "print(" in line]
    assert output_lines
    for line in output_lines:
        assert "password" not in line.lower()
        assert "secret" not in line.lower()
        assert "api_key" not in line.lower()


def test_postgres_restore_streams_captured_dump_bytes_to_target_without_path_read() -> None:
    drill = _load_drill_module()
    dump_bytes = b"real-p1-14-postgres-custom-format-dump"

    with patch.object(drill, "_compose_exec") as compose_exec:
        drill._restore_postgres_dump(
            "docker",
            Path("compose.yml"),
            "p1-14-project",
            {},
            dump_bytes,
        )

    compose_exec.assert_called_once()
    call = compose_exec.call_args
    assert call is not None
    command = call.args[5]
    assert call.args[4] == "target_postgres"
    assert command == (
        "pg_restore",
        "-U",
        "postgres",
        "--exit-on-error",
        "--no-owner",
        "--no-acl",
        "-d",
        drill.TARGET_DATABASE,
    )
    assert "-" not in command
    assert drill.PG_DUMP_PATH not in command
    assert call.kwargs["input_bytes"] is dump_bytes


def test_deterministic_fixture_and_rpo_calculation_are_meaningful() -> None:
    drill = _load_drill_module()

    assert drill.EXPECTED_RECORD.entity_id == "p1-14-entity-001"
    assert drill.EXPECTED_RECORD.tenant_id == "p1-14-tenant-001"
    assert drill.EXPECTED_RECORD.kb_id == "p1-14-kb-001"
    assert drill.EXPECTED_RECORD.object_key == drill.OBJECT_KEY
    assert drill.EXPECTED_RECORD.upload_path == drill.UPLOAD_PATH
    assert drill.EXPECTED_RECORD.content_sha256 == drill.CONTENT_SHA256
    assert drill.EXPECTED_RECORD.content_bytes == len(drill.SYNTHETIC_FILE_BYTES)
    cloned_record = replace(drill.EXPECTED_RECORD)
    changed_record = replace(drill.EXPECTED_RECORD, acl_role="writer")
    assert drill._record_sha256(cloned_record) == drill._record_sha256(drill.EXPECTED_RECORD)
    assert drill._record_sha256(changed_record) != drill._record_sha256(drill.EXPECTED_RECORD)

    checkpoint = dt.datetime(2026, 9, 25, tzinfo=dt.timezone.utc)
    artifact_times = {
        "postgres_dump": checkpoint + dt.timedelta(seconds=3),
        "elasticsearch_snapshot": checkpoint + dt.timedelta(seconds=7),
        "neo4j_dump": checkpoint + dt.timedelta(seconds=11),
    }
    assert drill._observed_rpo_seconds(checkpoint, artifact_times) == 11.0
    with pytest.raises(drill.DrillError, match="exceeded|timestamp"):
        drill._observed_rpo_seconds(
            checkpoint,
            {"late": checkpoint - dt.timedelta(seconds=1)},
        )


def test_record_validation_reports_only_mismatched_field_names() -> None:
    drill = _load_drill_module()
    wrong_object_key = "wrong-object-key"
    wrong_acl_subject = "wrong-acl-subject"
    wrong_content_hash = "wrong-content-hash"
    actual = replace(
        drill.EXPECTED_RECORD,
        object_key=wrong_object_key,
        acl_subject=wrong_acl_subject,
        content_sha256=wrong_content_hash,
    )

    with pytest.raises(drill.DrillError) as error_info:
        drill._validate_record("source Neo4j", actual, drill.EXPECTED_RECORD)

    message = str(error_info.value)
    assert message == (
        "source Neo4j mismatched synthetic fields: object_key, acl_subject, content_sha256."
    )
    for value in (
        wrong_object_key,
        wrong_acl_subject,
        wrong_content_hash,
        drill.EXPECTED_RECORD.object_key,
        drill.EXPECTED_RECORD.acl_subject,
        drill.EXPECTED_RECORD.content_sha256,
    ):
        assert value not in message


def test_neo4j_plain_rows_accept_spaces_before_quoted_values() -> None:
    drill = _load_drill_module()
    expected = drill.EXPECTED_RECORD
    output = (
        f"{', '.join(drill.RECORD_FIELDS)}\n"
        f'{expected.entity_id}, "{expected.tenant_id}", "{expected.kb_id}", '
        f'"{expected.object_key}", "{expected.upload_path}", '
        f'"{expected.acl_subject}", "{expected.acl_role}", "{expected.acl_scope}", '
        f'"{expected.content_sha256}", {expected.content_bytes}\n'
    )

    rows = drill._parse_cypher_plain_rows(output, drill.RECORD_FIELDS)
    assert len(rows) == 1
    assert drill._record_from_values([rows[0][field] for field in drill.RECORD_FIELDS]) == expected


def test_runner_requires_explicit_confirmation_without_touching_docker() -> None:
    drill = _load_drill_module()
    assert drill.main(()) == 2


def test_production_guard_rejects_known_environment_markers() -> None:
    drill = _load_drill_module()
    with (
        patch.dict("os.environ", {"APP_ENV": "production"}, clear=False),
        pytest.raises(drill.DrillError, match="production"),
    ):
        drill._require_non_production()
