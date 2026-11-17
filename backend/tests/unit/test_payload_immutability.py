"""Tests for payload immutability in ProjectionEvent (P1-04).

Verifies that handlers cannot mutate event payload and that
each access returns a fresh deep copy.
"""

from __future__ import annotations

from app.memory_core.projection_worker.contracts import ProjectionEvent
from uuid import uuid4


class TestPayloadImmutability:
    """Tests for payload defensive copying."""

    def test_payload_access_returns_fresh_copy(self) -> None:
        """Each payload access should return an independent copy."""
        original_payload = {"nested": {"value": 1}, "list": [1, 2, 3]}
        event = ProjectionEvent(
            outbox_id=uuid4(),
            aggregate_id=uuid4(),
            event_type="memory.remembered",
            schema_version=1,
            projection_target="redis",
            idempotency_key="test:123",
            payload=original_payload,
            attempt_count=1,
            max_attempts=8,
        )

        # Get payload twice
        payload1 = event.payload
        payload2 = event.payload

        # Mutate first copy
        payload1["nested"]["value"] = 999
        payload1["list"].append(4)

        # Second copy should be unaffected
        assert payload2["nested"]["value"] == 1
        assert payload2["list"] == [1, 2, 3]

    def test_handler_cannot_mutate_stored_payload(self) -> None:
        """Handler mutation of event.payload should not affect stored snapshot."""
        original_payload = {"nested": {"value": 1}, "list": [1, 2, 3]}
        event = ProjectionEvent(
            outbox_id=uuid4(),
            aggregate_id=uuid4(),
            event_type="memory.remembered",
            schema_version=1,
            projection_target="redis",
            idempotency_key="test:456",
            payload=original_payload,
            attempt_count=1,
            max_attempts=8,
        )

        # Simulate handler mutating the payload
        payload = event.payload
        payload["nested"]["value"] = 999
        payload["list"].append(4)

        # Later read should see original values
        later_payload = event.payload
        assert later_payload["nested"]["value"] == 1
        assert later_payload["list"] == [1, 2, 3]

    def test_original_input_cannot_affect_event(self) -> None:
        """Mutating the original input dict after construction should not affect event."""
        original_payload = {"nested": {"value": 1}, "list": [1, 2, 3]}
        event = ProjectionEvent(
            outbox_id=uuid4(),
            aggregate_id=uuid4(),
            event_type="memory.remembered",
            schema_version=1,
            projection_target="redis",
            idempotency_key="test:789",
            payload=original_payload,
            attempt_count=1,
            max_attempts=8,
        )

        # Mutate original after construction
        original_payload["nested"]["value"] = 999
        original_payload["list"].append(4)

        # Event payload should be unaffected
        event_payload = event.payload
        assert event_payload["nested"]["value"] == 1
        assert event_payload["list"] == [1, 2, 3]

    def test_get_payload_copy_returns_independent_copy(self) -> None:
        """get_payload_copy() should return independent copies."""
        original_payload = {"nested": {"value": 1}}
        event = ProjectionEvent(
            outbox_id=uuid4(),
            aggregate_id=uuid4(),
            event_type="memory.remembered",
            schema_version=1,
            projection_target="redis",
            idempotency_key="test:copy",
            payload=original_payload,
            attempt_count=1,
            max_attempts=8,
        )

        copy1 = event.get_payload_copy()
        copy2 = event.get_payload_copy()

        # Mutate first copy
        copy1["nested"]["value"] = 999

        # Second copy should be unaffected
        assert copy2["nested"]["value"] == 1

    def test_json_dumps_works_on_payload(self) -> None:
        """json.dumps(event.payload) should work correctly."""
        import json

        original_payload = {"nested": {"value": 1}, "list": [1, 2, 3]}
        event = ProjectionEvent(
            outbox_id=uuid4(),
            aggregate_id=uuid4(),
            event_type="memory.remembered",
            schema_version=1,
            projection_target="redis",
            idempotency_key="test:json",
            payload=original_payload,
            attempt_count=1,
            max_attempts=8,
        )

        # Should be JSON serializable
        json_str = json.dumps(event.payload)
        parsed = json.loads(json_str)

        assert parsed["nested"]["value"] == 1
        assert parsed["list"] == [1, 2, 3]

    def test_deeply_nested_mutation_isolated(self) -> None:
        """Mutations to deeply nested structures should be isolated."""
        original_payload = {
            "level1": {
                "level2": {
                    "level3": {
                        "value": 1
                    }
                }
            }
        }
        event = ProjectionEvent(
            outbox_id=uuid4(),
            aggregate_id=uuid4(),
            event_type="memory.remembered",
            schema_version=1,
            projection_target="redis",
            idempotency_key="test:nested",
            payload=original_payload,
            attempt_count=1,
            max_attempts=8,
        )

        # Mutate deeply nested value
        payload = event.payload
        payload["level1"]["level2"]["level3"]["value"] = 999

        # Fresh access should see original
        fresh = event.payload
        assert fresh["level1"]["level2"]["level3"]["value"] == 1
