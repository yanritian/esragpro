"""Unit tests for MinIO projection adapter (P1-08).

Covers:
- Adapter creation and preflight
- Evidence projection (idempotent, conflict detection)
- Evidence retrieval and deletion
- Error classification (retryable vs permanent)
"""

from __future__ import annotations

import asyncio
from unittest.mock import Mock
from uuid import uuid4

import pytest
from minio.error import S3Error, ServerError

from app.memory_core.repositories.minio_projection.adapter import (
    MinioProjectionAdapter,
    MinioProjectionConflictError,
    _is_retryable_minio_error,
)
from app.memory_core.repositories.minio_projection.models import (
    MinioEvidencePayload,
    compute_content_hash,
)


def _make_s3_error(
    code: str,
    message: str = "error",
    *,
    resource: str = "/bucket/key",
    request_id: str = "req-123",
    host_id: str = "host-123",
) -> S3Error:
    """Build a real S3Error instance compatible with the MinIO 7.2 SDK.

    MinIO 7.2 requires a ``response`` object as the first positional
    argument and does NOT accept ``response_code`` as a keyword.
    """
    mock_response = Mock()
    return S3Error(
        response=mock_response,
        code=code,
        message=message,
        resource=resource,
        request_id=request_id,
        host_id=host_id,
    )


def _make_payload(
    *,
    memory_id=None,
    evidence_bytes: bytes = b"test evidence",
) -> MinioEvidencePayload:
    """Build a test evidence payload."""
    mid = memory_id or uuid4()
    return MinioEvidencePayload(
        memory_id=mid,
        evidence_bytes=evidence_bytes,
        content_hash=compute_content_hash(evidence_bytes),
        source_id="doc-123",
        source_type="document",
        timestamp="2026-01-01T00:00:00+00:00",
        chunk_id="chunk-0",
        confidence=0.95,
        metadata={"page": 1},
    )


class TestIsRetryableMinioError:
    """Test error classification."""

    def test_server_error_retryable(self):
        """ServerError is retryable."""
        exc = ServerError("server error", 500)
        assert _is_retryable_minio_error(exc) is True

    def test_s3_error_internal_retryable(self):
        """S3Error with InternalError code is retryable."""
        exc = _make_s3_error("InternalError", "internal error")
        assert _is_retryable_minio_error(exc) is True

    def test_s3_error_access_denied_permanent(self):
        """S3Error with AccessDenied code is permanent."""
        exc = _make_s3_error("AccessDenied", "access denied")
        assert _is_retryable_minio_error(exc) is False

    def test_connection_error_retryable(self):
        """ConnectionError is retryable."""
        exc = ConnectionError("connection failed")
        assert _is_retryable_minio_error(exc) is True

    def test_timeout_error_retryable(self):
        """TimeoutError is retryable."""
        exc = TimeoutError("timeout")
        assert _is_retryable_minio_error(exc) is True

    def test_os_error_retryable(self):
        """OSError is retryable."""
        exc = OSError("os error")
        assert _is_retryable_minio_error(exc) is True

    def test_generic_error_permanent(self):
        """Generic Exception is permanent."""
        exc = Exception("generic error")
        assert _is_retryable_minio_error(exc) is False

    def test_urllib3_max_retry_error_retryable(self):
        """urllib3 MaxRetryError (real transport exhaustion) is retryable.

        Simulates the actual exception the MinIO SDK raises when all
        connection retries to the MinIO server are exhausted.
        """
        import urllib3.exceptions

        exc = urllib3.exceptions.MaxRetryError(
            pool=object(),
            url="http://localhost:9000/minio/health/live",
        )
        assert _is_retryable_minio_error(exc) is True

    def test_urllib3_protocol_error_retryable(self):
        """urllib3 ProtocolError (connection reset/broken) is retryable."""
        import urllib3.exceptions

        exc = urllib3.exceptions.ProtocolError("Connection aborted.")
        assert _is_retryable_minio_error(exc) is True


