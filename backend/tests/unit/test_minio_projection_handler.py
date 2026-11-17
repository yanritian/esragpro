"""Unit tests for MinIO projection handler (P1-08).

Covers:
- Handler creation and preflight
- Event processing (success, idempotent, conflict)
- Error handling (permanent vs retryable)
- Idempotency key validation
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from app.memory_core.minio_handler import (
    MinioProjectionHandler,
    _is_retryable_minio_error,
    _validate_deterministic_idempotency_key,
)
from app.memory_core.projection_worker.contracts import (
    PermanentProjectionError,
    ProjectionEvent,
)
from app.memory_core.repositories.minio_projection.adapter import (
    MinioProjectionAdapter,
    MinioProjectionConflictError,
)


def _make_event(
    *,
    memory_id: str,
    idempotency_key: str,
) -> ProjectionEvent:
    """Build a test projection event."""
    return ProjectionEvent(
        outbox_id=uuid4(),
        aggregate_id=UUID(memory_id),
        event_type="memory.remembered",
        schema_version=1,
        projection_target="minio",
        idempotency_key=idempotency_key,
        payload={
            "event_type": "memory.remembered",
            "schema_version": 1,
            "aggregate_type": "memory_item",
            "aggregate_id": memory_id,
            "projection_target": "minio",
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "memory_snapshot": {
                "id": memory_id,
                "content": "test content",
                "status": "active",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "expires_at": None,
                "project_id": None,
                "session_id": None,
                "agent_id": None,
                "knowledge_base_id": None,
                "provenance": None,
                "metadata": {},
                "evidence_refs": [
                    {
                        "source_id": "doc-123",
                        "source_type": "document",
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "chunk_id": "chunk-0",
                        "confidence": 0.95,
                        "metadata": {"page": 1},
                    }
                ],
            },
        },
        attempt_count=0,
        max_attempts=8,
    )


def _make_adapter_mock() -> MinioProjectionAdapter:
    """Build a mock MinioProjectionAdapter."""
    mock = Mock(spec=MinioProjectionAdapter)
    mock.project_evidence = AsyncMock(return_value="memory/abc/evidence/def")
    mock.preflight_ping = AsyncMock(return_value={"status": "ok"})
    mock.close = AsyncMock()
    return mock


class TestValidateDeterministicIdempotencyKey:
    """Test idempotency key validation."""

    def test_valid_key(self):
        """Valid deterministic key passes."""
        memory_id = uuid4()
        event = _make_event(
            memory_id=str(memory_id),
            idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
        )
        # Should not raise
        _validate_deterministic_idempotency_key(event)

    def test_invalid_key_raises(self):
        """Non-matching key raises PermanentProjectionError."""
        memory_id = uuid4()
        wrong_key = f"memory:{uuid4()}:remembered:v1:minio"
        event = _make_event(
            memory_id=str(memory_id),
            idempotency_key=wrong_key,
        )
        with pytest.raises(PermanentProjectionError, match="does not match"):
            _validate_deterministic_idempotency_key(event)


class TestMinioProjectionHandler:
    """Test MinioProjectionHandler."""

    def test_init_validates_adapter(self):
        """__init__ validates adapter type."""
        with pytest.raises(TypeError, match="adapter must be"):
            MinioProjectionHandler(adapter=Mock())

    @pytest.mark.asyncio
    async def test_call_success(self):
        """__call__ processes event successfully."""
        adapter = _make_adapter_mock()
        # Configure a concrete return value so we can verify the handler
        # consumes the adapter's result, not just that it was called.
        adapter.project_evidence = AsyncMock(return_value="memory/abc/evidence/def")
        handler = MinioProjectionHandler(adapter=adapter)

        memory_id = uuid4()
        event = _make_event(
            memory_id=str(memory_id),
            idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
        )

        await handler(event)

        adapter.project_evidence.assert_called_once()
        # Verify the handler passed a validated payload (not a raw dict)
        # and the deterministic idempotency key through to the adapter.
        call_args = adapter.project_evidence.call_args
        passed_payload = call_args.args[0]
        assert passed_payload.memory_id == memory_id
        assert call_args.kwargs["idempotency_key"] == event.idempotency_key

    @pytest.mark.asyncio
    async def test_call_idempotent(self):
        """__call__ handles idempotent replay."""
        adapter = _make_adapter_mock()
        handler = MinioProjectionHandler(adapter=adapter)

        memory_id = uuid4()
        event = _make_event(
            memory_id=str(memory_id),
            idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
        )

        # Call twice
        await handler(event)
        await handler(event)

        # Both calls should succeed
        assert adapter.project_evidence.call_count == 2

    @pytest.mark.asyncio
    async def test_call_conflict_raises_permanent(self):
        """__call__ converts conflict to PermanentProjectionError."""
        adapter = _make_adapter_mock()
        adapter.project_evidence.side_effect = MinioProjectionConflictError("content hash conflict")

        handler = MinioProjectionHandler(adapter=adapter)

        memory_id = uuid4()
        event = _make_event(
            memory_id=str(memory_id),
            idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
        )

        with pytest.raises(PermanentProjectionError, match="content hash conflict"):
            await handler(event)

    @pytest.mark.asyncio
    async def test_call_invalid_idempotency_key(self):
        """__call__ rejects invalid idempotency key."""
        adapter = _make_adapter_mock()
        handler = MinioProjectionHandler(adapter=adapter)

        memory_id = uuid4()
        event = _make_event(
            memory_id=str(memory_id),
            idempotency_key=f"memory:{uuid4()}:remembered:v1:minio",  # Wrong ID
        )

        with pytest.raises(PermanentProjectionError, match="does not match"):
            await handler(event)

    @pytest.mark.asyncio
    async def test_call_retryable_error(self):
        """__call__ re-raises retryable errors."""
        adapter = _make_adapter_mock()
        adapter.project_evidence.side_effect = ConnectionError("connection failed")

        handler = MinioProjectionHandler(adapter=adapter)

        memory_id = uuid4()
        event = _make_event(
            memory_id=str(memory_id),
            idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
        )

        with pytest.raises(ConnectionError):
            await handler(event)

    @pytest.mark.asyncio
    async def test_call_unknown_error_permanent(self):
        """__call__ treats unknown errors as permanent."""
        adapter = _make_adapter_mock()
        adapter.project_evidence.side_effect = ValueError("unknown error")

        handler = MinioProjectionHandler(adapter=adapter)

        memory_id = uuid4()
        event = _make_event(
            memory_id=str(memory_id),
            idempotency_key=f"memory:{memory_id}:remembered:v1:minio",
        )

        with pytest.raises(PermanentProjectionError, match="project_evidence failed"):
            await handler(event)

    @pytest.mark.asyncio
    async def test_close_idempotent(self):
        """close() is idempotent."""
        adapter = _make_adapter_mock()
        handler = MinioProjectionHandler(adapter=adapter)

        await handler.close()
        await handler.close()

        assert adapter.close.call_count == 2

    @pytest.mark.asyncio
    async def test_preflight_ping(self):
        """preflight_ping() delegates to adapter."""
        adapter = _make_adapter_mock()
        handler = MinioProjectionHandler(adapter=adapter)

        result = await handler.preflight_ping()

        assert result == {"status": "ok"}
        adapter.preflight_ping.assert_called_once()


class TestIsRetryableMinioError:
    """Test error classification in handler."""

    def test_connection_error_retryable(self):
        """ConnectionError is retryable."""
        assert _is_retryable_minio_error(ConnectionError()) is True

    def test_timeout_error_retryable(self):
        """TimeoutError is retryable."""
        assert _is_retryable_minio_error(TimeoutError()) is True

    def test_os_error_retryable(self):
        """OSError is retryable."""
        assert _is_retryable_minio_error(OSError()) is True

    def test_value_error_permanent(self):
        """ValueError is permanent."""
        assert _is_retryable_minio_error(ValueError()) is False

    def test_urllib3_max_retry_error_retryable(self):
        """urllib3 MaxRetryError (real transport exhaustion) is retryable.

        This simulates the actual exception the MinIO SDK raises when
        all connection retries to the MinIO server are exhausted.
        """
        import urllib3.exceptions

        exc = urllib3.exceptions.MaxRetryError(
            pool=object(),
            url="http://localhost:9000/minio/health/live",
        )
        assert _is_retryable_minio_error(exc) is True

    def test_urllib3_protocol_error_retryable(self):
        """urllib3 ProtocolError (connection reset) is retryable."""
        import urllib3.exceptions

        exc = urllib3.exceptions.ProtocolError("Connection aborted.")
        assert _is_retryable_minio_error(exc) is True

    def test_urllib3_http_error_retryable(self):
        """urllib3 HTTPError (base class) is retryable."""
        import urllib3.exceptions

        exc = urllib3.exceptions.HTTPError("generic HTTP error")
        assert _is_retryable_minio_error(exc) is True
