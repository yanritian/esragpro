"""Production worker entrypoint for P1-08 MinIO projection.

Provides a ``python -m app.memory_core.minio_worker_entrypoint`` command
that loads environment configuration, validates required settings, and
runs the MinIO projection polling worker with clean signal handling.

Required environment variables:
    DATABASE_URL          -- async SQLAlchemy URL for PostgreSQL
    MINIO_ENDPOINT        -- MinIO/S3 endpoint (host:port)
    MINIO_ACCESS_KEY      -- MinIO access key
    MINIO_SECRET_KEY      -- MinIO secret key

Optional environment variables:
    MINIO_BUCKET_NAME     -- MinIO bucket name (default: memory-evidence)
    MINIO_SECURE          -- Use HTTPS for MinIO (default: false)
    MINIO_WORKER_IDENTITY -- Worker identity string
                             (default: minio-worker-{hostname})
    MINIO_BATCH_SIZE      -- Max events per claim (default: 10)
    MINIO_IDLE_WAIT_S     -- Seconds between empty polls (default: 1.0)

The entrypoint exits non-zero on startup failure (missing env, invalid
configuration, preflight failure) and handles SIGINT/SIGTERM for clean
shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from app.memory_core.minio_compose import (
    compose_and_run_minio_worker_from_settings,
)

logger = logging.getLogger(__name__)


def _resolve_worker_identity() -> str:
    """Return a stable, host-qualified worker identity.

    Uses ``MINIO_WORKER_IDENTITY`` if set; otherwise falls back to
    ``minio-worker-{hostname}`` so that multiple containers on
    different hosts produce distinct identities.
    """
    explicit = os.environ.get("MINIO_WORKER_IDENTITY", "").strip()
    if explicit:
        return explicit
    import socket

    return f"minio-worker-{socket.gethostname()}"


def _require_env(name: str) -> str:
    """Return a non-empty env value or raise ``SystemExit``."""
    value = os.environ.get(name, "").strip()
    if not value:
        logger.error(
            "minio worker: required environment variable %s is not set",
            name,
        )
        raise SystemExit(1)
    return value


def _parse_secure_flag() -> bool:
    """Parse ``MINIO_SECURE`` into a boolean (default False)."""
    raw = os.environ.get("MINIO_SECURE", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _parse_int_env(name: str, default: int) -> int:
    """Parse an integer env var with a fallback default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        logger.error(
            "minio worker: %s=%r is not a valid integer",
            name,
            raw,
        )
        raise SystemExit(1) from exc
    if value <= 0:
        logger.error(
            "minio worker: %s=%d must be positive",
            name,
            value,
        )
        raise SystemExit(1)
    return value


def _parse_float_env(name: str, default: float) -> float:
    """Parse a float env var with a fallback default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        logger.error(
            "minio worker: %s=%r is not a valid number",
            name,
            raw,
        )
        raise SystemExit(1) from exc


def main() -> None:
    """Synchronous entrypoint: validate env, then run the async worker."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Validate required settings BEFORE entering the event loop.
    database_url = _require_env("DATABASE_URL")
    minio_endpoint = _require_env("MINIO_ENDPOINT")
    minio_access_key = _require_env("MINIO_ACCESS_KEY")
    minio_secret_key = _require_env("MINIO_SECRET_KEY")

    worker_identity = _resolve_worker_identity()
    minio_bucket = os.environ.get("MINIO_BUCKET_NAME", "memory-evidence").strip()
    minio_secure = _parse_secure_flag()
    batch_size = _parse_int_env("MINIO_BATCH_SIZE", 10)
    idle_wait_s = _parse_float_env("MINIO_IDLE_WAIT_S", 1.0)

    exit_code = 0
    try:
        asyncio.run(
            _run_worker(
                database_url=database_url,
                minio_endpoint=minio_endpoint,
                minio_access_key=minio_access_key,
                minio_secret_key=minio_secret_key,
                minio_bucket=minio_bucket,
                minio_secure=minio_secure,
                worker_identity=worker_identity,
                batch_size=batch_size,
                idle_wait_s=idle_wait_s,
            )
        )
    except SystemExit:
        raise
    except KeyboardInterrupt:
        logger.info("minio worker: interrupted by keyboard")
    except Exception as exc:
        logger.error(
            "minio worker: startup failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        exit_code = 1
    if exit_code:
        sys.exit(exit_code)


async def _run_worker(
    *,
    database_url: str,
    minio_endpoint: str,
    minio_access_key: str,
    minio_secret_key: str,
    minio_bucket: str,
    minio_secure: bool,
    worker_identity: str,
    batch_size: int,
    idle_wait_s: float,
) -> None:
    """Async worker body with signal handling."""
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    # asyncio signal handlers are only available on Unix.
    if sys.platform != "win32":
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(__import__("signal"), sig_name, None)
            if sig is not None:
                try:
                    loop.add_signal_handler(
                        sig,
                        lambda s=sig_name: (
                            logger.info(
                                "minio worker: received %s, stopping",
                                s,
                            ),
                            stop_event.set(),
                        ),
                    )
                except (NotImplementedError, RuntimeError) as sig_exc:
                    logger.debug(
                        "minio worker: could not install %s handler: %s",
                        sig_name,
                        sig_exc,
                    )

    try:
        await compose_and_run_minio_worker_from_settings(
            database_url=database_url,
            minio_endpoint=minio_endpoint,
            minio_access_key=minio_access_key,
            minio_secret_key=minio_secret_key,
            minio_bucket_name=minio_bucket,
            minio_secure=minio_secure,
            worker_identity=worker_identity,
            batch_size=batch_size,
            idle_wait_s=idle_wait_s,
            stop_event=stop_event,
        )
    except asyncio.CancelledError:
        logger.info("minio worker: cancelled")
        raise


__all__ = ["main"]


if __name__ == "__main__":
    main()
