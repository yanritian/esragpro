"""Unit tests for naive/aware datetime compatibility in memory contracts.

Verifies that ``MemoryItem.is_expired()`` safely compares both legacy
naive UTC and PostgreSQL-aware timestamps.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.memory_core.unpermissioned_contracts import MemoryItem


class TestIsExpiredCompatibility:
    """Verify MemoryItem.is_expired handles both naive and aware datetimes."""

    def test_naive_expires_at_past(self) -> None:
        past = datetime(2020, 1, 1)
        item = MemoryItem(id=uuid4(), content="test", expires_at=past)
        assert item.is_expired() is True

    def test_aware_expires_at_past(self) -> None:
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        item = MemoryItem(id=uuid4(), content="test", expires_at=past)
        assert item.is_expired() is True

    def test_aware_expires_at_future(self) -> None:
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        item = MemoryItem(id=uuid4(), content="test", expires_at=future)
        assert item.is_expired() is False

    def test_naive_expires_at_future(self) -> None:
        future = datetime(2099, 1, 1)
        item = MemoryItem(id=uuid4(), content="test", expires_at=future)
        assert item.is_expired() is False

    def test_no_expires_at(self) -> None:
        item = MemoryItem(id=uuid4(), content="test")
        assert item.is_expired() is False
