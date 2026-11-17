"""Tests for P2 candidate-source runtime wiring.

Proves:
1. Disabled mode: no sources created, coordinator gets empty list.
2. Enabled mode with injected factory: sources propagate to coordinator.
3. Client cleanup: ``close_session_factory()`` calls owned closers.
4. Idempotent close: repeated close calls are safe.
5. Composition failure: fallback to empty source list.
6. Override path is unaffected by candidate-source settings.
7. Typed factory seam: Sequence-returning and async factories work.
8. Partial cleanup: CONSTRUCTION_ERRORS catches expected failures while
   programming errors propagate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.memory_core.candidate_source import (
    CandidateSourceKind,
    CandidateSourceResult,
)
from app.memory_core.memory_plane_session import (
    _state,
    close_session_factory,
    service_scope,
    set_optional_source_factory,
    set_service_factory,
)
from app.memory_core.query_coordinator import MemoryQueryCoordinator


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeCandidateSource:
    """Minimal CandidateSource stub for testing."""

    def __init__(self, kind=CandidateSourceKind.HOT_STATE):
        self._kind = kind

    @property
    def kind(self):
        return self._kind

    async def propose_candidates(self, query):
        return CandidateSourceResult(kind=self._kind, proposals=[])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def _reset():
    """Ensure each test starts clean."""
    await close_session_factory()
    set_service_factory(None)
    await set_optional_source_factory(None)
    yield
    await close_session_factory()
    set_service_factory(None)
    await set_optional_source_factory(None)


class FakeSession:
    async def close(self):
        pass

    async def commit(self):
        pass

    async def flush(self):
        pass


class FakeSessionFactory:
    def __init__(self, session=None):
        self._session = session or FakeSession()

    def __call__(self):
        return self._session


def _install_fake_session_factory():
    """Install a fake session factory so service_scope takes the
    production branch without touching PostgreSQL."""
    state = _state()
    state["session_factory"] = FakeSessionFactory()
    return state


# ---------------------------------------------------------------------------
# Tests: disabled mode
# ---------------------------------------------------------------------------

class TestDisabledMode:
    """When MEMORY_CANDIDATE_SOURCES_ENABLED is False (the default),
    no optional sources are created and the coordinator gets an empty list."""

    @pytest.mark.asyncio
    async def test_disabled_mode_yields_empty_sources(self, monkeypatch):
        """Default setting (False) → coordinator.candidate_sources == []."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            False,
        )
        _install_fake_session_factory()

        async with service_scope() as (svc, commit):
            coord = svc.coordinator
            assert isinstance(coord, MemoryQueryCoordinator)
            assert coord.candidate_sources == []

    @pytest.mark.asyncio
    async def test_disabled_mode_does_not_call_composition(self, monkeypatch):
        """Disabled mode must not invoke _build_sources."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            False,
        )
        _install_fake_session_factory()

        # Install a factory that would fail if called.
        def exploding_factory():
            raise AssertionError("should not be called when disabled")

        await set_optional_source_factory(exploding_factory)

        async with service_scope() as (svc, _commit):
            assert svc.coordinator.candidate_sources == []


# ---------------------------------------------------------------------------
# Tests: enabled mode with injected factory
# ---------------------------------------------------------------------------

class TestEnabledModeWithFactory:
    """When enabled and a test factory is injected, sources propagate
    to the coordinator."""

    @pytest.mark.asyncio
    async def test_enabled_mode_propagates_sources(self, monkeypatch):
        """Enabled + factory → coordinator sees the injected sources."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            True,
        )
        _install_fake_session_factory()

        fake_es = FakeCandidateSource(CandidateSourceKind.HYBRID_SEARCH)
        fake_redis = FakeCandidateSource(CandidateSourceKind.HOT_STATE)
        closer = MagicMock()

        await set_optional_source_factory(
            lambda: ([fake_es, fake_redis], [closer])
        )

        async with service_scope() as (svc, _commit):
            coord = svc.coordinator
            assert len(coord.candidate_sources) == 2
            assert coord.candidate_sources[0] is fake_es
            assert coord.candidate_sources[1] is fake_redis

    @pytest.mark.asyncio
    async def test_enabled_mode_caches_sources(self, monkeypatch):
        """Sources are composed once and cached for subsequent calls."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            True,
        )
        _install_fake_session_factory()

        fake_source = FakeCandidateSource()
        call_count = 0

        def counting_factory():
            nonlocal call_count
            call_count += 1
            return ([fake_source], [])

        await set_optional_source_factory(counting_factory)

        # First call composes.
        async with service_scope() as (svc1, _):
            assert len(svc1.coordinator.candidate_sources) == 1
        assert call_count == 1

        # Second call reuses cached.
        async with service_scope() as (svc2, _):
            assert len(svc2.coordinator.candidate_sources) == 1
        assert call_count == 1  # not called again

    @pytest.mark.asyncio
    async def test_factory_closers_stored_in_state(self, monkeypatch):
        """Closers from the factory are stored for cleanup."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            True,
        )
        _install_fake_session_factory()

        closer = MagicMock()
        await set_optional_source_factory(
            lambda: ([FakeCandidateSource()], [closer])
        )

        async with service_scope() as (_svc, _):
            pass

        state = _state()
        assert len(state["optional_closers"]) == 1
        assert state["optional_closers"][0] is closer


