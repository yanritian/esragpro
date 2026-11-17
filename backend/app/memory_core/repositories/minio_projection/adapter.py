"""MinIO projection adapter for the memory plane (P1-08).

Projects evidence references as raw-evidence objects in MinIO/S3-compatible
storage. Uses deterministic object keys derived from memory_id and content_hash
to ensure idempotent, restart-persistent storage.

MinIO is a *derived projection* -- PostgreSQL remains the system of record.
No ACL, tenant, principal, or authorization field is added.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from typing import Any, Dict, Optional
from uuid import UUID

from minio import Minio
from minio.error import S3Error, ServerError

from app.memory_core.repositories.minio_projection.models import (
    MinioEvidencePayload,
)

logger = logging.getLogger(__name__)


class MinioProjectionConflictError(Exception):
    """Raised when an object with the same key but different content exists.

    This is a permanent error -- the content hash collision indicates a
    data integrity issue that no retry can fix.
    """


class MinioProjectionAdapter:
    """MinIO adapter for raw-evidence projection.

    Client ownership:
    - ``create()`` builds an adapter that owns the Minio client.
    - ``close()`` releases resources (though Minio client is stateless).
    """

    def __init__(
        self,
        client: Minio,
        bucket_name: str,
        *,
        owns_client: bool = True,
    ) -> None:
        self._client = client
        self._bucket_name = bucket_name
        self._owns_client = owns_client

    @property
    def _connected_client(self) -> Minio:
        """The connected Minio client; raises if the adapter has been closed."""
        if self._client is None:
            raise RuntimeError("Minio client not connected. Call create() first.")
        return self._client

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
    ) -> "MinioProjectionAdapter":
        """Build an adapter with an owned Minio client.

        Performs preflight connectivity check and ensures the bucket exists.
        """
        if not endpoint or not endpoint.strip():
            raise ValueError("endpoint must be non-empty")
        if not access_key or not access_key.strip():
            raise ValueError("access_key must be non-empty")
        if not secret_key or not secret_key.strip():
            raise ValueError("secret_key must be non-empty")
        if not bucket_name or not bucket_name.strip():
            raise ValueError("bucket_name must be non-empty")

        client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )

        adapter = cls(client, bucket_name, owns_client=True)

        # Preflight: check connectivity and ensure bucket exists.
        # On timeout or cancellation, close the owned client and clear
        # its urllib3 pool before propagating, so no connection leak
        # survives a failed construction.
        try:
            await asyncio.wait_for(
                adapter._preflight_check(),
                timeout=preflight_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            await adapter.close()
            raise TimeoutError(
                f"minio preflight check timed out after {preflight_timeout_s}s"
            ) from exc
        except asyncio.CancelledError:
            await adapter.close()
            raise
        except Exception:
            # Close client on preflight failure.
            await adapter.close()
            raise

        return adapter

    async def _preflight_check(self) -> None:
        """Check connectivity and ensure bucket exists."""
        # Run sync operations in thread pool.
        loop = asyncio.get_running_loop()

        # Check if bucket exists.
        bucket_exists = await loop.run_in_executor(
            None,
            self._connected_client.bucket_exists,
            self._bucket_name,
        )

        if not bucket_exists:
            # Create bucket.
            await loop.run_in_executor(
                None,
                self._connected_client.make_bucket,
                self._bucket_name,
            )
            logger.info("Created MinIO bucket: %s", self._bucket_name)

        # Test connectivity with a simple operation.
        # run_in_executor only accepts positional args, so wrap kwargs in a lambda.
        await loop.run_in_executor(
            None,
            lambda: self._connected_client.list_objects(
                self._bucket_name,
                prefix="preflight-check-",
                recursive=False,
            ),
        )

    async def preflight_ping(self, *, timeout_s: float = 5.0) -> dict[str, Any]:
        """Perform a lightweight connectivity check.

        Returns a dict with status information.
        """
        loop = asyncio.get_running_loop()
        try:
            bucket_exists = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    self._connected_client.bucket_exists,
                    self._bucket_name,
                ),
                timeout=timeout_s,
            )
            return {
                "status": "ok",
                "bucket": self._bucket_name,
                "bucket_exists": bucket_exists,
            }
        except asyncio.TimeoutError:
            return {"status": "timeout", "bucket": self._bucket_name}
        except Exception as exc:
            return {"status": "error", "bucket": self._bucket_name, "error": str(exc)}

    async def close(self) -> None:
        """Release resources.

        When the adapter owns the Minio client, explicitly clear the
        underlying urllib3 connection pool (if accessible) before
        dropping the client reference.  The Minio SDK does not expose
        a public ``close()`` method; the pool is reached through the
        private ``_http`` attribute (a ``urllib3.PoolManager``).

        A caller-owned client is never touched -- the caller retains
        full lifecycle responsibility.
        """
        if self._owns_client and self._client is not None:
            http_pool = getattr(self._client, "_http", None)
            if http_pool is not None:
                try:
                    http_pool.clear()
                except Exception as pool_exc:
                    logger.debug(
                        "minio adapter: failed to clear urllib3 pool: %s: %s",
                        type(pool_exc).__name__,
                        pool_exc,
                    )
            self._client = None

    async def _read_and_verify(
        self,
        object_key: str,
        expected_hash: str,
    ) -> bytes:
        """Download an object and verify its SHA-256 content hash.

        Always closes and releases the HTTP response, even on read or
        hash-mismatch errors.  Raises ``RuntimeError`` if the downloaded
        bytes do not match ``expected_hash`` -- this guards against
        silent corruption, tampering, or truncated reads.
        """
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            self._connected_client.get_object,
            self._bucket_name,
            object_key,
        )
        try:
            data = response.read()
        finally:
            response.close()
            response.release_conn()

        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"minio: read-back integrity mismatch for {object_key}: "
                f"expected content_hash={expected_hash!r}, "
                f"actual={actual_hash!r}"
            )
        return data

    async def project_evidence(
        self,
        payload: MinioEvidencePayload,
        *,
        idempotency_key: str,
    ) -> str:
        """Project evidence to MinIO as a raw-evidence object.

        Uses deterministic object key derived from memory_id and content_hash.
        If an object with the same key already exists:
        - Same content hash metadata AND matching bytes: idempotent success
        - Same content hash metadata but mismatched bytes: permanent conflict
          (data integrity issue -- tampered or corrupted stored object)
        - Different content hash metadata: permanent conflict

        Returns the object key.
        """
        object_key = payload.object_key()
        loop = asyncio.get_running_loop()

        # Check if object already exists.
        try:
            existing_stat = await loop.run_in_executor(
                None,
                self._connected_client.stat_object,
                self._bucket_name,
                object_key,
            )

            # Object exists. Verify the FULL stored hash before declaring
            # idempotent success -- the 16-char key prefix alone is not
            # sufficient to guarantee collision freedom.
            existing_metadata = existing_stat.metadata or {}
            existing_full_hash = existing_metadata.get("X-Amz-Meta-Content-Hash")

            if existing_full_hash == payload.content_hash:
                # Metadata hash matches. Verify the actual stored bytes
                # to guard against silent corruption or tampering.
                try:
                    await self._read_and_verify(
                        object_key,
                        payload.content_hash,
                    )
                except RuntimeError as verify_exc:
                    # Metadata says "same hash" but bytes differ --
                    # this is a permanent integrity violation.
                    raise MinioProjectionConflictError(
                        f"minio: object {object_key} metadata hash matches "
                        f"but stored bytes are altered or corrupted: "
                        f"{verify_exc}"
                    ) from verify_exc
                logger.debug(
                    "minio: idempotent upload (object exists with same "
                    "content and verified bytes): %s",
                    object_key,
                )
                return object_key
            elif existing_full_hash is not None:
                # Conflict: same key prefix, different full content hash.
                raise MinioProjectionConflictError(
                    f"minio: object {object_key} exists with different content hash. "
                    f"Stored: {existing_full_hash!r}, computed: {payload.content_hash!r}"
                )
            else:
                # Stored object has no content-hash metadata -- cannot
                # verify identity.  Treat as a permanent conflict rather
                # than silently overwriting.
                raise MinioProjectionConflictError(
                    f"minio: object {object_key} exists but has no content-hash "
                    f"metadata; refusing to overwrite"
                )

        except S3Error as exc:
            if exc.code == "NoSuchKey":
                # Object doesn't exist. Proceed with upload.
                pass
            else:
                # Other S3 error. Check if retryable.
                if _is_retryable_minio_error(exc):
                    raise
                raise MinioProjectionConflictError(
                    f"minio: failed to check existing object: {exc}"
                ) from exc

        # Upload object with metadata.  Store the FULL content hash
        # (not just the 16-char prefix in the key) so that read-back
        # and idempotent re-upload can verify the stored object
        # unambiguously.
        metadata: Dict[str, Any] = {
            "Content-Hash": payload.content_hash,
            "Memory-Id": str(payload.memory_id),
            "Source-Id": payload.source_id,
            "Source-Type": payload.source_type,
            "Idempotency-Key": idempotency_key,
        }

        try:
            await loop.run_in_executor(
                None,
                lambda: self._connected_client.put_object(
                    self._bucket_name,
                    object_key,
                    data=io.BytesIO(payload.evidence_bytes),
                    length=len(payload.evidence_bytes),
                    content_type="application/octet-stream",
                    metadata=metadata,
                ),
            )
            logger.info(
                "minio: uploaded evidence object %s (%d bytes)",
                object_key,
                len(payload.evidence_bytes),
            )
            return object_key

        except S3Error as exc:
            if _is_retryable_minio_error(exc):
                raise
            raise MinioProjectionConflictError(f"minio: failed to upload object: {exc}") from exc

    async def get_evidence(
        self,
        memory_id: UUID,
        content_hash: str,
    ) -> Optional[bytes]:
        """Retrieve raw evidence bytes from MinIO.

        Validates the full content hash of the downloaded bytes before
        returning.  Raises RuntimeError if the stored bytes do not match
        the expected hash (data corruption or tampering).

        Returns None if object doesn't exist.
        """
        object_key = f"memory/{memory_id}/evidence/{content_hash[:16]}"

        try:
            return await self._read_and_verify(object_key, content_hash)
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return None
            if _is_retryable_minio_error(exc):
                raise
            raise

    async def delete_evidence(
        self,
        memory_id: UUID,
        content_hash: str,
    ) -> bool:
        """Delete raw evidence object from MinIO.

        Returns True if deleted, False if didn't exist.
        """
        object_key = f"memory/{memory_id}/evidence/{content_hash[:16]}"
        loop = asyncio.get_running_loop()

        try:
            await loop.run_in_executor(
                None,
                self._connected_client.remove_object,
                self._bucket_name,
                object_key,
            )
            logger.info("minio: deleted evidence object %s", object_key)
            return True

        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return False
            if _is_retryable_minio_error(exc):
                raise
            raise


def _is_retryable_minio_error(exc: Exception) -> bool:
    """Classify MinIO/S3 exceptions as retryable or permanent.

    Connection/timeout/server/transport errors are retryable.
    Client errors (access denied, bad request) are permanent.
    Malformed payload, integrity conflict, and invalid configuration
    errors are permanent.
    """
    # Server-side errors are retryable.
    if isinstance(exc, ServerError):
        return True

    # S3Error classification.
    if isinstance(exc, S3Error):
        # Retryable: server errors, timeouts, throttling.
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

        # Permanent: client errors.
        permanent_codes = {
            "AccessDenied",
            "NoSuchBucket",
            "InvalidBucketName",
            "NoSuchKey",
            "InvalidAccessKeyId",
            "SignatureDoesNotMatch",
        }
        if exc.code in permanent_codes:
            return False

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


__all__ = [
    "MinioProjectionAdapter",
    "MinioProjectionConflictError",
]