class TestMinioProjectionAdapter:
    """Test MinioProjectionAdapter."""

    @pytest.mark.asyncio
    async def test_create_validates_endpoint(self):
        """create() validates endpoint parameter."""
        with pytest.raises(ValueError, match="endpoint"):
            await MinioProjectionAdapter.create(
                endpoint="",
                access_key="key",
                secret_key="secret",
            )

    @pytest.mark.asyncio
    async def test_create_validates_access_key(self):
        """create() validates access_key parameter."""
        with pytest.raises(ValueError, match="access_key"):
            await MinioProjectionAdapter.create(
                endpoint="localhost:9000",
                access_key="",
                secret_key="secret",
            )

    @pytest.mark.asyncio
    async def test_create_validates_secret_key(self):
        """create() validates secret_key parameter."""
        with pytest.raises(ValueError, match="secret_key"):
            await MinioProjectionAdapter.create(
                endpoint="localhost:9000",
                access_key="key",
                secret_key="",
            )

    @pytest.mark.asyncio
    async def test_create_validates_bucket_name(self):
        """create() validates bucket_name parameter."""
        with pytest.raises(ValueError, match="bucket_name"):
            await MinioProjectionAdapter.create(
                endpoint="localhost:9000",
                access_key="key",
                secret_key="secret",
                bucket_name="",
            )

    @pytest.mark.asyncio
    async def test_close_idempotent(self):
        """close() is idempotent."""
        adapter = MinioProjectionAdapter(
            client=Mock(),
            bucket_name="test-bucket",
            owns_client=True,
        )
        await adapter.close()
        await adapter.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_preflight_check_calls_list_objects_with_kwargs(self):
        """_preflight_check() passes prefix/recursive kwargs to list_objects.

        Regression test: run_in_executor only accepts positional args, so
        the adapter must wrap kwargs in a callable (lambda/partial) rather
        than passing them directly to run_in_executor.
        """
        mock_client = Mock()
        mock_client.bucket_exists.return_value = True

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=False,
        )

        await adapter._preflight_check()

        mock_client.list_objects.assert_called_once_with(
            "test-bucket",
            prefix="preflight-check-",
            recursive=False,
        )

    @pytest.mark.asyncio
    async def test_project_evidence_idempotent(self):
        """project_evidence() is idempotent for same content.

        When metadata hash matches, the adapter downloads the object and
        verifies its SHA-256 before declaring idempotent success.
        """
        evidence_bytes = b"test"
        expected_hash = compute_content_hash(evidence_bytes)

        mock_client = Mock()
        mock_client.stat_object.return_value = Mock(
            metadata={"X-Amz-Meta-Content-Hash": expected_hash}
        )

        # The idempotent path downloads and verifies the stored bytes.
        mock_response = Mock()
        mock_response.read.return_value = evidence_bytes
        mock_client.get_object.return_value = mock_response

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=False,
        )

        payload = MinioEvidencePayload(
            memory_id=uuid4(),
            evidence_bytes=evidence_bytes,
            content_hash=expected_hash,
            source_id="doc-123",
            source_type="document",
            timestamp="2026-01-01T00:00:00+00:00",
            chunk_id="chunk-0",
            confidence=0.95,
            metadata={"page": 1},
        )

        key = await adapter.project_evidence(
            payload,
            idempotency_key="test-key",
        )

        assert key is not None
        # Should NOT call put_object since content matches.
        mock_client.put_object.assert_not_called()
        # Should have downloaded and verified the stored bytes.
        mock_client.get_object.assert_called_once()
        # Response must be closed and released.
        mock_response.close.assert_called_once()
        mock_response.release_conn.assert_called_once()

    @pytest.mark.asyncio
    async def test_project_evidence_conflict(self):
        """project_evidence() raises conflict for different content."""
        mock_client = Mock()
        mock_client.stat_object.return_value = Mock(
            metadata={"X-Amz-Meta-Content-Hash": "different-hash"}
        )

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=False,
        )

        payload = _make_payload(evidence_bytes=b"test")

        with pytest.raises(MinioProjectionConflictError, match="different content"):
            await adapter.project_evidence(
                payload,
                idempotency_key="test-key",
            )

    @pytest.mark.asyncio
    async def test_project_evidence_shortened_key_collision_raises(self):
        """Same 16-char key prefix with different full hash raises conflict.

        The object key uses only the first 16 hex chars of content_hash,
        so two different contents could theoretically map to the same key.
        The adapter guards against this by reading back the full stored
        hash from metadata and comparing it to the computed full hash.
        A mismatch must raise MinioProjectionConflictError.
        """
        mock_client = Mock()

        # Simulate stored object whose full hash shares the first 16 chars
        # with the payload's content_hash but differs after that.
        stored_full_hash = "abcdef0123456789" + "F" * 48  # 64 chars total
        mock_client.stat_object.return_value = Mock(
            metadata={"X-Amz-Meta-Content-Hash": stored_full_hash}
        )

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=False,
        )

        # Build a payload whose content_hash has the SAME first 16 chars
        # as stored_full_hash but a different trailing portion.
        payload_full_hash = "abcdef0123456789" + "0" * 48  # different suffix
        payload = MinioEvidencePayload(
            memory_id=uuid4(),
            evidence_bytes=b"some content",
            content_hash=payload_full_hash,
            source_id="doc-123",
            source_type="document",
            timestamp="2026-01-01T00:00:00+00:00",
            chunk_id="chunk-0",
            confidence=0.95,
            metadata={"page": 1},
        )

        # Sanity check: same 16-char prefix means same object key.
        assert payload.object_key().endswith(payload_full_hash[:16])
        assert payload.object_key().endswith(stored_full_hash[:16])

        with pytest.raises(MinioProjectionConflictError, match="different content hash"):
            await adapter.project_evidence(
                payload,
                idempotency_key="test-key",
            )

        # put_object must NOT be called -- conflict is permanent.
        mock_client.put_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_project_evidence_new_object(self):
        """project_evidence() uploads new object when none exists."""
        mock_client = Mock()

        # Simulate NoSuchKey error
        no_such_key_error = _make_s3_error("NoSuchKey", "key not found")
        mock_client.stat_object.side_effect = no_such_key_error

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=False,
        )

        payload = _make_payload(evidence_bytes=b"test evidence")

        key = await adapter.project_evidence(
            payload,
            idempotency_key="test-key",
        )

        assert key is not None
        mock_client.put_object.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_evidence_returns_bytes(self):
        """get_evidence() returns evidence bytes when hash matches."""
        mock_client = Mock()
        mock_response = Mock()
        evidence_bytes = b"test evidence"
        mock_response.read.return_value = evidence_bytes
        mock_client.get_object.return_value = mock_response

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=False,
        )

        memory_id = uuid4()
        content_hash = compute_content_hash(evidence_bytes)

        data = await adapter.get_evidence(memory_id, content_hash)

        assert data == evidence_bytes
        mock_client.get_object.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_evidence_not_found(self):
        """get_evidence() returns None for missing object."""
        mock_client = Mock()

        no_such_key_error = _make_s3_error("NoSuchKey", "key not found")
        mock_client.get_object.side_effect = no_such_key_error

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=False,
        )

        memory_id = uuid4()
        content_hash = "a" * 64

        data = await adapter.get_evidence(memory_id, content_hash)

        assert data is None

    @pytest.mark.asyncio
    async def test_get_evidence_hash_mismatch_raises(self):
        """get_evidence() raises RuntimeError on read-back hash mismatch.

        Guards against data corruption, tampering, or truncated reads
        where the stored bytes do not match the expected full content hash.
        Idempotent replay must NOT accept altered bytes.
        """
        mock_client = Mock()
        mock_response = Mock()
        # Return bytes that do NOT match the expected content hash.
        mock_response.read.return_value = b"altered or corrupted bytes"
        mock_client.get_object.return_value = mock_response

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=False,
        )

        memory_id = uuid4()
        # This is the hash of the ORIGINAL (correct) content, not the
        # corrupted bytes returned by the mock.
        expected_hash = compute_content_hash(b"original uncorrupted content")

        with pytest.raises(RuntimeError, match="read-back integrity mismatch"):
            await adapter.get_evidence(memory_id, expected_hash)

    @pytest.mark.asyncio
    async def test_delete_evidence_success(self):
        """delete_evidence() returns True on success."""
        mock_client = Mock()

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=False,
        )

        memory_id = uuid4()
        content_hash = "a" * 64

        result = await adapter.delete_evidence(memory_id, content_hash)

        assert result is True
        mock_client.remove_object.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_evidence_not_found(self):
        """delete_evidence() returns False for missing object."""
        mock_client = Mock()

        no_such_key_error = _make_s3_error("NoSuchKey", "key not found")
        mock_client.remove_object.side_effect = no_such_key_error

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=False,
        )

        memory_id = uuid4()
        content_hash = "a" * 64

        result = await adapter.delete_evidence(memory_id, content_hash)

        assert result is False


