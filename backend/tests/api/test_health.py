"""
Health check tests
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _ReadinessProbeError(Exception):
    pass


class _ReadyConnection:
    async def execute(self, query: str) -> str:
        return query

    async def fetch(self, query: str) -> list[dict[str, str]]:
        return [{"version_num": "current-head"}]


class _ReadyPool:
    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_ReadyConnection]:
        yield _ReadyConnection()


async def _ready_pool_factory() -> _ReadyPool:
    return _ReadyPool()


async def _ready_probe() -> None:
    return None


class TestHealthCheck:
    """Test health check endpoints"""

    @pytest.mark.asyncio
    async def test_health_liveness(self):
        """/health should return ok even if ES is down"""
        from app.api.kb import health_check

        # Mock ES failure
        with patch("app.core.elasticsearch.es_client") as mock_es:
            mock_es.client.info = AsyncMock(side_effect=Exception("ES down"))

            result = await health_check()

        # Should still return ok (liveness probe)
        assert result.get("status") == "ok"

    @pytest.mark.asyncio
    async def test_health_reflects_es_status(self):
        """/health should report ES status"""
        from app.api.kb import health_check

        # 创建完整的 mock 对象
        mock_es = MagicMock()
        mock_client = MagicMock()
        mock_client.info = AsyncMock(return_value={"version": "8.12.0"})
        mock_es.client = mock_client

        with patch("app.core.elasticsearch.es_client", mock_es):
            result = await health_check()

        # ES should be healthy
        assert result.get("elasticsearch") == "healthy"

    def test_ready_endpoint_needed(self):
        from app import main

        routes = {getattr(route, "path", None) for route in main.app.routes}

        assert "/health" in routes
        assert "/ready" in routes

    @pytest.mark.asyncio
    async def test_ready_returns_503_when_migrations_are_behind(self):
        from app.core.readiness import (
            ReadinessDependencies,
            assess_application_readiness,
        )

        class FakeConnection:
            async def execute(self, query: str) -> str:
                assert query == "SELECT 1"
                return "SELECT 1"

            async def fetch(self, query: str) -> list[dict[str, str]]:
                assert query == "SELECT version_num FROM alembic_version"
                return [{"version_num": "old-head"}]

        class FakePool:
            @asynccontextmanager
            async def acquire(self) -> AsyncIterator[FakeConnection]:
                yield FakeConnection()

        async def pool_factory() -> FakePool:
            return FakePool()

        async def elasticsearch_probe() -> None:
            return None

        async def redis_probe() -> None:
            return None

        dependencies = ReadinessDependencies(
            database_pool_factory=pool_factory,
            elasticsearch_probe=elasticsearch_probe,
            redis_probe=redis_probe,
            migration_heads_provider=lambda: ("current-head",),
        )

        report = await assess_application_readiness(dependencies)

        assert report.ready is False
        assert report.components["migrations"].reason == "migration_head_mismatch"

    @pytest.mark.asyncio
    async def test_ready_masks_unexpected_dependency_errors(self):
        from app.core.readiness import (
            ReadinessDependencies,
            assess_application_readiness,
        )

        async def pool_factory():
            raise _ReadinessProbeError("postgresql://user:super-secret@db")

        async def elasticsearch_probe() -> None:
            raise _ReadinessProbeError("Authorization: Bearer super-secret")

        async def redis_probe() -> None:
            raise _ReadinessProbeError("redis://:super-secret@cache")

        dependencies = ReadinessDependencies(
            database_pool_factory=pool_factory,
            elasticsearch_probe=elasticsearch_probe,
            redis_probe=redis_probe,
            migration_heads_provider=lambda: ("current-head",),
        )

        report = await assess_application_readiness(dependencies)
        payload = report.to_payload()

        assert report.ready is False
        assert payload["components"]["postgres"]["reason"] == "postgres_unavailable"
        assert payload["components"]["elasticsearch"]["reason"] == "elasticsearch_unavailable"
        assert payload["components"]["redis"]["reason"] == "redis_unavailable"
        assert "super-secret" not in repr(payload)

    @pytest.mark.asyncio
    async def test_ready_fails_when_neo4j_enabled_without_probe(self, monkeypatch):
        from fastapi import HTTPException

        from app import main

        monkeypatch.setattr(main.settings, "NEO4J_ENABLED", True)
        monkeypatch.delattr(main.app.state, "neo4j_readiness_probe", raising=False)
        dependencies = main._readiness_dependencies()
        assert dependencies.neo4j_enabled is True
        assert dependencies.neo4j_probe is None

        healthy_dependencies = replace(
            dependencies,
            database_pool_factory=_ready_pool_factory,
            elasticsearch_probe=_ready_probe,
            redis_probe=_ready_probe,
            migration_heads_provider=lambda: ("current-head",),
            embedding_model_probe=_ready_probe,
        )
        monkeypatch.setattr(main, "_readiness_dependencies", lambda: healthy_dependencies)

        with pytest.raises(HTTPException) as error:
            await main.ready()

        assert error.value.status_code == 503
        detail = error.value.detail
        assert isinstance(detail, dict)
        components = detail.get("components")
        assert isinstance(components, dict)
        assert components["neo4j"] == {
            "status": "not_ready",
            "reason": "neo4j_probe_unavailable",
        }

    @pytest.mark.asyncio
    async def test_ready_fails_when_minio_enabled_without_probe(self, monkeypatch):
        from fastapi import HTTPException

        from app import main

        monkeypatch.setattr(main.app.state, "minio_enabled", True, raising=False)
        monkeypatch.delattr(main.app.state, "minio_readiness_probe", raising=False)
        dependencies = main._readiness_dependencies()
        assert dependencies.minio_enabled is True
        assert dependencies.minio_probe is None

        healthy_dependencies = replace(
            dependencies,
            database_pool_factory=_ready_pool_factory,
            elasticsearch_probe=_ready_probe,
            redis_probe=_ready_probe,
            migration_heads_provider=lambda: ("current-head",),
            embedding_model_probe=_ready_probe,
        )
        monkeypatch.setattr(main, "_readiness_dependencies", lambda: healthy_dependencies)

        with pytest.raises(HTTPException) as error:
            await main.ready()

        assert error.value.status_code == 503
        detail = error.value.detail
        assert isinstance(detail, dict)
        components = detail.get("components")
        assert isinstance(components, dict)
        assert components["minio"] == {
            "status": "not_ready",
            "reason": "minio_probe_unavailable",
        }

    @pytest.mark.asyncio
    async def test_ready_skips_disabled_optional_dependencies_without_probes(self, monkeypatch):
        from app import main

        monkeypatch.setattr(main.settings, "NEO4J_ENABLED", False)
        monkeypatch.setattr(main.app.state, "minio_enabled", False, raising=False)
        monkeypatch.delattr(main.app.state, "neo4j_readiness_probe", raising=False)
        monkeypatch.delattr(main.app.state, "minio_readiness_probe", raising=False)
        dependencies = main._readiness_dependencies()
        assert dependencies.neo4j_enabled is False
        assert dependencies.minio_enabled is False

        healthy_dependencies = replace(
            dependencies,
            database_pool_factory=_ready_pool_factory,
            elasticsearch_probe=_ready_probe,
            redis_probe=_ready_probe,
            migration_heads_provider=lambda: ("current-head",),
            embedding_model_probe=_ready_probe,
        )
        monkeypatch.setattr(main, "_readiness_dependencies", lambda: healthy_dependencies)

        payload = await main.ready()

        assert payload["status"] == "ready"
        assert payload["components"]["neo4j"] == {
            "status": "skipped",
            "reason": "not_enabled",
        }
        assert payload["components"]["minio"] == {
            "status": "skipped",
            "reason": "not_enabled",
        }

    @pytest.mark.asyncio
    async def test_ready_returns_503_when_embedding_model_is_unavailable(self, monkeypatch):
        from fastapi import HTTPException

        from app import main

        async def embedding_model_probe() -> None:
            raise _ReadinessProbeError("model load failed")

        dependencies = replace(
            main._readiness_dependencies(),
            database_pool_factory=_ready_pool_factory,
            elasticsearch_probe=_ready_probe,
            redis_probe=_ready_probe,
            migration_heads_provider=lambda: ("current-head",),
            embedding_model_probe=embedding_model_probe,
        )
        monkeypatch.setattr(main, "_readiness_dependencies", lambda: dependencies)

        with pytest.raises(HTTPException) as error:
            await main.ready()

        assert error.value.status_code == 503
        detail = error.value.detail
        assert isinstance(detail, dict)
        components = detail.get("components")
        assert isinstance(components, dict)
        assert components["embedding_model"] == {
            "status": "not_ready",
            "reason": "embedding_model_unavailable",
        }

    @pytest.mark.asyncio
    async def test_ready_returns_ready_when_embedding_model_is_available(self, monkeypatch):
        from app import main

        monkeypatch.setattr(main.settings, "NEO4J_ENABLED", False)
        monkeypatch.setattr(main.app.state, "minio_enabled", False, raising=False)
        dependencies = replace(
            main._readiness_dependencies(),
            database_pool_factory=_ready_pool_factory,
            elasticsearch_probe=_ready_probe,
            redis_probe=_ready_probe,
            migration_heads_provider=lambda: ("current-head",),
            embedding_model_probe=_ready_probe,
        )
        monkeypatch.setattr(main, "_readiness_dependencies", lambda: dependencies)

        payload = await main.ready()

        assert payload["status"] == "ready"
        assert payload["components"]["embedding_model"] == {
            "status": "ready",
            "reason": None,
        }

    @pytest.mark.asyncio
    async def test_lifespan_preflight_success_marks_embedding_model_ready(self, monkeypatch):
        from app import main

        preflight = MagicMock()
        monkeypatch.setattr(main.embedding_service, "preflight", preflight)
        monkeypatch.setattr(main.es_client, "connect", AsyncMock())
        monkeypatch.setattr(main.es_client, "create_index", AsyncMock(return_value=True))
        monkeypatch.setattr(main.es_client, "close", AsyncMock())
        monkeypatch.setattr(main, "get_db_pool", AsyncMock())
        monkeypatch.setattr(main, "init_session_factory", AsyncMock())
        monkeypatch.setattr(main, "get_world_memory_candidate_sources", AsyncMock())
        monkeypatch.setattr(main, "get_world_memory_event_history_client", AsyncMock())
        monkeypatch.setattr(main, "close_world_memory_event_history_coordinator_client", AsyncMock())
        monkeypatch.setattr(main, "close_world_memory_event_history_client", AsyncMock())
        monkeypatch.setattr(main, "close_world_memory_candidate_sources", AsyncMock())
        monkeypatch.setattr(main, "close_db_pool", AsyncMock())
        monkeypatch.setattr(main, "close_session_factory", AsyncMock())

        async with main.lifespan(main.app):
            assert main.app.state.embedding_model_ready is True

        preflight.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_lifespan_preflight_failure_keeps_app_live_but_not_ready(self, monkeypatch):
        from fastapi import HTTPException

        from app import main
        from app.services.embedding import EmbeddingModelError

        preflight = MagicMock(side_effect=EmbeddingModelError())
        monkeypatch.setattr(main.embedding_service, "preflight", preflight)
        monkeypatch.setattr(main.es_client, "connect", AsyncMock())
        monkeypatch.setattr(main.es_client, "create_index", AsyncMock(return_value=True))
        monkeypatch.setattr(main.es_client, "close", AsyncMock())
        monkeypatch.setattr(main, "get_db_pool", AsyncMock())
        monkeypatch.setattr(main, "init_session_factory", AsyncMock())
        monkeypatch.setattr(main, "get_world_memory_candidate_sources", AsyncMock())
        monkeypatch.setattr(main, "get_world_memory_event_history_client", AsyncMock())
        monkeypatch.setattr(main, "close_world_memory_event_history_coordinator_client", AsyncMock())
        monkeypatch.setattr(main, "close_world_memory_event_history_client", AsyncMock())
        monkeypatch.setattr(main, "close_world_memory_candidate_sources", AsyncMock())
        monkeypatch.setattr(main, "close_db_pool", AsyncMock())
        monkeypatch.setattr(main, "close_session_factory", AsyncMock())
        monkeypatch.setattr(main.settings, "NEO4J_ENABLED", False)
        monkeypatch.setattr(main.app.state, "minio_enabled", False, raising=False)

        dependencies = replace(
            main._readiness_dependencies(),
            database_pool_factory=_ready_pool_factory,
            elasticsearch_probe=_ready_probe,
            redis_probe=_ready_probe,
            migration_heads_provider=lambda: ("current-head",),
        )
        monkeypatch.setattr(main, "_readiness_dependencies", lambda: dependencies)

        async with main.lifespan(main.app):
            assert main.app.state.embedding_model_ready is False
            assert await main.health() == {"status": "healthy"}
            with pytest.raises(HTTPException) as error:
                await main.ready()

            assert error.value.status_code == 503
            detail = error.value.detail
            assert isinstance(detail, dict)
            components = detail.get("components")
            assert isinstance(components, dict)
            assert components["embedding_model"] == {
                "status": "not_ready",
                "reason": "embedding_model_unavailable",
            }

        preflight.assert_called_once_with()
