"""Unit tests for P1-07 Neo4j projection handler.

Covers:
- Handler __call__ delegates to adapter correctly
- IdempotencyConflictError → PermanentProjectionError
- Payload validation errors → PermanentProjectionError
- Retryable errors propagate as-is
- Worker integration (mock-based)
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.memory_core.neo4j_handler import (
    Neo4jProjectionHandler,
    _is_retryable_neo4j_error,
)
from app.memory_core.projection_worker.contracts import (
    PermanentProjectionError,
    ProjectionEvent,
    ProjectionHandlerRegistry,
)
from app.memory_core.repositories.neo4j_projection.adapter import (
    IdempotencyConflictError,
    Neo4jProjectionAdapter,
)
from app.memory_core.repositories.neo4j_projection.models import (
    PermanentNeo4jPayloadError,
)
from app.memory_core.repositories.postgres_outbox import (
    build_event_payload,
    build_idempotency_key,
)
from app.memory_core.unpermissioned_contracts import (
    EvidenceRef,
    MemoryItem,
    MemoryItemStatus,
    ProvenanceInfo,
)


def _make_memory_item(
    *,
    content: str = "test content",
    status: MemoryItemStatus = MemoryItemStatus.ACTIVE,
    evidence_refs: list | None = None,
    provenance: ProvenanceInfo | None = None,
) -> MemoryItem:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return MemoryItem(
        id=uuid4(),
        content=content,
        status=status,
        created_at=now,
        updated_at=now,
        expires_at=None,
        project_id=uuid4(),
        session_id=None,
        agent_id=None,
        knowledge_base_id=None,
        provenance=provenance,
        metadata={},
        evidence_refs=evidence_refs or [],
    )


def _make_event(
    memory_item: MemoryItem,
    *,
    idempotency_key: str | None = None,
) -> ProjectionEvent:
    payload = build_event_payload(
        memory_item=memory_item,
        projection_target="neo4j",
    )
    agg_id = memory_item.id
    idem_key = idempotency_key or build_idempotency_key(agg_id, "neo4j")
    return ProjectionEvent(
        outbox_id=uuid4(),
        aggregate_id=agg_id,
        event_type="memory.remembered",
        schema_version=1,
        projection_target="neo4j",
        idempotency_key=idem_key,
        payload=payload,
        attempt_count=1,
        max_attempts=5,
    )


class TestHandlerDelegation:
    """Test handler delegates to adapter correctly."""

    @pytest.mark.asyncio
    async def test_handler_calls_adapter_project(self):
        mock_adapter = AsyncMock(spec=Neo4jProjectionAdapter)
        handler = Neo4jProjectionHandler(mock_adapter)

        item = _make_memory_item(content="handler test")
        event = _make_event(item)

        await handler(event)

        mock_adapter.project_memory_item.assert_called_once()
        call_kwargs = mock_adapter.project_memory_item.call_args[1]
        assert call_kwargs["content"] == "handler test"
        assert call_kwargs["idempotency_key"] == event.idempotency_key

    @pytest.mark.asyncio
    async def test_handler_passes_evidence_with_position(self):
        mock_adapter = AsyncMock(spec=Neo4jProjectionAdapter)
        handler = Neo4jProjectionAdapter.__new__(Neo4jProjectionAdapter)
        handler = Neo4jProjectionHandler(mock_adapter)

        from datetime import datetime, timezone

        evidence = [
            EvidenceRef(
                source_id="doc-1",
                source_type="document",
                timestamp=datetime.now(timezone.utc),
                confidence=0.9,
            ),
        ]
        item = _make_memory_item(evidence_refs=evidence)
        event = _make_event(item)

        await handler(event)

        call_kwargs = mock_adapter.project_memory_item.call_args[1]
        evidence_refs = call_kwargs["evidence_refs"]
        assert len(evidence_refs) == 1
        assert evidence_refs[0]["position"] == 0
        assert evidence_refs[0]["source_id"] == "doc-1"


class TestHandlerErrorClassification:
    """Test error classification in the handler."""

    @pytest.mark.asyncio
    async def test_invalid_payload_classified_as_permanent(self):
        """Payload validation errors raised BEFORE the adapter call must
        still become PermanentProjectionError (dead-letter, no retry).

        Pins the P1-07 retry-9 fix: validate_payload_cross_check now
        runs inside the handler's protected classification block.
        """
        mock_adapter = AsyncMock(spec=Neo4jProjectionAdapter)
        handler = Neo4jProjectionHandler(mock_adapter)

        # Build an event whose payload is structurally invalid (missing
        # required 'event_type' field).  The idempotency_key is derived
        # deterministically from aggregate_id so the retry-14 key guard
        # passes and this test exercises the payload-validation branch.
        agg_id = uuid4()
        bad_payload = {
            "memory_snapshot": {"id": "x", "content": "c", "status": "active"},
        }
        event = ProjectionEvent(
            outbox_id=uuid4(),
            aggregate_id=agg_id,
            event_type="memory.remembered",
            schema_version=1,
            projection_target="neo4j",
            idempotency_key=build_idempotency_key(agg_id, "neo4j"),
            payload=bad_payload,
            attempt_count=1,
            max_attempts=5,
        )

        with pytest.raises(PermanentProjectionError, match="payload validation"):
            await handler(event)

        # The adapter must NOT have been called -- validation failed first.
        mock_adapter.project_memory_item.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotency_conflict_raises_permanent(self):
        mock_adapter = AsyncMock(spec=Neo4jProjectionAdapter)
        mock_adapter.project_memory_item.side_effect = IdempotencyConflictError(
            "test-id", "stored-key", "envelope-key"
        )
        handler = Neo4jProjectionHandler(mock_adapter)

        item = _make_memory_item()
        event = _make_event(item)

        with pytest.raises(PermanentProjectionError, match="idempotency_key conflict"):
            await handler(event)

    @pytest.mark.asyncio
    async def test_payload_error_raises_permanent(self):
        mock_adapter = AsyncMock(spec=Neo4jProjectionAdapter)
        mock_adapter.project_memory_item.side_effect = PermanentNeo4jPayloadError("bad payload")
        handler = Neo4jProjectionHandler(mock_adapter)

        item = _make_memory_item()
        event = _make_event(item)

        with pytest.raises(PermanentProjectionError, match="payload validation"):
            await handler(event)

    @pytest.mark.asyncio
    async def test_permanent_projection_error_propagated(self):
        mock_adapter = AsyncMock(spec=Neo4jProjectionAdapter)
        mock_adapter.project_memory_item.side_effect = PermanentProjectionError("already permanent")
        handler = Neo4jProjectionHandler(mock_adapter)

        item = _make_memory_item()
        event = _make_event(item)

        with pytest.raises(PermanentProjectionError, match="already permanent"):
            await handler(event)

    @pytest.mark.asyncio
    async def test_connection_error_propagated_as_retryable(self):
        mock_adapter = AsyncMock(spec=Neo4jProjectionAdapter)
        mock_adapter.project_memory_item.side_effect = ConnectionError("connection lost")
        handler = Neo4jProjectionHandler(mock_adapter)

        item = _make_memory_item()
        event = _make_event(item)

        with pytest.raises(ConnectionError, match="connection lost"):
            await handler(event)

    @pytest.mark.asyncio
    async def test_timeout_error_propagated_as_retryable(self):
        mock_adapter = AsyncMock(spec=Neo4jProjectionAdapter)
        mock_adapter.project_memory_item.side_effect = TimeoutError("timed out")
        handler = Neo4jProjectionHandler(mock_adapter)

        item = _make_memory_item()
        event = _make_event(item)

        with pytest.raises(TimeoutError, match="timed out"):
            await handler(event)

    @pytest.mark.asyncio
    async def test_unknown_error_raises_permanent(self):
        mock_adapter = AsyncMock(spec=Neo4jProjectionAdapter)
        mock_adapter.project_memory_item.side_effect = RuntimeError("unknown error")
        handler = Neo4jProjectionHandler(mock_adapter)

        item = _make_memory_item()
        event = _make_event(item)

        with pytest.raises(PermanentProjectionError, match="RuntimeError"):
            await handler(event)


class TestRetryableErrorClassification:
    """Test the retryable error classifier."""

    def test_connection_error_is_retryable(self):
        assert _is_retryable_neo4j_error(ConnectionError("test"))

    def test_timeout_error_is_retryable(self):
        assert _is_retryable_neo4j_error(TimeoutError("test"))

    def test_os_error_is_retryable(self):
        assert _is_retryable_neo4j_error(OSError("test"))

    def test_value_error_is_not_retryable(self):
        assert not _is_retryable_neo4j_error(ValueError("test"))

    def test_runtime_error_is_not_retryable(self):
        assert not _is_retryable_neo4j_error(RuntimeError("test"))


class TestHandlerRegistry:
    """Test handler registration."""

    def test_registry_accepts_neo4j_handler(self):
        mock_adapter = AsyncMock(spec=Neo4jProjectionAdapter)
        handler = Neo4jProjectionHandler(mock_adapter)
        registry = ProjectionHandlerRegistry({"neo4j": handler})
        assert registry.has_target("neo4j")
        assert registry.get_handler("neo4j") is handler

    def test_registry_rejects_duplicate_neo4j(self):
        from collections.abc import Mapping

        mock_adapter = AsyncMock(spec=Neo4jProjectionAdapter)
        handler = Neo4jProjectionHandler(mock_adapter)

        class _DuplicateMapping(Mapping):
            """Mapping that yields duplicate keys to test registry validation."""

            def __init__(self, items):
                self._items = items

            def __getitem__(self, key):
                for k, v in self._items:
                    if k == key:
                        return v
                raise KeyError(key)

            def __iter__(self):
                return iter(k for k, v in self._items)

            def __len__(self):
                return len(self._items)

        with pytest.raises(ValueError, match="duplicate"):
            ProjectionHandlerRegistry(_DuplicateMapping([("neo4j", handler), ("neo4j", handler)]))


class TestHandlerConstructor:
    """Test handler construction validation."""

    def test_none_adapter_raises(self):
        with pytest.raises(TypeError, match="adapter must be"):
            Neo4jProjectionHandler(None)

    def test_wrong_type_adapter_raises(self):
        with pytest.raises(TypeError, match="adapter must be"):
            Neo4jProjectionHandler("not-an-adapter")  # type: ignore[arg-type]


class TestDeterministicIdempotencyKeyContract:
    """The handler must enforce the deterministic outbox idempotency-key
    contract at the projection boundary.

    Pins the P1-07 retry-14 requirement: an envelope with an arbitrary
    or mismatched idempotency_key must dead-letter immediately rather
    than risk writing under a non-standard immutable graph key.
    """

    def test_deterministic_key_matches_outbox_derivation(self):
        """The outbox ``build_idempotency_key`` must produce the
        expected format for a known aggregate/target pair."""
        agg = uuid4()
        key = build_idempotency_key(agg, "neo4j")
        assert key == f"memory:{agg}:remembered:v1:neo4j"

    @pytest.mark.asyncio
    async def test_handler_rejects_non_deterministic_key(self):
        """An envelope whose idempotency_key does not match the
        deterministic derivation must raise PermanentProjectionError
        before any adapter call."""
        mock_adapter = AsyncMock(spec=Neo4jProjectionAdapter)
        handler = Neo4jProjectionHandler(mock_adapter)

        item = _make_memory_item()
        # Build an event with a random/arbitrary key that does NOT
        # match the outbox derivation.
        event = _make_event(item, idempotency_key="arbitrary-key-12345")

        with pytest.raises(PermanentProjectionError, match="deterministic contract"):
            await handler(event)

        # The adapter must NOT have been called -- validation fails first.
        mock_adapter.project_memory_item.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_accepts_deterministic_key(self):
        """An envelope whose idempotency_key matches the deterministic
        derivation must pass validation and reach the adapter."""
        mock_adapter = AsyncMock(spec=Neo4jProjectionAdapter)
        handler = Neo4jProjectionHandler(mock_adapter)

        item = _make_memory_item()
        # Use the outbox-derived deterministic key.
        event = _make_event(item)
        assert event.idempotency_key == build_idempotency_key(item.id, "neo4j")

        await handler(event)
        mock_adapter.project_memory_item.assert_called_once()
