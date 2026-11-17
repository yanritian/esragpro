"""Unit tests for projection worker contracts (P1-04).

Tests immutable event envelopes, registry validation, and exceptions.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from uuid import uuid4

from app.memory_core.projection_worker.contracts import (
    ProjectionEvent,
    ClaimedEvent,
    ProjectionHandlerRegistry,
    PermanentProjectionError,
    LeaseLostError,
    WorkerResult,
)


class TestProjectionEvent:
    """Tests for ProjectionEvent dataclass."""

    def test_event_is_frozen(self) -> None:
        """Event should be immutable."""
        event = ProjectionEvent(
            outbox_id=uuid4(),
            aggregate_id=uuid4(),
            event_type="memory.remembered",
            schema_version=1,
            projection_target="redis",
            idempotency_key="test:key",
            payload={"test": "data"},
            attempt_count=1,
            max_attempts=8,
        )

        with pytest.raises(AttributeError):
            event.attempt_count = 2  # type: ignore

    def test_payload_is_defensive_copy(self) -> None:
        """Payload should be a copy, not the original."""
        original = {"nested": {"value": 1}}
        event = ProjectionEvent(
            outbox_id=uuid4(),
            aggregate_id=uuid4(),
            event_type="memory.remembered",
            schema_version=1,
            projection_target="redis",
            idempotency_key="test:key",
            payload=original,
            attempt_count=1,
            max_attempts=8,
        )

        # Modify original
        original["nested"]["value"] = 2

        # Event payload should not change
        assert event.payload["nested"]["value"] == 1


class TestClaimedEvent:
    """Tests for ClaimedEvent dataclass."""

    def test_claimed_event_is_frozen(self) -> None:
        """ClaimedEvent should be immutable."""
        event = ProjectionEvent(
            outbox_id=uuid4(),
            aggregate_id=uuid4(),
            event_type="memory.remembered",
            schema_version=1,
            projection_target="redis",
            idempotency_key="test:key",
            payload={},
            attempt_count=1,
            max_attempts=8,
        )

        claimed = ClaimedEvent(
            event=event,
            fencing_token="worker:123",
            lease_expires_at=datetime.now(timezone.utc),
        )

        with pytest.raises(AttributeError):
            claimed.fencing_token = "other"  # type: ignore


class TestProjectionHandlerRegistry:
    """Tests for ProjectionHandlerRegistry."""

    def test_registry_validates_handlers(self) -> None:
        """Registry should accept valid handlers."""
        async def handler(event: ProjectionEvent) -> None:
            pass

        registry = ProjectionHandlerRegistry({"redis": handler})
        assert registry.has_target("redis")
        assert registry.get_handler("redis") is handler

    def test_registry_rejects_empty_target(self) -> None:
        """Registry should reject empty target names."""
        async def handler(event: ProjectionEvent) -> None:
            pass

        with pytest.raises(ValueError, match="non-empty"):
            ProjectionHandlerRegistry({"": handler})

    def test_registry_rejects_non_dict_mapping_with_duplicates(self) -> None:
        """Registry should reject duplicate targets from non-dict mappings."""
        async def handler(event: ProjectionEvent) -> None:
            pass

        # Create a mapping with duplicates (list of tuples)
        # Note: Python dicts can't have duplicate keys, but other mappings can
        from typing import Mapping
        class DuplicateMapping(Mapping):
            def __init__(self):
                self._items = [("redis", handler), ("redis", handler)]
            def __getitem__(self, key):
                for k, v in self._items:
                    if k == key:
                        return v
                raise KeyError(key)
            def __iter__(self):
                return iter([k for k, _ in self._items])
            def __len__(self):
                return len(self._items)

        with pytest.raises(ValueError, match="duplicate"):
            ProjectionHandlerRegistry(DuplicateMapping())

    def test_registry_rejects_non_callable(self) -> None:
        """Registry should reject non-callable handlers."""
        with pytest.raises(TypeError, match="callable"):
            ProjectionHandlerRegistry({"redis": "not a handler"})  # type: ignore

    def test_registry_targets_is_immutable(self) -> None:
        """Registry targets should be immutable."""
        async def handler(event: ProjectionEvent) -> None:
            pass

        registry = ProjectionHandlerRegistry({"redis": handler})
        targets = registry.targets

        assert isinstance(targets, tuple)
        assert targets == ("redis",)

    def test_registry_rejects_uppercase_target(self) -> None:
        """Registry should reject uppercase targets per P1-03 grammar."""
        async def handler(event: ProjectionEvent) -> None:
            pass

        with pytest.raises(ValueError, match="invalid projection target"):
            ProjectionHandlerRegistry({"Redis": handler})

    def test_registry_rejects_unsafe_characters(self) -> None:
        """Registry should reject targets with unsafe characters."""
        async def handler(event: ProjectionEvent) -> None:
            pass

        with pytest.raises(ValueError, match="invalid projection target"):
            ProjectionHandlerRegistry({"my.target!": handler})

    def test_registry_rejects_51_char_target(self) -> None:
        """Registry should reject targets longer than 50 characters."""
        async def handler(event: ProjectionEvent) -> None:
            pass

        target = "a" * 51
        with pytest.raises(ValueError, match="invalid projection target"):
            ProjectionHandlerRegistry({target: handler})

    def test_caller_dict_mutation_isolation(self) -> None:
        """Mutating the caller's dict after construction must not affect registry."""
        async def handler(event: ProjectionEvent) -> None:
            pass

        async def other_handler(event: ProjectionEvent) -> None:
            pass

        original = {"redis": handler}
        registry = ProjectionHandlerRegistry(original)

        # Mutate caller's original dict
        original["redis"] = other_handler
        original["neo4j"] = other_handler

        # Registry must be unaffected
        assert registry.get_handler("redis") is handler
        assert not registry.has_target("neo4j")
        assert registry.targets == ("redis",)

    def test_registry_internal_mapping_assignment_raises_typeerror(self) -> None:
        """Assigning through _handlers must raise TypeError."""
        async def handler(event: ProjectionEvent) -> None:
            pass

        registry = ProjectionHandlerRegistry({"redis": handler})
        with pytest.raises(TypeError):
            registry._handlers["redis"] = handler  # type: ignore[index]

    def test_registry_internal_mapping_deletion_raises_typeerror(self) -> None:
        """Deleting through _handlers must raise TypeError."""
        async def handler(event: ProjectionEvent) -> None:
            pass

        registry = ProjectionHandlerRegistry({"redis": handler})
        with pytest.raises(TypeError):
            del registry._handlers["redis"]  # type: ignore[index]


