from __future__ import annotations

import ast
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = PROJECT_ROOT / "backend" / "docker-compose.p1-12-minio-restore-test.yml"
SCRIPT_FILE = PROJECT_ROOT / "backend" / "scripts" / "run-p1-12-minio-restore-drill.py"


def _load_drill_module():
    """Load the runner without requiring its inline MinIO runtime dependency."""
    module_name = "p1_12_minio_restore_drill_contract_module"
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


def test_compose_defines_two_private_minio_data_stores() -> None:
    """Given the drill Compose file, source and target storage are isolated."""
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "source_minio:" in compose
    assert "target_minio:" in compose
    assert "image: minio/minio@sha256:" in compose
    assert "p1_12_source_minio_data:/data" in compose
    assert "p1_12_target_minio_data:/data" in compose
    assert compose.count("host_ip: 127.0.0.1") == 2
    assert "internal:" not in compose
    assert compose.count("p1_12_source_minio_network") == 2
    assert compose.count("p1_12_target_minio_network") == 2
    assert "uploads_data" not in compose


def test_compose_uses_synthetic_credentials_and_loopback_api_ports() -> None:
    """Given the drill Compose file, credentials and API ports stay test-only."""
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "P1_12_MINIO_ACCESS_KEY" in compose
    assert "P1_12_MINIO_SECRET_KEY" in compose
    assert "P1_12_SOURCE_MINIO_PORT" in compose
    assert "P1_12_TARGET_MINIO_PORT" in compose
    assert compose.count("target: 9000") == 2
    assert "- minio_data:/data" not in compose


def test_runner_passes_reserved_ports_and_requires_exact_loopback_binding() -> None:
    """Given reserved ports, Compose must return those exact loopback bindings."""
    drill = _load_drill_module()
    source_port, target_port = drill._reserve_loopback_ports()

    assert source_port != target_port
    environment = drill._compose_environment("access", "secret", source_port, target_port)
    assert environment["P1_12_SOURCE_MINIO_PORT"] == str(source_port)
    assert environment["P1_12_TARGET_MINIO_PORT"] == str(target_port)

    compose_run = Mock(
        return_value=SimpleNamespace(returncode=0, stdout=f"127.0.0.1:{source_port}\n")
    )
    with patch.object(drill.subprocess, "run", compose_run):
        assert (
            drill._compose_port(
                "docker",
                COMPOSE_FILE,
                "project",
                "source_minio",
                environment,
                source_port,
            )
            == source_port
        )

    wrong_port = source_port + 1 if source_port < 65535 else source_port - 1
    compose_run.return_value = SimpleNamespace(returncode=0, stdout=f"127.0.0.1:{wrong_port}\n")
    with (
        patch.object(drill.subprocess, "run", compose_run),
        pytest.raises(drill.DrillError, match="not reserved port"),
    ):
        drill._compose_port(
            "docker",
            COMPOSE_FILE,
            "project",
            "source_minio",
            environment,
            source_port,
        )


def test_drill_uses_byte_download_restore_and_hash_verification() -> None:
    """Given the drill script, restore evidence comes from real object bytes."""
    source = SCRIPT_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SCRIPT_FILE))
    method_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert {"fget_object", "fput_object", "get_object"} <= method_calls
    assert {"list_objects", "stat_object", "put_object"} <= method_calls
    assert "hashlib.sha256" in source
    assert "TemporaryDirectory" in source
    assert "source_sha256" in source
    assert "target_sha256" in source
    assert "source_size" in source
    assert "target_size" in source


def test_drill_is_guarded_and_scoped_to_its_compose_project() -> None:
    """Given the drill script, execution requires confirmation and cleans owned resources."""
    source = SCRIPT_FILE.read_text(encoding="utf-8")

    assert "--confirm-isolated-restore-test" in source
    assert "ENVIRONMENT" in source
    assert "APP_ENV" in source
    assert "production" in source
    assert "uuid.uuid4" in source
    assert "compose" in source
    assert "down" in source
    assert "--remove-orphans" in source
    assert '"down", "-v", "--remove-orphans"' in source
