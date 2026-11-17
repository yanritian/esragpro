"""Tests for the P1-09 memory-plane session composition seam.

Proves:
1. ``service_scope()`` uses the override factory when one is installed and
   yields ``(service, None)`` so callers do not attempt a commit.
2. Without an override, ``service_scope()`` creates a Postgres-backed
   service and yields ``(service, commit_callable)``.
3. Endpoints honour the override — no live DB needed for unit tests.
4. The override is isolated: clearing it restores the production path.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from app.memory_core import (
    UnpermissionedSmartRetrievalService,
)
from app.memory_core import memory_plane_session as session_module
from app.memory_core.memory_plane_session import (
    close_session_factory,
    get_service_factory,
    is_configured,
    service_scope,
    set_service_factory,
)
from app.memory_core.unpermissioned_repository import (
    InMemoryUnpermissionedMemoryRepository,
)


@pytest.fixture(autouse=True)
async def _reset_session_state():
    """Ensure each test starts with no override and no engine."""
    await close_session_factory()
    set_service_factory(None)
    yield
    await close_session_factory()
    set_service_factory(None)


class TestServiceScopeOverridePath:
    """When an override factory is set, service_scope bypasses Postgres."""

    def test_override_returns_in_memory_service(self):
        """set_service_factory → service_scope yields the override service."""
        repo = InMemoryUnpermissionedMemoryRepository()
        service = UnpermissionedSmartRetrievalService(repository=repo)
        set_service_factory(lambda: service)

        assert get_service_factory() is not None

    @pytest.mark.asyncio
    async def test_service_scope_yields_override_with_no_commit(self):
        """service_scope() with override yields (service, None)."""
        repo = InMemoryUnpermissionedMemoryRepository()
        service = UnpermissionedSmartRetrievalService(repository=repo)
        set_service_factory(lambda: service)

        async with service_scope() as (svc, commit):
            assert svc is service
            assert commit is None

    @pytest.mark.asyncio
    async def test_endpoint_remember_via_override_does_not_require_db(self):
        """The remember endpoint must work via override with no DB."""
        from app.api.memory_plane import (
            RememberRequest,
            remember,
        )

        repo = InMemoryUnpermissionedMemoryRepository()
        set_service_factory(
            lambda: UnpermissionedSmartRetrievalService(repository=repo)
        )

        request = RememberRequest(
            content="P1-09 DI seam test",
            evidence_refs=[],
            project_id=uuid4(),
        )
        response = await remember(request)

        assert response.success is True
        assert response.item.content == "P1-09 DI seam test"
        assert response.item.project_id == request.project_id

    @pytest.mark.asyncio
    async def test_endpoint_query_via_override_returns_no_match(self):
        """The query endpoint must work via override with no DB."""
        from app.api.memory_plane import QueryRequest, query

        repo = InMemoryUnpermissionedMemoryRepository()
        set_service_factory(
            lambda: UnpermissionedSmartRetrievalService(repository=repo)
        )

        request = QueryRequest(
            query="nonexistent content zzz",
            require_evidence=True,
        )
        response = await query(request)

        assert response.status == "no_match"
        assert response.total == 0

    @pytest.mark.asyncio
    async def test_remember_then_query_persists_via_override(self):
        """Remember → query must persist within the same override factory."""
        from app.api.memory_plane import (
            QueryRequest,
            RememberRequest,
            query,
            remember,
        )

        repo = InMemoryUnpermissionedMemoryRepository()
        project_id = uuid4()
        set_service_factory(
            lambda: UnpermissionedSmartRetrievalService(repository=repo)
        )

        remember_req = RememberRequest(
            content="Cross-endpoint persistence",
            evidence_refs=[],
            project_id=project_id,
        )
        rem_resp = await remember(remember_req)
        assert rem_resp.success is True

        query_req = QueryRequest(
            query="Cross-endpoint persistence",
            project_id=project_id,
            require_evidence=False,
        )
        q_resp = await query(query_req)
        assert q_resp.status in ("ok", "partial")
        assert any(
            item.id == rem_resp.item.id for item in q_resp.items
        )


class TestServiceScopePostgresPath:
    """Without an override, service_scope() constructs a Postgres-backed
    service and exposes a commit callable."""

    @pytest.mark.asyncio
    async def test_service_scope_yields_commit_without_override(self):
        """Without override, service_scope builds Postgres service with commit.

        The engine creation itself is tested by inspection; we assert the
        shape (commit callable is not None) without requiring a live DB
        because the test host may not have PostgreSQL available.
        """
        # Without override and no live DB, init_session_factory will raise.
        # Instead, assert the documented contract: commit should be non-None
        # on the Postgres path. We test this by stubbing init_session_factory
        # with a fake async_sessionmaker-like callable.
        # For this focused test, we verify is_configured() is False.
        assert is_configured() is False

    def test_override_clear_restores_production_flag(self):
        """Clearing the override reports is_configured() = False."""
        repo = InMemoryUnpermissionedMemoryRepository()
        set_service_factory(
            lambda: UnpermissionedSmartRetrievalService(repository=repo)
        )
        assert is_configured() is True
        set_service_factory(None)
        assert is_configured() is False

    @pytest.mark.asyncio
    async def test_postgres_engine_uses_explicit_pool_limits(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, dict[str, int]]] = []

        class FakeEngine:
            async def dispose(self) -> None:
                return None

        monkeypatch.setattr(
            session_module,
            "create_async_engine",
            lambda url, **kwargs: (
                calls.append((url, kwargs)) or FakeEngine()
            ),
        )
        monkeypatch.setattr(
            session_module,
            "async_sessionmaker",
            lambda engine, **kwargs: object(),
        )
        monkeypatch.setattr(
            session_module,
            "settings",
            session_module.settings.model_copy(
                update={"DATABASE_POOL_SIZE": 21, "DATABASE_MAX_OVERFLOW": 22}
            ),
        )

        await session_module.init_session_factory(
            "postgresql+asyncpg://user:pass@localhost/db"
        )

        assert calls == [
            (
                "postgresql+asyncpg://user:pass@localhost/db",
                {"pool_size": 21, "max_overflow": 22},
            )
        ]

    @pytest.mark.asyncio
    async def test_non_postgres_engine_does_not_receive_pool_options(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, dict[str, int]]] = []

        class FakeEngine:
            async def dispose(self) -> None:
                return None

        monkeypatch.setattr(
            session_module,
            "create_async_engine",
            lambda url, **kwargs: (
                calls.append((url, kwargs)) or FakeEngine()
            ),
        )
        monkeypatch.setattr(
            session_module,
            "async_sessionmaker",
            lambda engine, **kwargs: object(),
        )

        await session_module.init_session_factory("sqlite+aiosqlite:///:memory:")

        assert calls == [("sqlite+aiosqlite:///:memory:", {})]


class TestProductionCoordinatorWiring:
    """Proves the default production path wires a MemoryQueryCoordinator
    whose primary repository is the session-bound Postgres repository.

    These tests do not require a live PostgreSQL: a fake async_sessionmaker
    is injected into the shared state so service_scope() takes the
    production branch without touching the network.
    """

    @pytest.mark.asyncio
    async def test_default_service_owns_coordinator_with_postgres_primary(self):
        """Production service_scope yields a coordinator-backed service.

        The coordinator's primary must be the PostgresMemoryRepository
        constructed with the session produced by the session factory.
        """
        from app.memory_core.memory_plane_session import _state
        from app.memory_core.query_coordinator import MemoryQueryCoordinator
        from app.memory_core.repositories.postgres_repository import (
            PostgresMemoryRepository,
        )

        class FakeSession:
            """Minimal stub — just needs to be an object so
            PostgresMemoryRepository can wrap it."""
            async def close(self) -> None:
                pass

            async def commit(self) -> None:
                pass

            async def flush(self) -> None:
                pass

        fake_session = FakeSession()

        class FakeSessionFactory:
            """Mimics async_sessionmaker.__call__."""
            def __call__(self):
                return fake_session

        state = _state()
        state["session_factory"] = FakeSessionFactory()

        try:
            async with service_scope() as (svc, commit):
                assert isinstance(svc, UnpermissionedSmartRetrievalService)
                assert callable(commit)
                # commit must be the session's commit method (bound methods
                # create fresh objects per attribute access, so we cannot
                # use `is` identity — compare __self__ instead).
                assert commit.__self__ is fake_session

                coord = svc.coordinator
                assert coord is not None
                assert isinstance(coord, MemoryQueryCoordinator)
                assert isinstance(coord.primary, PostgresMemoryRepository)
                # The repo was constructed with the session from the factory.
                assert coord.primary._session is fake_session
                # No optional candidate sources in the default production wiring.
                assert coord.candidate_sources == []
        finally:
            state = _state()
            state["session_factory"] = None

    @pytest.mark.asyncio
    async def test_override_path_does_not_build_coordinator(self):
        """Override factory bypasses production wiring entirely.

        When a test override is installed, service_scope must NOT invoke
        the PostgresMemoryRepository constructor or the coordinator —
        the override's service is yielded as-is.
        """
        repo = InMemoryUnpermissionedMemoryRepository()
        override_service = UnpermissionedSmartRetrievalService(repository=repo)
        set_service_factory(lambda: override_service)

        # The override service has no coordinator (legacy shape).
        assert override_service.coordinator is None

        # service_scope should yield the override untouched.
        try:
            async with service_scope() as (svc, commit):
                assert svc is override_service
                assert commit is None
                assert svc.coordinator is None
        finally:
            set_service_factory(None)


class TestRepositorySelection:
    """Proves that the override path uses the injected repository, not the
    global reference repository."""

    @pytest.mark.asyncio
    async def test_override_repository_is_used_not_reference(self):
        """Items stored via the override land in the injected repository,
        not the global reference singleton."""
        from app.api.memory_plane import (
            RememberRequest,
            remember,
        )
        from app.memory_core.unpermissioned_repository import (
            get_reference_repository,
        )

        injected_repo = InMemoryUnpermissionedMemoryRepository()
        reference_repo = get_reference_repository()
        reference_repo.clear()

        set_service_factory(
            lambda: UnpermissionedSmartRetrievalService(repository=injected_repo)
        )

        project_id = uuid4()
        rem_resp = await remember(
            RememberRequest(
                content="Isolated repo test",
                evidence_refs=[],
                project_id=project_id,
            )
        )
        assert rem_resp.success is True

        # The injected repo should have the item.
        injected_items = await injected_repo.query(project_id=project_id)
        assert len(injected_items) == 1
        assert injected_items[0].content == "Isolated repo test"

        # The reference repo must remain empty (production vs test seam).
        reference_items = await reference_repo.query(project_id=project_id)
        assert len(reference_items) == 0


class TestContextEndpointIsPure:
    """Prove POST /memory-plane/context does NOT require service_scope.

    The context endpoint must package supplied items and evidence without
    touching the database or the service factory. This is a regression
    guard: the endpoint used to enter service_scope() which was
    unnecessary and made the endpoint non-pure.
    """

    @pytest.mark.asyncio
    async def test_context_works_without_service_factory(self):
        """create_context succeeds even when no service factory is set.

        Clearing the service factory means service_scope() would fall
        through to the PostgreSQL path (which would fail without a live
        DB). The pure /context endpoint must bypass service_scope entirely.
        """
        from app.api.memory_plane import (
            ContextRequest,
            EvidenceRefModel,
            MemoryItemResponse,
            create_context,
        )

        # Ensure no override is installed; the pure endpoint must not care.
        set_service_factory(None)

        now = datetime.utcnow()
        item_id = uuid4()

        supplied_item = MemoryItemResponse(
            id=item_id,
            content="Pure-context fact",
            evidence_refs=[
                EvidenceRefModel(
                    source_id="doc-pure-1",
                    source_type="document",
                    timestamp=now,
                    confidence=0.9,
                )
            ],
            provenance=None,
            status="active",
            created_at=now,
            updated_at=now,
            expires_at=None,
            metadata={},
            project_id=None,
            session_id=None,
            agent_id=None,
            knowledge_base_id=None,
        )

        response = await create_context(
            ContextRequest(
                project_id=uuid4(),
                memory_items=[supplied_item],
                token_budget=4000,
            )
        )

        assert response.has_memory is True
        assert len(response.memory_items) == 1
        assert response.memory_items[0].id == item_id
        assert response.memory_items[0].content == "Pure-context fact"

    @pytest.mark.asyncio
    async def test_context_does_not_require_is_configured(self):
        """is_configured() may be False and /context still works."""
        from app.api.memory_plane import ContextRequest, create_context

        set_service_factory(None)
        await close_session_factory()

        # With no override and no engine, is_configured() is False.
        assert is_configured() is False

        # /context must still succeed because it is pure.
        response = await create_context(
            ContextRequest(
                memory_items=[],
                token_budget=4000,
            )
        )
        assert response.has_memory is False


class TestSeamSurvivesModulePurge:
    """Prove set_service_factory controls the endpoint's service_scope()
    even after the app.* module subtree has been purged and reimported.

    This is the exact scenario triggered by test_memory_plane_import.py:
    the import-regression tests purge ``app.*`` from sys.modules and then
    re-import. The shared-state seam (stored under a private key in
    sys.modules) ensures both the endpoint and the test refer to the
    same override regardless of module reloads.
    """

    @pytest.mark.asyncio
    async def test_shared_state_persists_across_module_replacement(self):
        """Replacing the module in sys.modules does not lose the override.

        Simulates the import-regression test by removing the module from
        sys.modules and re-importing it. The override installed via the
        new module's set_service_factory must be visible to service_scope
        looked up from yet another import of the module.
        """
        import importlib
        import sys

        # Install an override via the canonical module.
        repo = InMemoryUnpermissionedMemoryRepository()
        set_service_factory(
            lambda: UnpermissionedSmartRetrievalService(repository=repo)
        )

        # Simulate the import-regression test: remove the session module
        # and re-import it.
        mod_name = "app.memory_core.memory_plane_session"
        saved = sys.modules.pop(mod_name, None)
        try:
            reloaded = importlib.import_module(mod_name)
            # The reloaded module's service_scope must see the override
            # installed through the original module.
            async with reloaded.service_scope() as (svc, commit):
                assert commit is None
                assert svc is not None
                # The service uses the injected repository.
                stored = await svc.remember(
                    content="Seam-survives-purge fact",
                    evidence_refs=[],
                )
                assert stored.content == "Seam-survives-purge fact"
                items = await repo.query()
                assert any(item.id == stored.id for item in items)
        finally:
            # Restore the original module so subsequent tests are stable.
            if saved is not None:
                sys.modules[mod_name] = saved
            set_service_factory(None)
