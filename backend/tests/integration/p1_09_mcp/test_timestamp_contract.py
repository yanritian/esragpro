"""Regression test for P1-09 timestamp wire contract.

Ensures all datetime fields crossing the FastAPI → Rust MCP boundary
carry explicit UTC offsets (Z or +00:00) for chrono::DateTime<Utc> parsing.
"""

from datetime import datetime, timezone
from uuid import uuid4

from app.api.memory_plane import (
    EvidenceRefModel,
    MemoryItemResponse,
    ContextResponse,
)


def test_evidence_ref_timestamp_has_utc_offset():
    """EvidenceRefModel.timestamp must serialize with explicit UTC offset."""
    # Simulate a naive UTC datetime from PostgreSQL (as returned by domain layer)
    naive_utc = datetime(2026, 7, 30, 12, 0, 0, 123456)

    model = EvidenceRefModel(
        source_id="doc-123",
        source_type="document",
        timestamp=naive_utc,
    )

    # Serialize to JSON-compatible dict
    data = model.model_dump(mode="json")

    # The timestamp must have an explicit UTC offset
    timestamp_str = data["timestamp"]
    assert isinstance(timestamp_str, str), f"Expected string, got {type(timestamp_str)}"

    # Must end with Z or +00:00 or contain timezone info
    assert (
        timestamp_str.endswith("Z")
        or "+00:00" in timestamp_str
        or "Z" in timestamp_str
    ), f"Timestamp missing UTC offset: {timestamp_str}"

    # Verify it can be parsed as ISO 8601 with timezone
    parsed = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, f"Parsed datetime has no tzinfo: {parsed}"


def test_memory_item_response_timestamps_have_utc_offsets():
    """MemoryItemResponse timestamps must serialize with explicit UTC offsets."""
    # Simulate naive UTC datetimes from PostgreSQL (as returned by domain layer)
    naive_created = datetime(2026, 7, 30, 10, 0, 0)
    naive_updated = datetime(2026, 7, 30, 11, 0, 0)
    naive_expires = datetime(2026, 8, 30, 12, 0, 0)

    model = MemoryItemResponse(
        id=uuid4(),
        content="Test memory item",
        evidence_refs=[],
        provenance=None,
        status="active",
        created_at=naive_created,
        updated_at=naive_updated,
        expires_at=naive_expires,
        metadata={},
        project_id=None,
        session_id=None,
        agent_id=None,
        knowledge_base_id=None,
    )

    # Serialize to JSON-compatible dict
    data = model.model_dump(mode="json")

    # Check all timestamp fields
    for field_name in ["created_at", "updated_at", "expires_at"]:
        timestamp_str = data[field_name]
        assert isinstance(
            timestamp_str, str
        ), f"{field_name}: Expected string, got {type(timestamp_str)}"

        # Must have explicit UTC offset
        assert (
            timestamp_str.endswith("Z")
            or "+00:00" in timestamp_str
            or "Z" in timestamp_str
        ), f"{field_name} missing UTC offset: {timestamp_str}"

        # Verify it can be parsed as ISO 8601 with timezone
        parsed = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        assert (
            parsed.tzinfo is not None
        ), f"{field_name}: Parsed datetime has no tzinfo: {parsed}"


def test_memory_item_response_with_aware_timestamps_converts_to_utc():
    """MemoryItemResponse must convert timezone-aware datetimes to UTC."""
    # Simulate a timezone-aware datetime in a non-UTC timezone
    from datetime import timedelta

    # UTC+5:30 (India Standard Time)
    ist = timezone(timedelta(hours=5, minutes=30))
    aware_ist = datetime(2026, 7, 30, 17, 30, 0, tzinfo=ist)  # 12:00 UTC

    model = MemoryItemResponse(
        id=uuid4(),
        content="Test memory item",
        evidence_refs=[],
        provenance=None,
        status="active",
        created_at=aware_ist,
        updated_at=aware_ist,
        expires_at=None,
        metadata={},
        project_id=None,
        session_id=None,
        agent_id=None,
        knowledge_base_id=None,
    )

    # Serialize to JSON-compatible dict
    data = model.model_dump(mode="json")

    # Should be converted to UTC (12:00 UTC, not 17:30 IST)
    created_at_str = data["created_at"]
    parsed = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))

    # The hour should be 12 (UTC), not 17 (IST)
    assert parsed.hour == 12, f"Expected UTC hour 12, got {parsed.hour} from {created_at_str}"
    assert parsed.tzinfo is not None, "Parsed datetime has no tzinfo"


def test_context_response_created_at_has_utc_offset():
    """ContextResponse.created_at must serialize with explicit UTC offset."""
    naive_utc = datetime(2026, 7, 30, 14, 30, 0)

    model = ContextResponse(
        project_id=None,
        session_id=None,
        agent_id=None,
        memory_items=[],
        evidence_packet=None,
        token_budget=4000,
        token_used=0,
        created_at=naive_utc,
        has_memory=False,
        has_evidence=False,
    )

    # Serialize to JSON-compatible dict
    data = model.model_dump(mode="json")

    # Check created_at field
    timestamp_str = data["created_at"]
    assert isinstance(timestamp_str, str), f"Expected string, got {type(timestamp_str)}"

    # Must have explicit UTC offset
    assert (
        timestamp_str.endswith("Z")
        or "+00:00" in timestamp_str
        or "Z" in timestamp_str
    ), f"created_at missing UTC offset: {timestamp_str}"

    # Verify it can be parsed as ISO 8601 with timezone
    parsed = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, f"Parsed datetime has no tzinfo: {parsed}"


def test_evidence_ref_model_default_timestamp_is_utc_aware():
    """EvidenceRefModel default timestamp factory must produce UTC-aware datetime."""
    model = EvidenceRefModel(
        source_id="doc-123",
        source_type="document",
    )

    # Serialize to JSON-compatible dict
    data = model.model_dump(mode="json")
    timestamp_str = data["timestamp"]

    # Must have explicit UTC offset
    assert (
        timestamp_str.endswith("Z")
        or "+00:00" in timestamp_str
        or "Z" in timestamp_str
    ), f"Default timestamp missing UTC offset: {timestamp_str}"

    # Verify it can be parsed as ISO 8601 with timezone
    parsed = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, f"Parsed datetime has no tzinfo: {parsed}"
