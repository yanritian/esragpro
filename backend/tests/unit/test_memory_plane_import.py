"""
Import regression tests for the memory-plane vertical path.

These tests prove that:
1. ``app.api.memory_plane`` is importable even when ``app.db.session`` is missing.
2. The legacy routers that depend on ``app.db.session`` degrade gracefully
   (set to ``None``) instead of breaking the entire ``app.api`` package.
3. ``app.main`` creates a FastAPI application with the memory-plane routes
   registered regardless of legacy router availability.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

# Path to the real app package in the repository
_REAL_APP_PATH = str(Path(__file__).parent.parent.parent / "app")


def _purge_subtree(prefix: str):
    """Remove all loaded modules under *prefix* from sys.modules.

    This forces a fresh import so we can observe import-time behaviour
    (including expected failures) without caching effects.
    """
    to_remove = [name for name in sys.modules if name == prefix or name.startswith(prefix + ".")]
    for name in to_remove:
        del sys.modules[name]


def _snapshot_sys_modules() -> dict[str, types.ModuleType]:
    """Return a shallow copy of ``sys.modules`` (key → module object)."""
    return dict(sys.modules)


def _restore_sys_modules(snapshot: dict[str, types.ModuleType]) -> None:
    """Restore ``sys.modules`` to match *snapshot* exactly.

    Removes keys added after the snapshot and restores both deleted keys
    and keys whose module object was replaced — using identity (``is``),
    not just key presence.  A key-only restore leaks stub module objects
    into later tests, breaking ``isinstance`` and ``pytest.raises`` checks.
    """
    current_keys = set(sys.modules)
    snapshot_keys = set(snapshot)
    # Remove keys that were added after the snapshot.
    for key in current_keys - snapshot_keys:
        sys.modules.pop(key, None)
    # Restore keys that were deleted or whose module object was replaced.
    for key, mod in snapshot.items():
        if sys.modules.get(key) is not mod:
            sys.modules[key] = mod


@pytest.fixture(autouse=True)
def _restore_modules():
    """Snapshot sys.modules before each test and restore it afterwards."""
    snapshot = _snapshot_sys_modules()
    yield
    _restore_sys_modules(snapshot)


class TestMemoryPlaneImportWithoutDbSession:
    """app.api.memory_plane must import when app.db.session is absent."""

    def test_memory_plane_importable_without_db_session(self):
        """Importing app.api.memory_plane must succeed even when
        app.db.session does not exist."""
        # Purge any cached state so the test is deterministic
        _purge_subtree("app")

        # Install a stub app package that points to the real app directory,
        # so Python can locate app.api and other real submodules.
        app_pkg = types.ModuleType("app")
        app_pkg.__path__ = [_REAL_APP_PATH]  # point to real app directory
        sys.modules["app"] = app_pkg

        # Install a stub app.db package that points to the real db directory.
        # The real db directory exists but has no session.py module.
        db_pkg = types.ModuleType("app.db")
        real_db_path = Path(_REAL_APP_PATH) / "db"
        db_pkg.__path__ = [str(real_db_path)] if real_db_path.exists() else []
        sys.modules["app.db"] = db_pkg
        app_pkg.db = db_pkg

        # Ensure importing app.api does NOT raise even though
        # app.db.session is missing.
        try:
            api_pkg = importlib.import_module("app.api")
        except Exception as exc:
            pytest.fail(f"app.api import failed when app.db.session is missing: {exc}")

        # memory_plane_router must be present and non-None
        assert hasattr(api_pkg, "memory_plane_router")
        assert api_pkg.memory_plane_router is not None

    def test_legacy_routers_are_none_without_db_session(self):
        """Legacy routers that need app.db.session should be None
        when the module is unavailable, not raise."""
        _purge_subtree("app")

        app_pkg = types.ModuleType("app")
        app_pkg.__path__ = [_REAL_APP_PATH]
        sys.modules["app"] = app_pkg

        db_pkg = types.ModuleType("app.db")
        real_db_path = Path(_REAL_APP_PATH) / "db"
        db_pkg.__path__ = [str(real_db_path)] if real_db_path.exists() else []
        sys.modules["app.db"] = db_pkg
        app_pkg.db = db_pkg

        api_pkg = importlib.import_module("app.api")

        # Legacy routers should be None (graceful degradation)
        assert api_pkg.memory_router is None
        assert api_pkg.task_router is None
        assert api_pkg.smart_search_router is None


class TestMainApplicationImportable:
    """app.main must be importable and create the FastAPI app."""

    def test_main_creates_app_with_memory_plane(self):
        """app.main:app exists and includes memory-plane routes."""
        _purge_subtree("app")

        # Stub the app package to point to the real app directory
        app_pkg = types.ModuleType("app")
        app_pkg.__path__ = [_REAL_APP_PATH]
        sys.modules["app"] = app_pkg

        # Stub app.db to point to the real db directory (which has no session.py)
        db_pkg = types.ModuleType("app.db")
        real_db_path = Path(_REAL_APP_PATH) / "db"
        db_pkg.__path__ = [str(real_db_path)] if real_db_path.exists() else []
        sys.modules["app.db"] = db_pkg
        app_pkg.db = db_pkg

        try:
            main_mod = importlib.import_module("app.main")
        except Exception as exc:
            pytest.fail(f"app.main import failed: {exc}")

        fastapi_app = main_mod.app
        assert fastapi_app is not None

        # Collect all registered route paths, including those inside
        # FastAPI's _IncludedRouter wrappers (used in newer FastAPI versions).
        route_paths = []
        for route in fastapi_app.routes:
            # Direct routes have a .path attribute
            if hasattr(route, "path") and route.path:
                route_paths.append(route.path)
            # _IncludedRouter objects wrap the actual router
            # Access the original router's routes to get real paths
            if hasattr(route, "original_router") and hasattr(route.original_router, "routes"):
                for inner_route in route.original_router.routes:
                    if hasattr(inner_route, "path") and inner_route.path:
                        route_paths.append(inner_route.path)

        # Memory-plane endpoints must be registered
        memory_plane_paths = [p for p in route_paths if "/memory-plane/" in p]
        assert len(memory_plane_paths) >= 3, (
            f"Expected at least 3 memory-plane routes, got: {memory_plane_paths}"
        )


class TestModuleIdentityRestoration:
    """Regression guard: ``_restore_sys_modules`` must restore exact
    module-object identity, not just key presence.

    Without identity-aware restoration, a stub module installed into
    ``sys.modules`` during one test leaks into later tests.  Exception
    classes looked up through the stub are different objects from the
    real ones, breaking ``isinstance`` and ``pytest.raises`` checks.

    This test is self-contained — it snapshots, replaces, restores, and
    asserts all within a single test function.  No module-level mutable
    holders, no reliance on test ordering.
    """

    def test_restore_replaces_stub_with_original_module_object(self):
        """_restore_sys_modules must put back the exact module object
        that was present at snapshot time, even when a key is still
        occupied by a different (stub) module object."""
        # Ensure the real app module is loaded before snapshotting.
        import app as real_app_module
        import app.db as real_app_db_module

        # Snapshot with the guarantee that "app" and "app.db" are present.
        snapshot = _snapshot_sys_modules()
        assert "app" in snapshot, "app must be in sys.modules after import"
        assert snapshot["app"] is real_app_module

        # Replace sys.modules entries with stubs (keys stay present).
        stub_app = types.ModuleType("app")
        stub_app.__path__ = [_REAL_APP_PATH]
        sys.modules["app"] = stub_app

        stub_db = types.ModuleType("app.db")
        real_db_path = Path(_REAL_APP_PATH) / "db"
        stub_db.__path__ = [str(real_db_path)] if real_db_path.exists() else []
        sys.modules["app.db"] = stub_db

        # Keys are present but point at stubs — identity differs from snapshot.
        assert sys.modules["app"] is stub_app
        assert sys.modules["app"] is not real_app_module

        # Invoke the restore function.
        _restore_sys_modules(snapshot)

        # Exact identity must be restored — not just key presence.
        assert sys.modules["app"] is real_app_module, (
            "sys.modules['app'] was not restored to the original module object"
        )
        assert sys.modules["app.db"] is real_app_db_module, (
            "sys.modules['app.db'] was not restored to the original module object"
        )

    def test_restore_removes_keys_added_after_snapshot(self):
        """_restore_sys_modules must remove keys that didn't exist at
        snapshot time."""
        snapshot = _snapshot_sys_modules()

        # Add a new key that wasn't in the snapshot.
        sentinel_key = "__test_restore_sentinel__"
        sys.modules[sentinel_key] = types.ModuleType(sentinel_key)
        assert sentinel_key in sys.modules

        _restore_sys_modules(snapshot)

        assert sentinel_key not in sys.modules
