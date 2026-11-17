from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_world_memory_candidate_settings_are_opt_in_and_bounded() -> None:
    settings = Settings(_env_file=None)

    assert settings.WORLD_MEMORY_CANDIDATE_SOURCES_ENABLED is False
    assert settings.WORLD_MEMORY_ELASTICSEARCH_INDEX_NAME == "world-memory-history-v1"
    assert settings.WORLD_MEMORY_NEO4J_DATABASE == "neo4j"
    assert settings.WORLD_MEMORY_CANDIDATE_SOURCE_TIMEOUT_S == 2.0
    assert settings.WORLD_MEMORY_EVENT_HISTORY_ENABLED is False
    assert settings.WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME == "world-memory-events-v2"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, WORLD_MEMORY_CANDIDATE_SOURCE_TIMEOUT_S=0.0)


def test_compose_neo4j_healthcheck_uses_container_auth_with_cypher_shell() -> None:
    compose = (_REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    neo4j = compose.split("  neo4j:", 1)[1].split("  backend:", 1)[0]
    backend = compose.split("  backend:", 1)[1].split("  minio-worker:", 1)[0]

    assert "cypher-shell" in neo4j
    assert "curl" not in neo4j
    assert "$${NEO4J_AUTH%%/*}" in neo4j
    assert "$${NEO4J_AUTH#*/}" in neo4j
    assert "WORLD_MEMORY_CANDIDATE_SOURCES_ENABLED" in backend
    assert "WORLD_MEMORY_ELASTICSEARCH_INDEX_NAME" in backend
    assert "WORLD_MEMORY_NEO4J_DATABASE" in backend
    assert "WORLD_MEMORY_CANDIDATE_SOURCE_TIMEOUT_S" in backend
    assert "WORLD_MEMORY_EVENT_HISTORY_ENABLED" in backend
    assert "WORLD_MEMORY_EVENT_HISTORY_INDEX_NAME" in backend
