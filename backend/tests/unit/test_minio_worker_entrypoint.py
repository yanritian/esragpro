"""Unit tests for P1-08 MinIO worker entrypoint.

Covers:
- Entrypoint is importable and has a ``main`` callable.
- Required environment variables are validated: missing or blank values
  cause a non-zero exit (``SystemExit(1)``).
- ``_resolve_worker_identity`` returns the explicit env value when set,
  and a hostname-qualified default otherwise.
- ``_parse_secure_flag`` accepts the documented truthy set.
- Integer and float env parsers reject non-numeric and non-positive
  values with ``SystemExit(1)``.

No Docker, no real database, no real MinIO -- the entrypoint's env
validation is tested without invoking the async worker.
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock

import pytest

from app.memory_core import minio_worker_entrypoint as entrypoint_mod
from app.memory_core.minio_worker_entrypoint import (
    _parse_float_env,
    _parse_int_env,
    _parse_secure_flag,
    _require_env,
    _resolve_worker_identity,
    main,
)


class TestEntrypointImportability:
    """Pin that the entrypoint module is importable and exposes main."""

    def test_main_is_callable(self):
        assert callable(main)

    def test_module_is_importable(self):
        import app.memory_core.minio_worker_entrypoint  # noqa: F401


class TestRequireEnv:
    """``_require_env`` must reject missing or blank required vars."""

    def test_missing_env_raises_system_exit(self, monkeypatch):
        monkeypatch.delenv("REQUIRED_TEST_VAR", raising=False)
        with pytest.raises(SystemExit):
            _require_env("REQUIRED_TEST_VAR")

    def test_blank_env_raises_system_exit(self, monkeypatch):
        monkeypatch.setenv("REQUIRED_TEST_VAR", "   ")
        with pytest.raises(SystemExit):
            _require_env("REQUIRED_TEST_VAR")

    def test_present_env_returned_stripped(self, monkeypatch):
        monkeypatch.setenv("REQUIRED_TEST_VAR", "  value  ")
        assert _require_env("REQUIRED_TEST_VAR") == "value"


class TestResolveWorkerIdentity:
    """``_resolve_worker_identity`` explicit vs default."""

    def test_explicit_env_used_when_set(self, monkeypatch):
        monkeypatch.setenv("MINIO_WORKER_IDENTITY", "explicit-worker")
        assert _resolve_worker_identity() == "explicit-worker"

    def test_blank_env_falls_back_to_hostname(self, monkeypatch):
        monkeypatch.setenv("MINIO_WORKER_IDENTITY", "   ")
        identity = _resolve_worker_identity()
        assert identity.startswith("minio-worker-")
        assert socket.gethostname() in identity

    def test_unset_env_falls_back_to_hostname(self, monkeypatch):
        monkeypatch.delenv("MINIO_WORKER_IDENTITY", raising=False)
        identity = _resolve_worker_identity()
        assert identity.startswith("minio-worker-")


class TestParseSecureFlag:
    """``_parse_secure_flag`` truthy set."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1", True),
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("yes", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("no", False),
            ("", False),
        ],
    )
    def test_truthy_and_falsy(self, monkeypatch, value, expected):
        monkeypatch.setenv("MINIO_SECURE", value)
        assert _parse_secure_flag() is expected

    def test_unset_defaults_to_false(self, monkeypatch):
        monkeypatch.delenv("MINIO_SECURE", raising=False)
        assert _parse_secure_flag() is False


class TestParseIntEnv:
    """``_parse_int_env`` accepts valid positive ints; rejects others."""

    def test_valid_positive(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "42")
        assert _parse_int_env("TEST_INT", 10) == 42

    def test_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv("TEST_INT", raising=False)
        assert _parse_int_env("TEST_INT", 10) == 10

    def test_blank_uses_default(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "  ")
        assert _parse_int_env("TEST_INT", 10) == 10

    def test_non_numeric_raises(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "abc")
        with pytest.raises(SystemExit):
            _parse_int_env("TEST_INT", 10)

    def test_zero_raises(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "0")
        with pytest.raises(SystemExit):
            _parse_int_env("TEST_INT", 10)

    def test_negative_raises(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "-1")
        with pytest.raises(SystemExit):
            _parse_int_env("TEST_INT", 10)