class TestByteLevelTampering:
    """Byte-level idempotency integrity.

    When the stored object's metadata hash matches the payload's content
    hash, the adapter must still download and SHA-256 verify the actual
    bytes.  If the bytes are altered or corrupted (a tampered replay),
    the adapter must raise ``MinioProjectionConflictError`` instead of
    returning idempotent success.
    """

    @pytest.mark.asyncio
    async def test_idempotent_metadata_match_but_bytes_altered_raises(self):
        """Metadata hash matches but downloaded bytes differ → conflict.

        This is the tampered-replay guard: the stored object has the
        correct content-hash in its metadata, but the actual bytes have
        been altered.  The adapter must NOT silently accept.
        """
        evidence_bytes = b"original evidence content"
        real_hash = compute_content_hash(evidence_bytes)

        mock_client = Mock()
        # stat_object returns metadata with the correct content hash.
        mock_client.stat_object.return_value = Mock(metadata={"X-Amz-Meta-Content-Hash": real_hash})
        # get_object returns bytes that do NOT match the hash.
        mock_response = Mock()
        mock_response.read.return_value = b"TAMPERED content -- different bytes"
        mock_client.get_object.return_value = mock_response

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=False,
        )

        payload = MinioEvidencePayload(
            memory_id=uuid4(),
            evidence_bytes=evidence_bytes,
            content_hash=real_hash,
            source_id="doc-1",
            source_type="document",
            timestamp="2026-01-01T00:00:00+00:00",
            chunk_id=None,
            confidence=1.0,
            metadata={},
        )

        with pytest.raises(
            MinioProjectionConflictError,
            match="altered or corrupted",
        ):
            await adapter.project_evidence(payload, idempotency_key="tamper-key")

        # The HTTP response must still be released.
        mock_response.close.assert_called_once()
        mock_response.release_conn.assert_called_once()
        # put_object must NOT be called.
        mock_client.put_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotent_metadata_and_bytes_both_match_succeeds(self):
        """Metadata hash matches AND bytes verify → idempotent success."""
        evidence_bytes = b"correct evidence content"
        real_hash = compute_content_hash(evidence_bytes)

        mock_client = Mock()
        mock_client.stat_object.return_value = Mock(metadata={"X-Amz-Meta-Content-Hash": real_hash})
        mock_response = Mock()
        mock_response.read.return_value = evidence_bytes
        mock_client.get_object.return_value = mock_response

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=False,
        )

        payload = MinioEvidencePayload(
            memory_id=uuid4(),
            evidence_bytes=evidence_bytes,
            content_hash=real_hash,
            source_id="doc-1",
            source_type="document",
            timestamp="2026-01-01T00:00:00+00:00",
            chunk_id=None,
            confidence=1.0,
            metadata={},
        )

        key = await adapter.project_evidence(payload, idempotency_key="idem-key")

        assert key == payload.object_key()
        mock_client.put_object.assert_not_called()
        mock_response.close.assert_called_once()
        mock_response.release_conn.assert_called_once()


