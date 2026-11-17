"""Shared test helpers for worker behavior tests (P1-04)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from app.memory_core.projection_worker.contracts import (
    ClaimedEvent,
    ProjectionEvent,
)


def make_event(target: str = "redis") -> ProjectionEvent:
    """Create a test event."""
    return ProjectionEvent(
        outbox_id=uuid4(),
        aggregate_id=uuid4(),
        event_type="memory.remembered",
        schema_version=1,
        projection_target=target,
        idempotency_key=f"test:{uuid4()}",
        payload={"test": "data"},
        attempt_count=1,
        max_attempts=8,
    )


def make_claimed_event(target: str = "redis") -> ClaimedEvent:
    """Create a test claimed event."""
    return ClaimedEvent(
        event=make_event(target),
        fencing_token=f"worker:{uuid4()}",
        lease_expires_at=asyncio.get_event_loop().time() + 60,
    )


__all__ = ["make_event", "make_claimed_event"]