# ---------------------------------------------------------------------------
# Tests: client cleanup
# ---------------------------------------------------------------------------

class TestClientCleanup:
    """close_session_factory() calls all owned closers."""

    @pytest.mark.asyncio
    async def test_close_calls_sync_closers(self, monkeypatch):
        """Sync closers are called by close_session_factory."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            True,
        )
        _install_fake_session_factory()

        closer = MagicMock()
        await set_optional_source_factory(
            lambda: ([FakeCandidateSource()], [closer])
        )

        # Trigger composition.
        async with service_scope() as (_svc, _):
            pass

        assert closer.call_count == 0  # not yet closed

        await close_session_factory()
        assert closer.call_count == 1

    @pytest.mark.asyncio
    async def test_close_calls_async_closers(self, monkeypatch):
        """Async closers are awaited by close_session_factory."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            True,
        )
        _install_fake_session_factory()

        closer = AsyncMock()
        await set_optional_source_factory(
            lambda: ([FakeCandidateSource()], [closer])
        )

        async with service_scope() as (_svc, _):
            pass

        await close_session_factory()
        closer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, monkeypatch):
        """Calling close_session_factory twice is safe."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            True,
        )
        _install_fake_session_factory()

        closer = MagicMock()
        await set_optional_source_factory(
            lambda: ([FakeCandidateSource()], [closer])
        )

        async with service_scope() as (_svc, _):
            pass

        await close_session_factory()
        assert closer.call_count == 1

        # Second close: closers list is already empty.
        await close_session_factory()
        assert closer.call_count == 1  # not called again

    @pytest.mark.asyncio
    async def test_close_handles_closer_error(self, monkeypatch):
        """A failing closer does not prevent other closers from running."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            True,
        )
        _install_fake_session_factory()

        bad_closer = MagicMock(side_effect=RuntimeError("boom"))
        good_closer = MagicMock()

        await set_optional_source_factory(
            lambda: ([FakeCandidateSource()], [bad_closer, good_closer])
        )

        async with service_scope() as (_svc, _):
            pass

        # Should not raise despite bad_closer failing.
        await close_session_factory()
        assert bad_closer.call_count == 1
        assert good_closer.call_count == 1


# ---------------------------------------------------------------------------
# Tests: composition failure fallback
# ---------------------------------------------------------------------------

