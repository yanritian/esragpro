from pathlib import Path

import yaml


_BACKEND = Path(__file__).resolve().parents[2]
_COMPOSE = _BACKEND / "docker-compose.p1-10-restore-test.yml"
_RUNNER = _BACKEND / "scripts" / "run-p1-10-elasticsearch-restore-drill.ps1"


def test_p1_10_compose_has_separate_loopback_clusters_and_shared_repository() -> None:
    compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {
        "snapshot_repository_init",
        "source_elasticsearch",
        "target_elasticsearch",
    }
    assert compose["volumes"] == {
        "p1_10_source_es_data": None,
        "p1_10_target_es_data": None,
        "p1_10_snapshot_repository": None,
    }

    source = services["source_elasticsearch"]
    target = services["target_elasticsearch"]
    repository_init = services["snapshot_repository_init"]
    assert repository_init["image"] == "busybox:1.36"
    assert repository_init["user"] == "0:0"
    assert "chown 1000:0 /snapshots" in repository_init["command"][-1]
    assert "chmod 0770 /snapshots" in repository_init["command"][-1]
    assert repository_init["volumes"] == ["p1_10_snapshot_repository:/snapshots"]
    for service in (source, target):
        assert service["depends_on"]["snapshot_repository_init"]["condition"] == (
            "service_completed_successfully"
        )
    assert (
        source["image"] == target["image"] == "docker.elastic.co/elasticsearch/elasticsearch:8.12.0"
    )
    assert source["volumes"][0] == "p1_10_source_es_data:/usr/share/elasticsearch/data"
    assert target["volumes"][0] == "p1_10_target_es_data:/usr/share/elasticsearch/data"
    assert source["volumes"][1] == target["volumes"][1] == "p1_10_snapshot_repository:/snapshots"

    for service in (source, target):
        port = service["ports"][0]
        assert port["target"] == 9200
        assert port["host_ip"] == "127.0.0.1"
        assert port["protocol"] == "tcp"
        assert "ELASTICSEARCH_PORT" in port["published"]
        environment = "\n".join(service["environment"])
        assert "discovery.type=single-node" in environment
        assert "xpack.security.enabled=false" in environment
        assert "path.repo=/snapshots" in environment


def test_p1_10_runner_is_fail_closed_and_proves_real_restore_integrity() -> None:
    source = _RUNNER.read_text(encoding="utf-8").replace("\\", "/").lower()

    required_fragments = (
        "confirmisolatedrestoretest",
        "production|prod",
        "esrag-restoretest-",
        "docker-compose.p1-10-restore-test.yml",
        "source_elasticsearch",
        "target_elasticsearch",
        "127/.0/.0/.1:(?<port>/d+)",
        "p1_10_shared_snapshot_repository",
        'location = "/snapshots"',
        "readonly = $true",
        '"/_snapshot/${repository}?verify=true"',
        '"/_snapshot/${repository}/${snapshot}?wait_for_completion=true"',
        "verify=false",
        "_snapshot",
        "?verify=true",
        "wait_for_completion=true",
        "_restore",
        "p1-10-sentinel-001",
        "p1-10-sentinel-002",
        "p1-10-sentinel-003",
        "track_total_hits=true",
        "snapshotstate",
        "success",
        "convertto-json",
        "sha256",
        "sourceevidence.count",
        "restoredevidence",
        "term",
        "finally",
        "down -v --remove-orphans",
        "p1_10_source_elasticsearch_port",
        "p1_10_target_elasticsearch_port",
        "previouslocation",
    )
    for fragment in required_fragments:
        assert fragment in source, f"runner is missing required contract fragment: {fragment}"

    assert "simulated" not in source
    assert "docker-compose.p1-06-test.yml" not in source
    assert '"/_snapshot/$repository?verify=true"' not in source
    assert '"/_snapshot/$repository?verify=false"' not in source


def test_p1_10_runner_does_not_print_credentials() -> None:
    source = _RUNNER.read_text(encoding="utf-8").lower()
    output_lines = [line for line in source.splitlines() if "write-host" in line]

    assert output_lines
    for line in output_lines:
        assert "password" not in line
        assert "secret" not in line
        assert "api_key" not in line
        assert "authorization" not in line