class TestResourceOwnership:
    """Owned vs caller-owned client cleanup (urllib3 pool release)."""

    @pytest.mark.asyncio
    async def test_close_owned_client_clears_urllib3_pool(self):
        """close() on an owned adapter clears the urllib3 pool and drops
        the client reference."""
        mock_client = Mock()
        mock_http_pool = Mock()
        mock_client._http = mock_http_pool

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=True,
        )

        await adapter.close()

        mock_http_pool.clear.assert_called_once()
        assert adapter._client is None

    @pytest.mark.asyncio
    async def test_close_owned_client_tolerates_missing_http(self):
        """close() on an owned adapter does not raise if _http is absent."""
        mock_client = Mock(spec=[])  # no attributes

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=True,
        )

        # Should not raise even though _http is missing.
        await adapter.close()
        assert adapter._client is None

    @pytest.mark.asyncio
    async def test_close_owned_client_tolerates_pool_clear_failure(self):
        """close() on an owned adapter logs but does not raise if pool
        clear fails."""
        mock_client = Mock()
        mock_http_pool = Mock()
        mock_http_pool.clear.side_effect = RuntimeError("pool error")
        mock_client._http = mock_http_pool

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=True,
        )

        # Should not raise -- pool-clear failure is logged, not propagated.
        await adapter.close()
        assert adapter._client is None

    @pytest.mark.asyncio
    async def test_close_caller_owned_does_not_clear_pool(self):
        """close() on a caller-owned adapter must NOT touch the client."""
        mock_client = Mock()
        mock_http_pool = Mock()
        mock_client._http = mock_http_pool

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=False,
        )

        await adapter.close()

        mock_http_pool.clear.assert_not_called()
        # Caller-owned: the client reference is preserved.
        assert adapter._client is mock_client

    @pytest.mark.asyncio
    async def test_close_owned_idempotent_after_first_close(self):
        """close() on an owned adapter is idempotent after the first call."""
        mock_client = Mock()
        mock_http_pool = Mock()
        mock_client._http = mock_http_pool

        adapter = MinioProjectionAdapter(
            client=mock_client,
            bucket_name="test-bucket",
            owns_client=True,
        )

        await adapter.close()
        await adapter.close()

        # Pool cleared only once; second close is a no-op.
        mock_http_pool.clear.assert_called_once()
        assert adapter._client is None


