"""Session composition seam for the memory-plane API (P1-09).

Owns the SQLAlchemy async engine and ``async_sessionmaker`` used by the
memory-plane endpoints. The engine is created lazily from
``settings.DATABASE_URL`` and disposed only by :func:`close_session_factory`.

A test seam :func:`set_service_factory` lets unit tests inject an
in-memory-backed :class:`UnpermissionedSmartRetrievalService` so endpoint
tests do not require a live PostgreSQL connection. The production path
never silently falls back to the in-memory repository.

State storage
-------------
All mutable state lives in a dict stored under the private key
``_memory_plane_session_state`` in :data:`sys.modules`. This keeps the
state identical across module reloads, so ``set_service_factory`` called
from a test and ``service_scope`` called from a re-imported endpoint
always observe the same override — even when a test purges and reloads
the ``app.*`` subtree.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import (
    Awaitable,
    Callable,
    Optional,
    TypedDict,
    Union,
    cast,
)

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.memory_core.candidate_source import CandidateSource
from app.memory_core.candidate_sources_runtime import (
    CONSTRUCTION_ERRORS,
    CloseCallable,
    CompositionResult,
    close_optional_sources,
)
from app.memory_core.query_coordinator import MemoryQueryCoordinator
from app.memory_core.repositories.postgres_repository import (
    PostgresMemoryRepository,
)
from app.memory_core.unpermissioned_retrieval import (
    UnpermissionedSmartRetrievalService,
)

logger = logging.getLogger(__name__)

ServiceFactory = Callable[[], UnpermissionedSmartRetrievalService]

OptionalSourceFactory = Callable[
    [],
    Union[CompositionResult, Awaitable[CompositionResult]],
]


class SharedState(TypedDict):
    """Typed view over the module's shared mutable state dict.

    The actual storage is a plain ``dict`` kept in :data:`sys.modules`
    so it survives ``importlib``-triggered module reloads.  This class
    exists solely to carry precise field types for static analysis;
    at runtime it is never instantiated — the helpers below operate on
    the raw dict stored under :data:`_SHARED_STATE_KEY`, recovered via
    :func:`cast`.
    """

    engine: AsyncEngine | None
    session_factory: async_sessionmaker[AsyncSession] | None
    override_factory: ServiceFactory | None
    optional_sources: list[CandidateSource] | None
    optional_closers: list[CloseCallable]
    optional_source_factory: OptionalSourceFactory | None


_SHARED_STATE_KEY = "_memory_plane_session_state"


def _state() -> SharedState:
    """Return the module's shared mutable state dict.

    The dict is stored under a private key in :data:`sys.modules` so it
    survives ``importlib``-triggered module reloads (e.g. the import-
    regression tests that purge the ``app.*`` subtree). All functions in
    this module read and write the same dict regardless of which reloaded
    copy of the module they belong to.
    """
    existing_raw: object = sys.modules.get(_SHARED_STATE_KEY)
    if isinstance(existing_raw, dict):
        # Ensure P2 keys exist for backward compat with state dicts
        # created before the optional-source fields were added.
        existing_raw.setdefault("optional_sources", None)
        existing_raw.setdefault("optional_closers", [])
        existing_raw.setdefault("optional_source_factory", None)
        # Route through object: isinstance narrowing on the sys.modules value
        # yields ModuleType & dict, which has no overlap with SharedState.
        return cast(SharedState, cast(object, existing_raw))
    fresh: SharedState = {
        "engine": None,
        "session_factory": None,
        "override_factory": None,
        "optional_sources": None,
        "optional_closers": [],
        "optional_source_factory": None,
    }
    # sys.modules is typed as dict[str, ModuleType | None]; we stash
    # the state dict under a private key for reload-survival.
    _stash = cast(dict[str, object], sys.modules)
    _stash[_SHARED_STATE_KEY] = fresh
    return fresh


def _get_closers(state: SharedState) -> list[CloseCallable]:
    """Return the optional_closers list from *state*, never ``None``."""
    closers = state.get("optional_closers")
    return closers if isinstance(closers, list) else []


def _get_cached_sources(
    state: SharedState,
) -> list[CandidateSource] | None:
    """Return the cached optional_sources, or ``None`` if not yet composed."""
    cached = state.get("optional_sources")
    if isinstance(cached, list):
        return cached
    return None


async def init_session_factory(database_url: Optional[str] = None) -> None:
    """Create the async engine/session factory from ``database_url``.

    Idempotent: a second call is a no-op when the factory is already set.
    The engine is owned by this module and disposed by
    :func:`close_session_factory`.
    """
    state = _state()
    if state["session_factory"] is not None:
        return
    url = database_url or settings.DATABASE_URL
    if not url or not url.strip():
        raise ValueError("database_url must be non-empty")
    engine_options: dict[str, int] = {}
    if make_url(url).get_backend_name() == "postgresql":
        engine_options = {
            "pool_size": settings.DATABASE_POOL_SIZE,
            "max_overflow": settings.DATABASE_MAX_OVERFLOW,
        }
    engine = create_async_engine(url, **engine_options)
    state["engine"] = engine
    state["session_factory"] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    logger.info("memory_plane_session: engine created for %s", url.split("@")[-1])


async def close_session_factory() -> None:
    """Dispose resources owned by this module.

    Only disposes the engine this module created; it never disposes a
    caller-supplied session factory.  Also closes any optional P2
    candidate-source clients (ES, Redis) that were composed by
    :func:`service_scope`.
    """
    state = _state()
    # Close optional P2 candidate-source clients first.
    closers = _get_closers(state)
    if closers:
        await close_optional_sources(closers)
    engine = state.get("engine")
    if engine is not None:
        await engine.dispose()
    state["engine"] = None
    state["session_factory"] = None
    state["override_factory"] = None
    state["optional_sources"] = None
    state["optional_closers"] = []
    state["optional_source_factory"] = None


def set_service_factory(factory: Optional[ServiceFactory]) -> None:
    """Override the service factory used by endpoints.

    Pass ``None`` to clear the override. Used by unit tests to avoid
    the PostgreSQL dependency.
    """
    _state()["override_factory"] = factory


def get_service_factory() -> Optional[ServiceFactory]:
    """Return the currently installed override, if any."""
    return _state()["override_factory"]


def _session_factory_ref() -> Optional[async_sessionmaker[AsyncSession]]:
    return _state()["session_factory"]


async def set_optional_source_factory(
    factory: Optional[OptionalSourceFactory],
) -> None:
    """Test seam: inject a factory that returns ``(sources, closers)``.

    When set, :func:`service_scope` uses this factory instead of the
    real ES/Redis composition.  Pass ``None`` to clear.

    The factory may return ``(Sequence[CandidateSource],
    Sequence[CloseCallable])`` directly or as an awaitable.  Closers may
    be sync or async callables.

    If a previous factory's resources are cached, they are explicitly
    closed before the new factory takes effect — this prevents resource
    leaks when tests swap factories without an intervening
    :func:`close_session_factory`.
    """
    state = _state()
    # Close any cached closers from the previous factory before
    # replacing it.  This prevents resource leaks when a test swaps
    # factories without an explicit close_session_factory() call.
    old_closers = _get_closers(state)
    if old_closers:
        await close_optional_sources(old_closers)
    state["optional_source_factory"] = factory
    # Invalidate cached sources so the new factory takes effect.
    state["optional_sources"] = None
    state["optional_closers"] = []


def get_optional_source_factory() -> Optional[OptionalSourceFactory]:
    """Return the currently installed optional-source factory, if any."""
    return _state()["optional_source_factory"]


@asynccontextmanager
async def service_scope():
    """Yield an :class:`UnpermissionedSmartRetrievalService` bound to a fresh
    PostgreSQL session. The caller owns the commit boundary.

    When :func:`set_service_factory` has been called, the override is used
    instead and no database session is created. The second element of the
    yielded tuple is a ``commit`` callable for the Postgres path and
    ``None`` for the override path (in-memory commits are no-ops).

    When ``settings.MEMORY_CANDIDATE_SOURCES_ENABLED`` is ``True``, optional
    P2 candidate sources (Elasticsearch hybrid, Redis hot-state) are
    composed lazily on first use and passed to the coordinator.  When
    disabled or composition fails, the coordinator falls back to
    PostgreSQL-only with an empty source list.
    """
    state = _state()
    override = state["override_factory"]
    if override is not None:
        yield override(), None
        return

    session_factory = state["session_factory"]
    if session_factory is None:
        await init_session_factory()
        session_factory = state["session_factory"]

    if session_factory is None:
        # Fail closed: init could not produce a session factory.
        # Raise an explicit error rather than dereferencing None.
        raise RuntimeError(
            "memory_plane_session: session factory unavailable after init"
        )

    session: AsyncSession = session_factory()
    try:
        repo = PostgresMemoryRepository(session)

        # Resolve optional P2 candidate sources.
        candidate_sources = await _resolve_optional_sources(state)

        coordinator = MemoryQueryCoordinator(
            primary=repo,
            candidate_sources=candidate_sources,
            source_timeout_s=settings.MEMORY_CANDIDATE_SOURCE_TIMEOUT_S,
        )
        service = UnpermissionedSmartRetrievalService(
            repository=repo, coordinator=coordinator
        )
        yield service, session.commit
    finally:
        await session.close()


async def _resolve_optional_sources(
    state: SharedState,
) -> list[CandidateSource]:
    """Return the candidate sources for the coordinator.

    When ``MEMORY_CANDIDATE_SOURCES_ENABLED`` is ``False``, returns an
    empty list (PostgreSQL-only).  When enabled, composes sources lazily
    on first call and caches them in ``state`` for reuse.  A test seam
    (``optional_source_factory``) can override the composition.

    Only :data:`CONSTRUCTION_ERRORS` are caught — programming errors
    propagate to surface bugs.
    """
    if not settings.MEMORY_CANDIDATE_SOURCES_ENABLED:
        return []

    cached = _get_cached_sources(state)
    if cached is not None:
        return cached

    try:
        factory = state.get("optional_source_factory")
        if factory is not None:
            from app.memory_core.candidate_sources_runtime import (
                _invoke_optional_factory,
            )
            sources, closers = await _invoke_optional_factory(factory)
        else:
            from app.memory_core.candidate_sources_runtime import (
                _build_sources,
            )
            sources, closers = await _build_sources(settings)
    except CONSTRUCTION_ERRORS:
        logger.warning(
            "memory_plane_session: optional source composition failed, "
            "falling back to PostgreSQL-only",
            exc_info=True,
        )
        sources, closers = [], []

    state["optional_sources"] = list(sources)
    state["optional_closers"] = list(closers)
    return list(sources)


def is_configured() -> bool:
    """Return ``True`` when either an override or a Postgres factory is set."""
    state = _state()
    return state["override_factory"] is not None or state["session_factory"] is not None