class TestParseFloatEnv:
    """``_parse_float_env`` accepts valid floats; rejects non-numeric."""

    def test_valid_float(self, monkeypatch):
        monkeypatch.setenv("TEST_FLOAT", "1.5")
        assert _parse_float_env("TEST_FLOAT", 1.0) == 1.5

    def test_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv("TEST_FLOAT", raising=False)
        assert _parse_float_env("TEST_FLOAT", 1.0) == 1.0

    def test_non_numeric_raises(self, monkeypatch):
        monkeypatch.setenv("TEST_FLOAT", "not-a-number")
        with pytest.raises(SystemExit):
            _parse_float_env("TEST_FLOAT", 1.0)


class TestMainMissingEnv:
    """``main`` must exit non-zero when a required var is absent.

    This pins the startup-failure contract: the process must NOT enter
    the event loop, must NOT invoke the compose root, and must exit 1.
    """

    @pytest.mark.parametrize(
        "missing_var",
        [
            "DATABASE_URL",
            "MINIO_ENDPOINT",
            "MINIO_ACCESS_KEY",
            "MINIO_SECRET_KEY",
        ],
    )
    def test_main_rejects_missing_required_env(self, monkeypatch, missing_var):
        monkeypatch.delenv(missing_var, raising=False)
        with pytest.raises(SystemExit):
            main()

    def test_main_does_not_reach_compose_when_env_missing(self, monkeypatch):
        """When a required var is absent, the compose root is never
        invoked."""
        monkeypatch.delenv("MINIO_SECRET_KEY", raising=False)
        fake_compose = AsyncMock()
        monkeypatch.setattr(
            entrypoint_mod,
            "compose_and_run_minio_worker_from_settings",
            fake_compose,
        )
        with pytest.raises(SystemExit):
            main()
        fake_compose.assert_not_called()


class TestMainInvokesCompose:
    """``main`` passes env values to the compose root."""

    def test_main_passes_env_to_compose(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
        monkeypatch.setenv("MINIO_ENDPOINT", "minio:9000")
        monkeypatch.setenv("MINIO_ACCESS_KEY", "access")
        monkeypatch.setenv("MINIO_SECRET_KEY", "secret")
        monkeypatch.setenv("MINIO_BUCKET_NAME", "custom-bucket")
        monkeypatch.setenv("MINIO_SECURE", "true")
        monkeypatch.setenv("MINIO_WORKER_IDENTITY", "worker-id-x")
        monkeypatch.setenv("MINIO_BATCH_SIZE", "5")
        monkeypatch.setenv("MINIO_IDLE_WAIT_S", "2.0")

        fake_compose = AsyncMock()
        monkeypatch.setattr(
            entrypoint_mod,
            "compose_and_run_minio_worker_from_settings",
            fake_compose,
        )

        main()

        fake_compose.assert_awaited_once()
        call_kwargs = fake_compose.call_args.kwargs
        assert call_kwargs["database_url"] == "postgresql+asyncpg://u:p@h/db"
        assert call_kwargs["minio_endpoint"] == "minio:9000"
        assert call_kwargs["minio_access_key"] == "access"
        assert call_kwargs["minio_secret_key"] == "secret"
        assert call_kwargs["minio_bucket_name"] == "custom-bucket"
        assert call_kwargs["minio_secure"] is True
        assert call_kwargs["worker_identity"] == "worker-id-x"
        assert call_kwargs["batch_size"] == 5
        assert call_kwargs["idle_wait_s"] == 2.0