class TestOwnedClientCleanupOnPreflightFailure:
    """Owned client must be closed and pool cleared when preflight fails.

    Covers asyncio.TimeoutError and asyncio.CancelledError during create().
    Caller-owned clients must remain untouched.
    """

    @pytest.mark.asyncio
    async def test_create_owned_timeout_closes_client_and_clears_pool(self, monkeypatch):
        """asyncio.TimeoutError during preflight must close the owned client."""
        mock_client = Mock()
        mock_http_pool = Mock()
        mock_client._http = mock_http_pool

        # Hang forever in preflight so wait_for triggers TimeoutError.
        async def _hang(self_adapter):
            await asyncio.Event().wait()

        import app.memory_core.repositories.minio_projection.adapter as adapter_mod

        monkeypatch.setattr(adapter_mod, "Minio", lambda *a, **kw: mock_client)
        monkeypatch.setattr(MinioProjectionAdapter, "_preflight_check", _hang)

        with pytest.raises(TimeoutError, match="preflight check timed out"):
            await MinioProjectionAdapter.create(
                endpoint="localhost:9000",
                access_key="key",
                secret_key="secret",
                preflight_timeout_s=0.05,
            )

        mock_http_pool.clear.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_owned_cancelled_closes_client_and_clears_pool(self, monkeypatch):
        """asyncio.CancelledError during preflight must close the owned client."""
        mock_client = Mock()
        mock_http_pool = Mock()
        mock_client._http = mock_http_pool

        async def _raise_cancelled(self_adapter):
            raise asyncio.CancelledError()

        import app.memory_core.repositories.minio_projection.adapter as adapter_mod

        monkeypatch.setattr(adapter_mod, "Minio", lambda *a, **kw: mock_client)
        monkeypatch.setattr(MinioProjectionAdapter, "_preflight_check", _raise_cancelled)

        with pytest.raises(asyncio.CancelledError):
            await MinioProjectionAdapter.create(
                endpoint="localhost:9000",
                access_key="key",
                secret_key="secret",
            )

        mock_http_pool.clear.assert_called_once()
