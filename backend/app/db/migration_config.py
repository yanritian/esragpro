"""Helpers for safe Alembic migration-test database URL configuration.

The validator is a pure function so tests and ``alembic.env`` share the same
fail-closed safety logic without importing Alembic's runtime environment.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.engine import make_url

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_DANGEROUS_HOST_MARKERS = ("prod", "production", "primary", "master", "live")


class _AlembicConfig(Protocol):
    def set_main_option(self, name: str, value: str) -> None: ...


def set_alembic_database_url(config: _AlembicConfig, database_url: str) -> None:
    """Set the URL while preserving literal percent escapes through ConfigParser."""
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))


def validate_migration_test_database_url(database_url: str | None) -> str:
    """Return a safe isolated PostgreSQL URL or fail without exposing credentials.

    Safety guarantees:
      * driver must be ``postgresql+asyncpg``;
      * host must be loopback (``localhost``, ``127.0.0.1``, ``::1``);
      * host must not contain dangerous markers (``prod``, ``master``, ...);
      * database name must end in ``_test``;
      * query overrides are rejected;
      * credentials are never included in error messages.
    """

    if not database_url or not database_url.strip():
        raise RuntimeError(
            "Migration test safety check failed: field=database_url reason=missing"
        )

    try:
        parsed = make_url(database_url)
    except Exception:
        raise RuntimeError(
            "Migration test safety check failed: field=database_url reason=invalid"
        ) from None

    if parsed.drivername != "postgresql+asyncpg":
        raise RuntimeError(
            "Migration test safety check failed: field=driver reason=asyncpg_required"
        )

    host = (parsed.host or "").lower().rstrip(".")
    if any(marker in host for marker in _DANGEROUS_HOST_MARKERS):
        raise RuntimeError(
            "Migration test safety check failed: field=host reason=dangerous"
        )
    if host not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            "Migration test safety check failed: field=host reason=loopback_required"
        )

    database_name = (parsed.database or "").lower()
    if not database_name.endswith("_test"):
        raise RuntimeError(
            "Migration test safety check failed:"
            " field=database_name reason=_test_suffix_required"
        )

    if parsed.query:
        raise RuntimeError(
            "Migration test safety check failed: field=query reason=overrides_rejected"
        )

    return database_url
