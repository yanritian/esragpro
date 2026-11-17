"""P6-E unit tests: world-memory Elasticsearch handler boundary.

Proves the handler:

- delivers metadata + complete typed payload to the fake writer;
- rejects target mismatch as :class:`PermanentProjectionError`;
- translates malformed / unsupported snapshots to permanent failure;
- propagates writer exceptions unchanged (retryable by P6-C worker);
- never mutates the incoming event or the adapted payload.

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
    ElasticsearchCommand,
    ElasticsearchPayload,
    ProjectionMetadata,
    WorldMemoryElasticsearchHandler,
)
from app.memory_core.world_memory_serializer import KIND_OBSERVATION
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


class _FakeESWriter:
    """Records the received :class:`ElasticsearchCommand`."""

    def __init__(self, to_raise: BaseException | None = None) -> None:
        self.calls: list[ElasticsearchCommand] = []
        self._to_raise = to_raise

    async def __call__(self, command: ElasticsearchCommand) -> None:
        if self._to_raise is not None:
            raise self._to_raise
        self.calls.append(command)


# ---------------------------------------------------------------------------
# Success path: metadata + payload reach the writer
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_handler_delivers_metadata_and_payload_to_writer():
    """Given a valid event, when invoked, handler passes metadata and
    complete typed payload to the writer."""
    event = _event_for_target("elasticsearch")
    writer = _FakeESWriter()
    handler = WorldMemoryElasticsearchHandler(writer)

    await handler(event)

    assert len(writer.calls) == 1
    command = writer.calls[0]
    assert isinstance(command, ElasticsearchCommand)

    # Metadata mirrors ProjectionEvent identity fields.
    meta: ProjectionMetadata = command.metadata
    assert meta.outbox_id == event.outbox_id
    assert meta.aggregate_id == event.aggregate_id
    assert meta.event_type == event.event_type
    assert meta.schema_version == event.schema_version
    assert meta.projection_target == "elasticsearch"
    assert meta.idempotency_key == event.idempotency_key

    # Payload is the full typed ES payload from P6-D.
    payload: ElasticsearchPayload = command.payload
    assert isinstance(payload, ElasticsearchPayload)
    assert payload.id == "obs-001"
    assert payload.object_kind == KIND_OBSERVATION
    assert payload.content == "sensor reading 42"
    assert payload.evidence_ids == ("ev-1", "ev-2")
    assert payload.version == 1
    # Source envelope is preserved for audit / replay.
    assert isinstance(payload.source_envelope, dict)
    assert "world_memory_snapshot" in payload.source_envelope


@pytest.mark.anyio
async def test_handler_does_not_mutate_event_or_payload():
    """Handler must not modify the incoming event's payload snapshot."""
    event = _event_for_target("elasticsearch")
    original_payload = event.payload
    original_snapshot = original_payload["world_memory_snapshot"]
    writer = _FakeESWriter()
    handler = WorldMemoryElasticsearchHandler(writer)

    await handler(event)

    # The event's payload must still deep-equal the original.
    assert event.payload == original_payload
    assert event.payload["world_memory_snapshot"] == original_snapshot


# ---------------------------------------------------------------------------
# Target mismatch → permanent failure
# ---------------------------------------------------------------------------


def test_elasticsearch_handler_target_is_fixed_by_contract() -> None:
    parameters = tuple(signature(WorldMemoryElasticsearchHandler).parameters)

    assert parameters == ("writer",)
    assert WorldMemoryElasticsearchHandler(_FakeESWriter()).target == (
        "elasticsearch"
    )


@pytest.mark.anyio
async def test_handler_rejects_target_mismatch_permanently():
    """Given an event whose projection_target differs, handler raises
    :class:`PermanentProjectionError` without invoking the writer."""
    event = _event_for_target("neo4j")  # registered for "elasticsearch"
    writer = _FakeESWriter()
    handler = WorldMemoryElasticsearchHandler(writer)

    with pytest.raises(PermanentProjectionError) as excinfo:
        await handler(event)

    assert excinfo.value.target == "elasticsearch"
    assert "neo4j" in str(excinfo.value)
    assert writer.calls == []


