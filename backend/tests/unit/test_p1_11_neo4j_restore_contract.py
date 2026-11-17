from pathlib import Path

import yaml

_BACKEND = Path(__file__).resolve().parents[2]
_COMPOSE = _BACKEND / "docker-compose.p1-11-neo4j-restore-test.yml"
_RUNNER = _BACKEND / "scripts" / "run-p1-11-neo4j-restore-drill.ps1"


def test_p1_11_compose_isolates_community_source_target_and_dump_volume() -> None:
    compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {"source_neo4j", "target_neo4j"}
    assert compose["volumes"] == {
        "p1_11_source_neo4j_data": None,
        "p1_11_target_neo4j_data": None,
        "p1_11_dump_artifacts": None,
    }

    source = services["source_neo4j"]
    target = services["target_neo4j"]
    assert source["image"] == target["image"] == "neo4j:5.15-community"
    assert source["restart"] == target["restart"] == "no"
    assert source["volumes"] == [
        "p1_11_source_neo4j_data:/data",
        "p1_11_dump_artifacts:/var/lib/neo4j/import",
    ]
    assert target["volumes"] == [
        "p1_11_target_neo4j_data:/data",
        "p1_11_dump_artifacts:/var/lib/neo4j/import",
    ]

    for service, port_prefix in (
        (source, "P1_11_SOURCE_NEO4J_"),
        (target, "P1_11_TARGET_NEO4J_"),
    ):
        assert service["environment"]["NEO4J_AUTH"].startswith("neo4j/")
        assert service["environment"]["NEO4J_dbms_default__database"] == "neo4j"
        assert len(service["ports"]) == 2
        assert {port["target"] for port in service["ports"]} == {7474, 7687}
        for port in service["ports"]:
            assert port["host_ip"] == "127.0.0.1"
            assert port["protocol"] == "tcp"
            assert port_prefix in port["published"]


def test_p1_11_runner_requires_offline_dump_load_and_integrity_proof() -> None:
    source = _RUNNER.read_text(encoding="utf-8").replace("\\", "/").lower()

    required_fragments = (
        "confirmisolatedrestoretest",
        "production|prod",
        "esrag-restoretest-",
        "docker-compose.p1-11-neo4j-restore-test.yml",
        "source_neo4j",
        "target_neo4j",
        "127/.0/.0/.1:(?<port>/d+)$",
        "cypher-shell",
        "p1-11-node-001",
        "p1-11-node-002",
        "p1-11-node-003",
        "p1-11-edge-001",
        "p1-11-edge-002",
        "neo4j-admin",
        "offline neo4j database dump",
        "offline neo4j database load",
        "--to-path=/var/lib/neo4j/import",
        "--from-path=/var/lib/neo4j/import",
        "--overwrite-destination=true",
        '"--user", "neo4j"',
        '"neo4j-admin", "database"',
        "source_node_rows",
        "target_node_rows",
        "source_edge_rows",
        "target_edge_rows",
        "sha256",
        "finally",
        '"down", "-v", "--remove-orphans"',
        "compose_started = $true",
    )
    for fragment in required_fragments:
        assert fragment in source, f"runner is missing required contract fragment: {fragment}"

    stop_index = source.index('operation "stop neo4j services for offline restore"')
    dump_index = source.index('operation "offline neo4j database dump"')
    load_index = source.index('operation "offline neo4j database load"')
    start_index = source.index('operation "restart restored neo4j target"')
    assert stop_index < dump_index
    assert stop_index < load_index
    assert dump_index < load_index
    assert load_index < start_index
    assert " cp " not in source
    assert " copy-item " not in source
    assert "prepare shared dump volume" not in source
    assert "chown 7474:7474 /dumps" not in source
    assert "/dumps" not in source
    assert "simulated" not in source
    assert "docker-compose.p1-06-test.yml" not in source


def test_p1_11_runner_uses_neo4j_515_plain_shell_output() -> None:
    source = _RUNNER.read_text(encoding="utf-8").lower()

    assert '"--format", "plain"' in source
    assert "convert-cypherplainrows" in source
    assert "convertfrom-csv" in source
    assert "invalid plain output" in source
    assert '"--format", "csv"' not in source
    assert "-format csv" not in source
    assert "convert-cyphercsvrows" not in source


def test_p1_11_runner_does_not_print_credentials() -> None:
    source = _RUNNER.read_text(encoding="utf-8").lower()
    output_lines = [line for line in source.splitlines() if "write-host" in line]

    assert output_lines
    for line in output_lines:
        assert "password" not in line
        assert "secret" not in line
        assert "api_key" not in line


def test_p1_11_runner_reports_labeled_redacted_compose_errors() -> None:
    source = _RUNNER.read_text(encoding="utf-8").lower()

    assert "get-safecomposeerrortext" in source
    assert "operation '$operationlabel' failed" in source
    assert "2> $stderrpath" in source
    assert '$erroractionpreference = "continue"' in source
    assert "last compose error" in source
    assert "2>$null" not in source
    for operation in (
        "start isolated neo4j services",
        "discover $service loopback port $containerport",
        "offline neo4j database dump",
        "offline neo4j database load",
        "cleanup isolated compose project",
    ):
        assert f'-operation "{operation}"' in source
