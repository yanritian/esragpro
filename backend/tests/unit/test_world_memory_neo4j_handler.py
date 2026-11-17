"""P6-E unit tests: world-memory Neo4j handler boundary.

Proves the Neo4j handler:

- delivers metadata + complete typed payload to the fake writer;
- rejects target mismatch as :class:`PermanentProjectionError`;
- translates malformed / unsupported snapshots to permanent failure;
- propagates writer exceptions unchanged (retryable by P6-C worker).

Structure: Given/When/Then (BDD-style).
"""
from __future__ import annotations

import uuid
from inspect import signature

import pytest

from app.memory_core.projection_worker.contracts import (
    PermanentProjectionError,
    ProjectionEvent,
)
from app.memory_core.world_memory_projection import (
    Neo4jCommand,
    Neo4jPayload,
    ProjectionMetadata,
    WorldMemoryNeo4jHandler,
)
from tests.unit.projection_fixtures import _obs_snapshot


def _event_for_target(target: str) -> ProjectionEvent:
    agg = uuid.uuid4()
    return ProjectionEvent(
        outbox_id=uuid.uuid4(),
        aggregate_id=agg,
        event_type="world.test.event",
        schema_version=1,
        projection_target=target,
        idempotency_key=f"idem-{uuid.uuid4()}",
        payload={"world_memory_snapshot": _obs_snapshot()},
        attempt_count=1,
        max_attempts=3,
    )


class _FakeNeo4jWriter:
    """Records the received :class:`Neo4jCommand`."""

    def __init__(self, to_raise: BaseException | None = None) -> None:
        self.calls: list[Neo4jCommand] = []
        self._to_raise = to_raise

    async def __call__(self, command: Neo4jCommand) -> None:
        if self._to_raise is not None:
            raise self._to_raise
        self.calls.append(command)


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_neo4j_handler_delivers_metadata_and_payload():
    """Given a valid event, handler passes metadata + typed Neo4j payload
    to the writer."""
    event = _event_for_target("neo4j")
    original_payload = event.payload
    writer = _FakeNeo4jWriter()
    handler = WorldMemoryNeo4jHandler(writer)

    await handler(event)

    assert len(writer.calls) == 1
    command = writer.calls[0]
    assert isinstance(command, Neo4jCommand)

    meta: ProjectionMetadata = command.metadata
    assert meta.outbox_id == event.outbox_id
    assert meta.aggregate_id == event.aggregate_id
    assert meta.event_type == event.event_type
    assert meta.schema_version == event.schema_version
    assert meta.projection_target == "neo4j"
    assert meta.idempotency_key == event.idempotency_key

    payload: Neo4jPayload = command.payload
    assert isinstance(payload, Neo4jPayload)
    assert payload.primary_node.identity == "obs-001"
    assert "WorldObservation" in payload.primary_node.labels
    assert isinstance(payload.source_envelope, dict)
    assert "world_memory_snapshot" in payload.source_envelope
    assert event.payload == original_payload


# ---------------------------------------------------------------------------
# Target mismatch
# ---------------------------------------------------------------------------


def test_neo4j_handler_target_is_fixed_by_contract() -> None:
    parameters = tuple(signature(WorldMemoryNeo4jHandler).parameters)

    assert parameters == ("writer",)
    assert WorldMemoryNeo4jHandler(_FakeNeo4jWriter()).target == "neo4j"


@pytest.mark.anyio
async def test_neo4j_handler_rejects_target_mismatch():
    """Given an event with target 'elasticsearch', handler raises
    :class:`PermanentProjectionError`."""
    event = _event_for_target("elasticsearch")
    writer = _FakeNeo4jWriter()
    handler = WorldMemoryNeo4jHandler(writer)

    with pytest.raises(PermanentProjectionError) as excinfo:
        await handler(event)

    assert excinfo.value.target == "neo4j"
    assert "elasticsearch" in str(excinfo.value)
    assert writer.calls == []


# ---------------------------------------------------------------------------
# Malformed snapshot
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_neo4j_handler_translates_malformed_snapshot_to_permanent():
    """Given a malformed snapshot, handler raises
    :class:`PermanentProjectionError`."""
    event = ProjectionEvent(
        outbox_id=uuid.uuid4(),
        aggregate_id=uuid.uuid4(),
        event_type="world.test.event",
        schema_version=1,
        projection_target="neo4j",
        idempotency_key="idem-malformed",
        payload={"world_memory_snapshot": {"id": "x"}},  # missing _kind
        attempt_count=1,
        max_attempts=3,
    )
    writer = _FakeNeo4jWriter()
    handler = WorldMemoryNeo4jHandler(writer)

    with pytest.raises(PermanentProjectionError) as excinfo:
        await handler(event)

    assert excinfo.value.target == "neo4j"
    assert writer.calls == []


@pytest.mark.anyio
async def test_neo4j_handler_translates_unsupported_kind_to_permanent():
    """Given an unknown snapshot kind, handler raises
    :class:`PermanentProjectionError`."""
    event = ProjectionEvent(
        outbox_id=uuid.uuid4(),
        aggregate_id=uuid.uuid4(),
        event_type="world.test.event",
        schema_version=1,
        projection_target="neo4j",
        idempotency_key="idem-unknown",
        payload={"world_memory_snapshot": {"_kind": "weird_kind", "id": "x"}},
        attempt_count=1,
        max_attempts=3,
    )
    writer = _FakeNeo4jWriter()
    handler = WorldMemoryNeo4jHandler(writer)

    with pytest.raises(PermanentProjectionError):
        await handler(event)

    assert writer.calls == []


# ---------------------------------------------------------------------------
# Writer exception propagation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_neo4j_handler_propagates_writer_exception_unchanged():
    """Writer exceptions propagate unchanged — the P6-C worker treats
    them as retryable."""

    class TransientError(Exception):
        pass

    transient = TransientError("Neo4j unavailable")
    writer = _FakeNeo4jWriter(to_raise=transient)
    handler = WorldMemoryNeo4jHandler(writer)
    event = _event_for_target("neo4j")

    with pytest.raises(TransientError) as excinfo:
        await handler(event)

    assert excinfo.value is transient
    assert str(excinfo.value) == "Neo4j unavailable"


@pytest.mark.anyio
async def test_neo4j_handler_propagates_oserror_from_writer():
    """Infrastructure errors (e.g. OSError) propagate unchanged."""
    writer = _FakeNeo4jWriter(to_raise=OSError("connection lost"))
    handler = WorldMemoryNeo4jHandler(writer)
    event = _event_for_target("neo4j")

    with pytest.raises(OSError) as excinfo:
        await handler(event)

    assert "connection lost" in str(excinfo.value)
