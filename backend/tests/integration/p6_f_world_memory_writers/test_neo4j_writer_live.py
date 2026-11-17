from __future__ import annotations

from collections.abc import AsyncIterator
from typing import LiteralString, Protocol, Self, runtime_checkable
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from neo4j import AsyncDriver

from app.memory_core.projection_worker.contracts import PermanentProjectionError
from app.memory_core.world_memory_projection._commands import (
    Neo4jCommand,
    ProjectionMetadata,
)
from app.memory_core.world_memory_projection._neo4j_codec import encode_command
from app.memory_core.world_memory_projection._neo4j_writer import WorldMemoryNeo4jWriter
from app.memory_core.world_memory_projection._write_codec import JsonValue
from app.memory_core.world_memory_projection.payloads import (
    Neo4jNode,
    Neo4jPayload,
    Neo4jRelationship,
)

_DATABASE = "neo4j"


@runtime_checkable
class _Neo4jRecord(Protocol):
    def __getitem__(self, key: str) -> JsonValue: ...


@runtime_checkable
class _Neo4jResult(Protocol):
    def __aiter__(self) -> AsyncIterator[_Neo4jRecord]: ...

    async def consume(self) -> object: ...

    async def single(self) -> _Neo4jRecord | None: ...


@runtime_checkable
class _Neo4jSession(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def run(self, query: LiteralString, **parameters: object) -> _Neo4jResult: ...


@runtime_checkable
class _Neo4jQueryDriver(Protocol):
    def session(self, *, database: str) -> _Neo4jSession: ...

    async def close(self) -> None: ...


def _query_driver(driver: AsyncDriver) -> _Neo4jQueryDriver:
    driver_object: object = driver
    assert isinstance(driver_object, _Neo4jQueryDriver)
    return driver_object


def _record_string(record: _Neo4jRecord, key: str) -> str:
    value = record[key]
    assert isinstance(value, str)
    return value


def _record_strings(record: _Neo4jRecord, key: str) -> list[str]:
    value = record[key]
    assert isinstance(value, list)
    values: list[JsonValue] = value
    strings: list[str] = []
    value_count = 0
    for item in values:
        assert isinstance(item, str)
        strings.append(item)
        value_count += 1
    assert len(strings) == value_count
    return strings


async def _clear_graph(driver: AsyncDriver) -> None:
    query_driver = _query_driver(driver)
    async with query_driver.session(database=_DATABASE) as session:
        result = await session.run("MATCH (n) DETACH DELETE n")
        _ = await result.consume()


async def _count(driver: AsyncDriver, query: LiteralString) -> int:
    query_driver = _query_driver(driver)
    async with query_driver.session(database=_DATABASE) as session:
        result = await session.run(query)
        record = await result.single()
        assert record is not None
        value = record["count"]
        assert isinstance(value, int)
        return value


async def _graph_counts(driver: AsyncDriver) -> tuple[int, int, int]:
    return (
        await _count(driver, "MATCH (n:WorldMemoryProjectionReceipt) RETURN count(n) AS count"),
        await _count(driver, "MATCH (n:WorldMemoryNode) RETURN count(n) AS count"),
        await _count(driver, "MATCH ()-[r]->() RETURN count(r) AS count"),
    )


@pytest_asyncio.fixture(autouse=True)
async def _clear_neo4j_graph_between_tests(neo4j_driver: AsyncDriver) -> AsyncIterator[None]:
    await _clear_graph(neo4j_driver)
    yield
    await _clear_graph(neo4j_driver)


@pytest_asyncio.fixture
async def world_memory_writer(neo4j_driver: AsyncDriver) -> AsyncIterator[WorldMemoryNeo4jWriter]:
    writer = WorldMemoryNeo4jWriter(neo4j_driver, database=_DATABASE)
    await writer.ensure_schema()
    yield writer


def _command(*, key: str, content: str = "stable observation") -> Neo4jCommand:
    payload = Neo4jPayload(
        primary_node=Neo4jNode(
            labels=("Observation_2026", "WorldMemoryNode"),
            identity="object-1",
            properties={"content": content, "version": 1, "active": True},
        ),
        related_nodes=(
            Neo4jNode(
                labels=("Evidence",),
                identity="evidence-1",
                properties={"title": "source"},
            ),
        ),
        relationships=(
            Neo4jRelationship(
                rel_type="SUPPORTED_BY_2026",
                from_identity="object-1",
                to_identity="placeholder-1",
                properties={"weight": 1},
            ),
        ),
        source_envelope={
            "aggregate_id": "00000000-0000-0000-0000-000000000001",
            "world_memory_snapshot": {"id": "object-1", "content": content},
        },
    )
    return Neo4jCommand(
        metadata=ProjectionMetadata(
            outbox_id=uuid4(),
            aggregate_id=UUID("00000000-0000-0000-0000-000000000001"),
            event_type="world.observation.ingested",
            schema_version=1,
            projection_target="neo4j",
            idempotency_key=key,
        ),
        payload=payload,
    )


@pytest.mark.asyncio
async def test_live_ensure_schema_creates_both_unique_constraints(
    neo4j_driver: AsyncDriver, world_memory_writer: WorldMemoryNeo4jWriter
) -> None:
    await world_memory_writer.ensure_schema()
    query_driver = _query_driver(neo4j_driver)
    async with query_driver.session(database=_DATABASE) as session:
        result = await session.run(
            "SHOW CONSTRAINTS YIELD name WHERE name IN "
            + "['world_memory_projection_receipt_idempotency_key', 'world_memory_node_identity'] "
            + "RETURN name"
        )
        names = {_record_string(record, "name") async for record in result}
    assert names == {
        "world_memory_projection_receipt_idempotency_key",
        "world_memory_node_identity",
    }


@pytest.mark.asyncio
async def test_live_first_write_has_receipt_nodes_relationship_and_audit_fields(
    neo4j_driver: AsyncDriver, world_memory_writer: WorldMemoryNeo4jWriter
) -> None:
    command = _command(key=f"p6-f-first-{uuid4().hex}")
    command_json, fingerprint, _ = encode_command(command)

    await world_memory_writer(command)

    assert await _graph_counts(neo4j_driver) == (1, 3, 1)
    query_driver = _query_driver(neo4j_driver)
    async with query_driver.session(database=_DATABASE) as session:
        receipt_result = await session.run(
            "MATCH (receipt:WorldMemoryProjectionReceipt {idempotency_key: $key}) "
            + "RETURN receipt.command_json AS command_json, receipt.fingerprint AS fingerprint",
            key=command.metadata.idempotency_key,
        )
        receipt = await receipt_result.single()
        assert receipt is not None
        receipt_command_json = _record_string(receipt, "command_json")
        receipt_fingerprint = _record_string(receipt, "fingerprint")
        assert receipt_command_json == command_json
        assert receipt_fingerprint == fingerprint
        assert len(receipt_fingerprint) == 64

        node_result = await session.run(
            "MATCH (node:WorldMemoryNode) RETURN node.identity AS identity, labels(node) AS labels, "
            + "node.content AS content ORDER BY node.identity"
        )
        nodes = [record async for record in node_result]
        identities = [_record_string(record, "identity") for record in nodes]
        assert identities == [
            "evidence-1",
            "object-1",
            "placeholder-1",
        ]
        primary = nodes[1]
        primary_labels = _record_strings(primary, "labels")
        primary_content = _record_string(primary, "content")
        assert "Observation_2026" in primary_labels
        assert primary_content == "stable observation"


@pytest.mark.asyncio
async def test_live_exact_replay_leaves_graph_counts_unchanged(
    neo4j_driver: AsyncDriver, world_memory_writer: WorldMemoryNeo4jWriter
) -> None:
    command = _command(key=f"p6-f-replay-{uuid4().hex}")
    await world_memory_writer(command)
    before = await _graph_counts(neo4j_driver)

    await world_memory_writer(command)

    assert await _graph_counts(neo4j_driver) == before


@pytest.mark.asyncio
async def test_live_changed_payload_same_key_is_permanent_and_graph_unchanged(
    neo4j_driver: AsyncDriver, world_memory_writer: WorldMemoryNeo4jWriter
) -> None:
    key = f"p6-f-conflict-{uuid4().hex}"
    original = _command(key=key)
    changed = _command(key=key, content="changed observation")
    await world_memory_writer(original)
    before = await _graph_counts(neo4j_driver)

    with pytest.raises(PermanentProjectionError) as error:
        await world_memory_writer(changed)

    assert error.value.target == "neo4j"
    assert await _graph_counts(neo4j_driver) == before


@pytest.mark.asyncio
async def test_live_distinct_key_updates_one_stable_graph_without_duplicates(
    neo4j_driver: AsyncDriver, world_memory_writer: WorldMemoryNeo4jWriter
) -> None:
    first = _command(key=f"p6-f-history-a-{uuid4().hex}")
    second = _command(key=f"p6-f-history-b-{uuid4().hex}", content="updated observation")
    await world_memory_writer(first)
    await world_memory_writer(second)

    assert await _graph_counts(neo4j_driver) == (2, 3, 1)
    query_driver = _query_driver(neo4j_driver)
    async with query_driver.session(database=_DATABASE) as session:
        result = await session.run(
            "MATCH (node:WorldMemoryNode {identity: 'object-1'}) "
            + "RETURN node.content AS content"
        )
        record = await result.single()
        assert record is not None
        assert _record_string(record, "content") == "updated observation"


@pytest.mark.asyncio
async def test_live_cleanup_leaves_world_memory_graph_empty(
    neo4j_driver: AsyncDriver, world_memory_writer: WorldMemoryNeo4jWriter
) -> None:
    await world_memory_writer(_command(key=f"p6-f-cleanup-{uuid4().hex}"))
    await _clear_graph(neo4j_driver)

    assert (
        await _count(
            neo4j_driver, "MATCH (n:WorldMemoryProjectionReceipt) RETURN count(n) AS count"
        )
        == 0
    )
    assert await _count(neo4j_driver, "MATCH (n:WorldMemoryNode) RETURN count(n) AS count") == 0