class TestCompositionFailure:
    """When composition raises, the coordinator falls back to empty."""

    @pytest.mark.asyncio
    async def test_factory_failure_yields_empty_sources(self, monkeypatch):
        """Enabled but factory raises → coordinator gets []."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            True,
        )
        _install_fake_session_factory()

        await set_optional_source_factory(
            lambda: (_ for _ in ()).throw(ImportError("fake")),
        )

        async with service_scope() as (svc, _):
            assert svc.coordinator.candidate_sources == []

    @pytest.mark.asyncio
    async def test_build_sources_failure_yields_empty_sources(self, monkeypatch):
        """Enabled, no factory, _build_sources raises → coordinator gets []."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            True,
        )
        _install_fake_session_factory()

        import app.memory_core.candidate_sources_runtime as rt
        original = rt._build_sources

        def raising_build(s):
            raise ImportError("fake")

        rt._build_sources = raising_build
        try:
            async with service_scope() as (svc, _):
                assert svc.coordinator.candidate_sources == []
        finally:
            rt._build_sources = original


# ---------------------------------------------------------------------------
# Tests: override path unaffected
# ---------------------------------------------------------------------------

class TestOverridePathUnaffected:
    """The override factory path is not affected by candidate-source settings."""

    @pytest.mark.asyncio
    async def test_override_bypasses_sources(self, monkeypatch):
        """Override path yields the override service regardless of setting."""
        from app.memory_core import UnpermissionedSmartRetrievalService
        from app.memory_core.unpermissioned_repository import (
            InMemoryUnpermissionedMemoryRepository,
        )

        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            True,
        )

        repo = InMemoryUnpermissionedMemoryRepository()
        override_svc = UnpermissionedSmartRetrievalService(repository=repo)
        set_service_factory(lambda: override_svc)

        # Even with sources enabled, override path does not compose.
        def exploding_factory():
            raise AssertionError("should not be called for override")

        await set_optional_source_factory(exploding_factory)

        async with service_scope() as (svc, commit):
            assert svc is override_svc
            assert commit is None
            # Override service has no coordinator.
            assert svc.coordinator is None


# ---------------------------------------------------------------------------
# Tests: runtime composition module
# ---------------------------------------------------------------------------

class TestCandidateSourcesRuntime:
    """Tests for the candidate_sources_runtime module directly."""

    @pytest.mark.asyncio
    async def test_close_optional_sources_handles_empty(self):
        """close_optional_sources with empty list is a no-op."""
        from app.memory_core.candidate_sources_runtime import (
            close_optional_sources,
        )
        # Should not raise.
        await close_optional_sources([])

    @pytest.mark.asyncio
    async def test_close_optional_sources_calls_all(self):
        """All closers are called, even if one fails."""
        from app.memory_core.candidate_sources_runtime import (
            close_optional_sources,
        )
        closer1 = MagicMock()
        closer2 = MagicMock(side_effect=RuntimeError("fail"))
        closer3 = MagicMock()

        await close_optional_sources([closer1, closer2, closer3])

        closer1.assert_called_once()
        closer2.assert_called_once()
        closer3.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_optional_sources_awaits_async(self):
        """Async closers are properly awaited."""
        from app.memory_core.candidate_sources_runtime import (
            close_optional_sources,
        )
        closer = AsyncMock()
        await close_optional_sources([closer])
        closer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_optional_sources_accepts_sequence(self):
        """close_optional_sources accepts any Sequence, not just list."""
        from app.memory_core.candidate_sources_runtime import (
            close_optional_sources,
        )
        closer = MagicMock()
        # Pass a tuple (Sequence, not list) — must work.
        await close_optional_sources((closer,))
        closer.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: typed factory seam
# ---------------------------------------------------------------------------

class TestTypedFactorySeam:
    """The factory alias accepts precise Sequence-returning and async
    factories without type ignores."""

    @pytest.mark.asyncio
    async def test_invoke_optional_factory_sync_tuple(self):
        """Sync factory returning a plain tuple is normalised to lists."""
        from app.memory_core.candidate_sources_runtime import (
            _invoke_optional_factory,
        )
        fake = FakeCandidateSource()
        closer = MagicMock()

        # Factory returns a tuple of tuples (Sequence, not list).
        sources, closers = await _invoke_optional_factory(
            lambda: ((fake,), (closer,))
        )

        assert isinstance(sources, list)
        assert isinstance(closers, list)
        assert sources == [fake]
        assert closers == [closer]

    @pytest.mark.asyncio
    async def test_invoke_optional_factory_async(self):
        """Async factory returning an awaitable is awaited."""
        from app.memory_core.candidate_sources_runtime import (
            _invoke_optional_factory,
        )
        fake = FakeCandidateSource()

        async def async_factory():
            return ([fake], [])

        sources, closers = await _invoke_optional_factory(async_factory)

        assert sources == [fake]
        assert closers == []

    @pytest.mark.asyncio
    async def test_invoke_optional_factory_async_with_tuple(self):
        """Async factory returning tuples (not lists) is normalised."""
        from app.memory_core.candidate_sources_runtime import (
            _invoke_optional_factory,
        )
        fake = FakeCandidateSource()
        closer = MagicMock()

        async def async_factory():
            return ((fake,), (closer,))

        sources, closers = await _invoke_optional_factory(async_factory)

        assert isinstance(sources, list)
        assert isinstance(closers, list)
        assert sources == [fake]
        assert closers == [closer]


