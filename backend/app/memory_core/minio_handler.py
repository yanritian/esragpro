"""Projection handler for ``target='minio'`` (P1-08).

Implements the ``ProjectionHandler`` protocol.  Delegates the actual
MinIO work to ``MinioProjectionAdapter``.

Never mutates PostgreSQL/outbox state.  Never logs credentials or
raw payloads.
"""

from __future__ import annotations

import logging
from typing import Any

from app.memory_core.projection_worker.contracts import (
    PermanentProjectionError,
    ProjectionEvent,
)
from app.memory_core.repositories.minio_projection.adapter import (
    MinioProjectionAdapter,
    MinioProjectionConflictError,
)
from app.memory_core.repositories.minio_projection.models import (
    PermanentMinioPayloadError,
    validate_evidence_payload,
)
from app.memory_core.repositories.postgres_outbox import (
    build_idempotency_key,
)

logger = logging.getLogger(__name__)


class MinioProjectionHandler:
    """ProjectionHandler for target 'minio'.

    Client ownership:
    - ``create()`` builds an adapter that owns the Minio client
      (``close()`` will release it).
    - Direct construction does NOT close the client unless the adapter
      was built with ``owns_client=True``.
    """

    def __init__(
        self,
        adapter: MinioProjectionAdapter,
    ) -> None:
        if not isinstance(adapter, MinioProjectionAdapter):
            raise TypeError("adapter must be a MinioProjectionAdapter")
        self._adapter = adapter

    @classmethod
    async def create(
        cls,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket_name: str = "memory-evidence",
        *,
        secure: bool = False,
        preflight_timeout_s: float = 5.0,
    ) -> "MinioProjectionHandler":
        """Build a handler with an owned Minio client.

        Performs preflight connectivity check and ensures the bucket exists.
        """
        adapter = await MinioProjectionAdapter.create(
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            bucket_name=bucket_name,
            secure=secure,
            preflight_timeout_s=preflight_timeout_s,
        )
        return cls(adapter)

    @property
    def adapter(self) -> MinioProjectionAdapter:
        return self._adapter

    async def preflight_ping(self, *, timeout_s: float = 5.0) -> dict[str, Any]:
        return await self._adapter.preflight_ping(timeout_s=timeout_s)

    async def close(self) -> None:
        await self._adapter.close()

    async def __call__(self, event: ProjectionEvent) -> None:
        try:
            # Enforce the deterministic outbox idempotency-key contract
            # at the projection boundary.
            _validate_deterministic_idempotency_key(event)

            payload = event.get_payload_copy()
            validated = validate_evidence_payload(
                payload,
                envelope_idempotency_key=event.idempotency_key,
                envelope_aggregate_id=str(event.aggregate_id),
                envelope_event_type=event.event_type,
                envelope_schema_version=event.schema_version,
                envelope_target=event.projection_target,
            )

            try:
                object_key = await self._adapter.project_evidence(
                    validated,
                    idempotency_key=event.idempotency_key,
                )
            except MinioProjectionConflictError as exc:
                raise PermanentProjectionError(
                    f"minio handler: content hash conflict: {exc}",
                ) from exc
            except PermanentMinioPayloadError as exc:
                raise PermanentProjectionError(
                    f"minio handler: payload validation error: {exc}",
                ) from exc
            except PermanentProjectionError:
                raise
            except Exception as exc:
                # Check if it's a MinIO-specific transient error.
                error_name = type(exc).__name__
                if _is_retryable_minio_error(exc):
                    logger.warning(
                        "minio handler: transient error (retryable): %s: %s",
                        error_name,
                        exc,
                    )
                    raise
                # Unknown errors are treated as permanent to avoid infinite
                # retry loops on configuration or data errors.
                raise PermanentProjectionError(
                    f"minio projection: project_evidence failed: {error_name}",
                ) from exc

            logger.debug(
                "minio handler: event=%s memory_id=%s object_key=%s",
                event.outbox_id,
                validated.memory_id,
                object_key,
            )

        except PermanentMinioPayloadError as exc:
            # Payload validation failures are permanent -- they must
            # reach dead-letter immediately with no retry path.
            raise PermanentProjectionError(
                f"minio handler: payload validation error: {exc}",
            ) from exc


def _is_retryable_minio_error(exc: Exception) -> bool:
    """Classify MinIO exceptions as retryable or permanent.

    Connection/timeout/server/transport errors are retryable.
    Client errors (access denied, bad request) are permanent.
    Malformed payload, integrity conflict, and invalid configuration
    errors are permanent.
    """
    from minio.error import S3Error, ServerError

    # Server-side errors are retryable.
    if isinstance(exc, ServerError):
        return True

    # S3Error classification.
    if isinstance(exc, S3Error):
        retryable_codes = {
            "InternalError",
            "ServiceUnavailable",
            "SlowDown",
            "RequestTimeout",
            "ConnectionError",
            "TimeoutError",
        }
        if exc.code in retryable_codes:
            return True

    # Connection/OS-level errors are retryable.
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True

    # urllib3 transport errors (raised by the MinIO SDK's HTTP layer)
    # are retryable.  Covers MaxRetryError, ProtocolError,
    # NewConnectionError, ConnectTimeoutError, ReadTimeoutError, and
    # retry exhaustion.
    try:
        import urllib3.exceptions as _urllib3_exc

        if isinstance(exc, _urllib3_exc.HTTPError):
            return True
    except ImportError:
        pass

    return False


def _validate_deterministic_idempotency_key(event: ProjectionEvent) -> None:
    """Ensure the envelope's idempotency_key matches the deterministic
    outbox derivation for this (aggregate_id, projection_target) pair.

    Raises ``PermanentProjectionError`` on mismatch.
    """
    expected = build_idempotency_key(event.aggregate_id, event.projection_target)
    if event.idempotency_key != expected:
        raise PermanentProjectionError(
            f"minio handler: idempotency_key {event.idempotency_key!r} "
            f"does not match deterministic contract {expected!r} "
            f"for aggregate_id={event.aggregate_id} "
            f"target={event.projection_target}",
        )


__all__ = ["MinioProjectionHandler"]