# ---------------------------------------------------------------------------
# Malformed / unsupported snapshot → permanent failure
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_handler_translates_malformed_snapshot_to_permanent():
    """Given a snapshot missing the ``_kind`` discriminator, handler
    raises :class:`PermanentProjectionError`."""
    malformed = {"id": "obs-broken"}  # missing _kind
    event = ProjectionEvent(
        outbox_id=uuid.uuid4(),
        aggregate_id=uuid.uuid4(),
        event_type="world.test.event",
        schema_version=1,
        projection_target="elasticsearch",
        idempotency_key="idem-malformed",
        payload={"world_memory_snapshot": malformed},
        attempt_count=1,
        max_attempts=3,
    )
    writer = _FakeESWriter()
    handler = WorldMemoryElasticsearchHandler(writer)

    with pytest.raises(PermanentProjectionError) as excinfo:
        await handler(event)

    assert excinfo.value.target == "elasticsearch"
    assert writer.calls == []


@pytest.mark.anyio
async def test_handler_translates_unsupported_kind_to_permanent():
    """Given an unknown snapshot kind, handler raises
    :class:`PermanentProjectionError`."""
    unknown = {"_kind": "unknown_kind", "id": "x-1"}
    event = ProjectionEvent(
        outbox_id=uuid.uuid4(),
        aggregate_id=uuid.uuid4(),
        event_type="world.test.event",
        schema_version=1,
        projection_target="elasticsearch",
        idempotency_key="idem-unknown",
        payload={"world_memory_snapshot": unknown},
        attempt_count=1,
        max_attempts=3,
    )
    writer = _FakeESWriter()
    handler = WorldMemoryElasticsearchHandler(writer)

    with pytest.raises(PermanentProjectionError):
        await handler(event)

    assert writer.calls == []


@pytest.mark.anyio
async def test_handler_translates_missing_snapshot_to_permanent():
    """Given a payload without ``world_memory_snapshot``, handler raises
    :class:`PermanentProjectionError`."""
    event = ProjectionEvent(
        outbox_id=uuid.uuid4(),
        aggregate_id=uuid.uuid4(),
        event_type="world.test.event",
        schema_version=1,
        projection_target="elasticsearch",
        idempotency_key="idem-no-snap",
        payload={"something_else": {}},
        attempt_count=1,
        max_attempts=3,
    )
    writer = _FakeESWriter()
    handler = WorldMemoryElasticsearchHandler(writer)

    with pytest.raises(PermanentProjectionError):
        await handler(event)

    assert writer.calls == []


# ---------------------------------------------------------------------------
# Writer exception → propagate unchanged (retryable)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_handler_propagates_writer_exception_unchanged():
    """Given a writer that raises, handler propagates the same exception
    instance — P6-C worker treats this as retryable."""

    class TransientError(Exception):
        pass

    transient = TransientError("ES unavailable")
    writer = _FakeESWriter(to_raise=transient)
    handler = WorldMemoryElasticsearchHandler(writer)
    event = _event_for_target("elasticsearch")

    with pytest.raises(TransientError) as excinfo:
        await handler(event)

    assert excinfo.value is transient  # same instance, unwrapped
    assert str(excinfo.value) == "ES unavailable"


@pytest.mark.anyio
async def test_handler_propagates_oserror_from_writer():
    """Infrastructure errors (e.g. OSError) propagate unchanged."""
    writer = _FakeESWriter(to_raise=OSError("network down"))
    handler = WorldMemoryElasticsearchHandler(writer)
    event = _event_for_target("elasticsearch")

    with pytest.raises(OSError) as excinfo:
        await handler(event)

    assert "network down" in str(excinfo.value)