# ---------------------------------------------------------------------------
# Tests: CONSTRUCTION_ERRORS precision
# ---------------------------------------------------------------------------

class TestConstructionErrorsPrecision:
    """CONSTRUCTION_ERRORS catches expected resource/import failures but
    does not swallow programming errors."""

    @pytest.mark.asyncio
    async def test_construction_errors_catches_import_error(self, monkeypatch):
        """ImportError during composition → fallback to empty."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            True,
        )
        _install_fake_session_factory()

        await set_optional_source_factory(
            lambda: (_ for _ in ()).throw(ImportError("missing dep")),
        )

        async with service_scope() as (svc, _):
            assert svc.coordinator.candidate_sources == []

    @pytest.mark.asyncio
    async def test_construction_errors_catches_type_error(self, monkeypatch):
        """TypeError during composition → fallback to empty."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            True,
        )
        _install_fake_session_factory()

        await set_optional_source_factory(
            lambda: (_ for _ in ()).throw(TypeError("bad arg")),
        )

        async with service_scope() as (svc, _):
            assert svc.coordinator.candidate_sources == []

    @pytest.mark.asyncio
    async def test_construction_errors_catches_connection_error(self, monkeypatch):
        """ConnectionError during composition → fallback to empty."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            True,
        )
        _install_fake_session_factory()

        await set_optional_source_factory(
            lambda: (_ for _ in ()).throw(ConnectionError("refused")),
        )

        async with service_scope() as (svc, _):
            assert svc.coordinator.candidate_sources == []

    @pytest.mark.asyncio
    async def test_programming_errors_propagate(self, monkeypatch):
        """AssertionError is NOT in CONSTRUCTION_ERRORS — it propagates."""
        monkeypatch.setattr(
            "app.memory_core.memory_plane_session.settings.MEMORY_CANDIDATE_SOURCES_ENABLED",
            True,
        )
        _install_fake_session_factory()

        await set_optional_source_factory(
            lambda: (_ for _ in ()).throw(AssertionError("bug")),
        )

        with pytest.raises(AssertionError, match="bug"):
            async with service_scope():
                pass  # pragma: no cover

    def test_construction_errors_does_not_include_programming_errors(self):
        """CONSTRUCTION_ERRORS must not contain AttributeError,
        NotImplementedError, or AssertionError."""
        from app.memory_core.candidate_sources_runtime import (
            CONSTRUCTION_ERRORS,
        )
        assert AttributeError not in CONSTRUCTION_ERRORS
        assert NotImplementedError not in CONSTRUCTION_ERRORS
        assert AssertionError not in CONSTRUCTION_ERRORS
        assert KeyboardInterrupt not in CONSTRUCTION_ERRORS
        assert SystemExit not in CONSTRUCTION_ERRORS

    def test_construction_errors_annotation_is_precise(self):
        """CONSTRUCTION_ERRORS annotation is tuple[type[Exception], ...]."""
        from app.memory_core.candidate_sources_runtime import (
            CONSTRUCTION_ERRORS,
        )
        assert isinstance(CONSTRUCTION_ERRORS, tuple)
        for exc_type in CONSTRUCTION_ERRORS:
            assert isinstance(exc_type, type)
            assert issubclass(exc_type, Exception)
