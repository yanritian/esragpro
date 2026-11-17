"""P6-B: ProjectionEvent mapping test.

Verifies that the adapter output is accepted by the existing
``ProjectionEvent`` contract and carries the full envelope payload.
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.models.world_memory import WorldMemoryEnvelope
from app.memory_core.projection_worker.contracts import ProjectionEvent
from app.memory_core.repositories.world_memory_authority import (
    WorldMemoryAuthorityRepository,
)
from app.memory_core.repositories.world_memory_projection_outbox import (
    WorldMemoryProjectionOutboxRepository,
)

from .conftest import make_observation_envelope


async def test_map_to_projection_event_produces_valid_event(session_factory):
    """The adapter produces a ProjectionEvent accepted by the contract."""
    envelope_dict = make_observation_envelope(content="mapping test")

    async with session_factory() as session:
        authority_repo = WorldMemoryAuthorityRepository(session)
        stored = await authority_repo.remember(envelope_dict)

        result = await session.execute(
            select(WorldMemoryEnvelope).where(
                WorldMemoryEnvelope.idempotency_key == stored.idempotency_key
            )
        )
        orm_row = result.scalar_one()

        projection_repo = WorldMemoryProjectionOutboxRepository(session)
        rows = await projection_repo.enqueue_for_targets(
            orm_row, targets=["elasticsearch"]
        )
        await session.commit()

    # Map to ProjectionEvent
    event = projection_repo.map_to_projection_event(rows[0])

    # Verify it's a valid ProjectionEvent
    assert isinstance(event, ProjectionEvent)
    assert event.outbox_id == rows[0].id
    assert event.aggregate_id == stored.aggregate_id
    assert event.event_type == stored.event_type
    assert event.schema_version == stored.schema_version
    assert event.projection_target == "elasticsearch"
    assert event.idempotency_key == rows[0].idempotency_key
    assert event.attempt_count == 0
    assert event.max_attempts == 8


async def test_projection_event_payload_is_full_envelope(session_factory):
    """The ProjectionEvent payload contains the complete envelope."""
    envelope_dict = make_observation_envelope(content="payload test")

    async with session_factory() as session:
        authority_repo = WorldMemoryAuthorityRepository(session)
        stored = await authority_repo.remember(envelope_dict)

        result = await session.execute(
            select(WorldMemoryEnvelope).where(
                WorldMemoryEnvelope.idempotency_key == stored.idempotency_key
            )
        )
        orm_row = result.scalar_one()

        projection_repo = WorldMemoryProjectionOutboxRepository(session)
        rows = await projection_repo.enqueue_for_targets(
            orm_row, targets=["neo4j"]
        )
        await session.commit()

    event = projection_repo.map_to_projection_event(rows[0])
    payload = event.payload  # deep-copied

    # Verify payload structure matches the original envelope
    assert payload["event_type"] == stored.event_type
    assert payload["schema_version"] == stored.schema_version
    assert payload["aggregate_type"] == "world_memory"
    assert "world_memory_snapshot" in payload
    assert payload["world_memory_snapshot"]["_kind"] == "observation"


async def test_projection_event_payload_is_deep_copy(session_factory):
    """Mutating the payload does not affect the stored row."""
    envelope_dict = make_observation_envelope()

    async with session_factory() as session:
        authority_repo = WorldMemoryAuthorityRepository(session)
        stored = await authority_repo.remember(envelope_dict)

        result = await session.execute(
            select(WorldMemoryEnvelope).where(
                WorldMemoryEnvelope.idempotency_key == stored.idempotency_key
            )
        )
        orm_row = result.scalar_one()

        projection_repo = WorldMemoryProjectionOutboxRepository(session)
        rows = await projection_repo.enqueue_for_targets(
            orm_row, targets=["elasticsearch"]
        )
        await session.commit()

    event = projection_repo.map_to_projection_event(rows[0])

    # Mutate the payload
    payload = event.payload
    payload["mutated"] = True

    # Re-access — should be a fresh deep copy without mutation
    payload_2 = event.payload
    assert "mutated" not in payload_2


async def test_projection_event_accepted_by_handler_contract(session_factory):
    """The mapped event satisfies the ProjectionHandler protocol signature."""

    envelope_dict = make_observation_envelope()

    async with session_factory() as session:
        authority_repo = WorldMemoryAuthorityRepository(session)
        stored = await authority_repo.remember(envelope_dict)

        result = await session.execute(
            select(WorldMemoryEnvelope).where(
                WorldMemoryEnvelope.idempotency_key == stored.idempotency_key
            )
        )
        orm_row = result.scalar_one()

        projection_repo = WorldMemoryProjectionOutboxRepository(session)
        rows = await projection_repo.enqueue_for_targets(
            orm_row, targets=["elasticsearch"]
        )
        await session.commit()

    event = projection_repo.map_to_projection_event(rows[0])

    # The event must be a valid ProjectionEvent that a handler can accept
    assert hasattr(event, "payload")
    assert hasattr(event, "outbox_id")
    assert hasattr(event, "projection_target")
    assert hasattr(event, "idempotency_key")

    # Verify it can be used as a ProjectionEvent (type check)
    assert isinstance(event, ProjectionEvent)