class TestPermanentProjectionError:
    """Tests for PermanentProjectionError."""

    def test_error_carries_target(self) -> None:
        """Error should carry target information."""
        error = PermanentProjectionError("Test error", target="redis")
        assert error.target == "redis"
        assert "Test error" in str(error)


class TestLeaseLostError:
    """Tests for LeaseLostError."""

    def test_error_carries_outbox_id(self) -> None:
        """Error should carry outbox ID."""
        outbox_id = uuid4()
        error = LeaseLostError(outbox_id)
        assert error.outbox_id == outbox_id
        assert str(outbox_id) in str(error)


class TestWorkerResult:
    """Tests for WorkerResult."""

    def test_result_defaults_zero(self) -> None:
        """Result should default all counts to zero."""
        result = WorkerResult()
        assert result.claimed == 0
        assert result.completed == 0
        assert result.retried == 0
        assert result.dead_lettered == 0
        assert result.lease_lost == 0
        assert result.exhausted_recovered == 0

    def test_total_processed_excludes_lease_lost(self) -> None:
        """total_processed should exclude lease_lost."""
        result = WorkerResult(
            claimed=10,
            completed=5,
            retried=2,
            dead_lettered=1,
            lease_lost=2,
        )
        # 5 + 2 + 1 = 8, not 10 (which would include lease_lost)
        assert result.total_processed == 8