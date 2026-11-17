from __future__ import annotations

from datetime import datetime, timezone
from typing import LiteralString
from uuid import uuid4

import pytest
from elastic_transport import ObjectApiResponse
from elasticsearch import AsyncElasticsearch
from neo4j import AsyncDriver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.world_memory import (
    WorldMemoryEnvelope,
    WorldMemoryProjectionOutbox,
)
from app.memory_core.repositories.world_memory_authority import (
    WorldMemoryAuthorityRepository,
)
from app.memory_core.repositories.world_memory_projection_outbox import (
    StoredProjectionRow,
    WorldMemoryProjectionOutboxRepository,
)
from app.memory_core.world_memory import WorldObservation
from app.memory_core.world_memory_projection import (
    create_world_memory_projection_worker,
)
from app.memory_core.world_memory_serializer import (
    EVENT_OBSERVATION_INGESTED,
    build_world_memory_envelope,
)


async def _elasticsearch_count(
    client: AsyncElasticsearch,
    index_name: str,
) -> int:
    await client.indices.refresh(index=index_name)
    response: ObjectApiResponse[dict[str, int]] = await client.count(index=index_name)
    count = response.body["count"]
    assert isinstance(count, int)
    return count


async def _neo4j_count(
    driver: AsyncDriver,
    query: LiteralString,
    *,
    key: str,
) -> int:
    async with driver.session(database="neo4j") as session:
        result = await session.run(query, key=key)
        record = await result.single()
        assert record is not None
        count = record["count"]
        assert isinstance(count, int)
        return count


async def _cleanup_neo4j(
    driver: AsyncDriver,
    receipt_key: str,
    node_identities: tuple[str, ...] | list[str],
) -> None:
    async with driver.session(database="neo4j") as session:
        receipt_result = await session.run(
            "MATCH (receipt:WorldMemoryProjectionReceipt {idempotency_key: $key}) DELETE receipt",
            key=receipt_key,
        )
        _ = await receipt_result.consume()
        node_result = await session.run(
            "MATCH (node:WorldMemoryNode) WHERE node.identity IN $identities DETACH DELETE node",
            identities=node_identities,
        )
        _ = await node_result.consume()


async def _stored_projection_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[WorldMemoryProjectionOutbox]:
    async with session_factory() as session:
        result = await session.execute(select(WorldMemoryProjectionOutbox))
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_factory_worker_projects_both_targets_idempotently(
    p6_session_factory: async_sessionmaker[AsyncSession],
    elasticsearch_client: AsyncElasticsearch,
    world_memory_index_name: str,
    neo4j_driver: AsyncDriver,
) -> None:
    observation_id = uuid4()
    content = f"P6-F dual-target live observation {uuid4().hex}"
    evidence_ref = f"p6-f-evidence://{uuid4().hex}"
    observation = WorldObservation(
        id=observation_id,
        content=content,
        observed_at=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
        source="p6-f-live-test",
        ingested_at=datetime(2026, 8, 1, 12, 0, 1, tzinfo=timezone.utc),
        raw_evidence_ref=evidence_ref,
    )
    envelope = build_world_memory_envelope(
        observation,
        event_type=EVENT_OBSERVATION_INGESTED,
        projection_target="elasticsearch",
    )

    async with p6_session_factory() as session:
        authority = WorldMemoryAuthorityRepository(session)
        stored = await authority.remember(envelope)
        result = await session.execute(
            select(WorldMemoryEnvelope).where(
                WorldMemoryEnvelope.idempotency_key == stored.idempotency_key,
            )
        )
        authority_row = result.scalar_one()
        projection = WorldMemoryProjectionOutboxRepository(session)
        stored_rows: list[StoredProjectionRow] = await projection.enqueue_for_targets(
            authority_row,
            targets=["elasticsearch", "neo4j"],
        )
        await session.commit()

    assert len(stored_rows) == 2
    elasticsearch_row = next(row for row in stored_rows if row.projection_target == "elasticsearch")
    neo4j_row = next(row for row in stored_rows if row.projection_target == "neo4j")
    try:
        worker = await create_world_memory_projection_worker(
            session_factory=p6_session_factory,
            elasticsearch_client=elasticsearch_client,
            neo4j_driver=neo4j_driver,
            worker_identity=f"p6-f-dual-target-{uuid4().hex}",
            elasticsearch_index_name=world_memory_index_name,
            neo4j_database="neo4j",
        )
        first_result = await worker.run_once()
        assert first_result.claimed == 2
        assert first_result.completed == 2
        assert first_result.retried == 0
        assert first_result.dead_lettered == 0
        assert first_result.lease_lost == 0
        assert first_result.state_write_errors == 0

        completed_rows = await _stored_projection_rows(p6_session_factory)
        assert {row.id for row in completed_rows} == {
            elasticsearch_row.id,
            neo4j_row.id,
        }
        assert {row.state for row in completed_rows} == {"completed"}

        assert await _elasticsearch_count(elasticsearch_client, world_memory_index_name) == 1
        _ = await elasticsearch_client.get(
            index=world_memory_index_name,
            id=elasticsearch_row.idempotency_key,
        )
        assert (
            await _neo4j_count(
                neo4j_driver,
                "MATCH (receipt:WorldMemoryProjectionReceipt {idempotency_key: $key}) "
                "RETURN count(receipt) AS count",
                key=neo4j_row.idempotency_key,
            )
            == 1
        )
        assert (
            await _neo4j_count(
                neo4j_driver,
                "MATCH (node:WorldMemoryNode {identity: $key}) RETURN count(node) AS count",
                key=str(observation_id),
            )
            == 1
        )

        es_count = await _elasticsearch_count(elasticsearch_client, world_memory_index_name)
        receipt_count = await _neo4j_count(
            neo4j_driver,
            "MATCH (receipt:WorldMemoryProjectionReceipt {idempotency_key: $key}) "
            "RETURN count(receipt) AS count",
            key=neo4j_row.idempotency_key,
        )
        node_count = await _neo4j_count(
            neo4j_driver,
            "MATCH (node:WorldMemoryNode {identity: $key}) RETURN count(node) AS count",
            key=str(observation_id),
        )

        second_result = await worker.run_once()
        assert second_result.claimed == 0
        assert second_result.completed == 0
        assert second_result.retried == 0
        assert second_result.dead_lettered == 0
        assert second_result.state_write_errors == 0
        assert await _elasticsearch_count(elasticsearch_client, world_memory_index_name) == es_count
        assert (
            await _neo4j_count(
                neo4j_driver,
                "MATCH (receipt:WorldMemoryProjectionReceipt {idempotency_key: $key}) "
                "RETURN count(receipt) AS count",
                key=neo4j_row.idempotency_key,
            )
            == receipt_count
        )
        assert (
            await _neo4j_count(
                neo4j_driver,
                "MATCH (node:WorldMemoryNode {identity: $key}) RETURN count(node) AS count",
                key=str(observation_id),
            )
            == node_count
        )
    finally:
        await _cleanup_neo4j(
            neo4j_driver,
            neo4j_row.idempotency_key,
            (str(observation_id), evidence_ref),
        )
        await elasticsearch_client.indices.delete(
            index=world_memory_index_name,
            ignore_unavailable=True,
        )
