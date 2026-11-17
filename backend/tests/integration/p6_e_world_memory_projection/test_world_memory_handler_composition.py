from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.world_memory import WorldMemoryProjectionOutbox
from app.memory_core.projection_worker.world_memory_store import (
    WorldMemoryProjectionStore,
)
from app.memory_core.projection_worker.world_memory_worker import (
    WorldMemoryProjectionWorker,
)
from app.memory_core.world_memory_projection import (
    ElasticsearchCommand,
    ElasticsearchPayload,
    Neo4jCommand,
    build_world_memory_handler_registry,
)
from tests.integration.p6_c_world_memory_projection.conftest import (
    seed_authority_and_projection,
)


class _RecordingElasticsearchWriter:
    def __init__(self) -> None:
        self.commands: list[ElasticsearchCommand] = []

    async def __call__(self, command: ElasticsearchCommand) -> None:
        self.commands.append(command)


class _RecordingNeo4jWriter:
    def __init__(self) -> None:
        self.commands: list[Neo4jCommand] = []

    async def __call__(self, command: Neo4jCommand) -> None:
        self.commands.append(command)


async def test_worker_invokes_composed_handler_and_preserves_idempotency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_authority_and_projection(
        session_factory,
        target="elasticsearch",
        content="P6-E integration observation",
    )

    async with session_factory() as session:
        result = await session.execute(select(WorldMemoryProjectionOutbox))
        seeded_row = result.scalar_one()

    elasticsearch_writer = _RecordingElasticsearchWriter()
    neo4j_writer = _RecordingNeo4jWriter()
    registry = build_world_memory_handler_registry(
        elasticsearch_writer=elasticsearch_writer,
        neo4j_writer=neo4j_writer,
    )
    assert registry.targets == ("elasticsearch", "neo4j")
    worker = WorldMemoryProjectionWorker(
        WorldMemoryProjectionStore(session_factory, "p6-e-worker"),
        registry,
        "p6-e-worker",
        target_filter=set(registry.targets),
        batch_size=1,
        lease_duration=timedelta(seconds=15),
        delivery_timeout=timedelta(seconds=5),
    )

    first_result = await worker.run_once()

    assert first_result.claimed == 1
    assert first_result.completed == 1
    assert first_result.retried == 0
    assert first_result.dead_lettered == 0
    assert len(elasticsearch_writer.commands) == 1
    assert neo4j_writer.commands == []

    command = elasticsearch_writer.commands[0]
    assert command.metadata.outbox_id == seeded_row.id
    assert command.metadata.aggregate_id == seeded_row.aggregate_id
    assert command.metadata.event_type == seeded_row.event_type
    assert command.metadata.schema_version == seeded_row.schema_version
    assert command.metadata.projection_target == "elasticsearch"
    assert command.metadata.idempotency_key == seeded_row.idempotency_key
    assert isinstance(command.payload, ElasticsearchPayload)
    assert command.payload.content == "P6-E integration observation"
    assert "world_memory_snapshot" in command.payload.source_envelope

    async with session_factory() as session:
        result = await session.execute(
            select(WorldMemoryProjectionOutbox).where(
                WorldMemoryProjectionOutbox.id == seeded_row.id
            )
        )
        completed_row = result.scalar_one()
    assert completed_row.state == "completed"

    second_result = await worker.run_once()

    assert second_result.claimed == 0
    assert len(elasticsearch_writer.commands) == 1

    async with session_factory() as session:
        result = await session.execute(
            select(WorldMemoryProjectionOutbox).where(
                WorldMemoryProjectionOutbox.idempotency_key
                == seeded_row.idempotency_key
            )
        )
        replay_rows = result.scalars().all()
    assert len(replay_rows) == 1
    assert replay_rows[0].state == "completed"
