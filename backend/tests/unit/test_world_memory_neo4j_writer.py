from __future__ import annotations

from types import TracebackType
from typing import Final, Never, Self
from uuid import UUID

import pytest

from app.memory_core.projection_worker.contracts import PermanentProjectionError
from app.memory_core.world_memory_projection._commands import Neo4jCommand, ProjectionMetadata
from app.memory_core.world_memory_projection._neo4j_codec import (
    canonical_json,
    encode_command,
    materialize_properties,
)
from app.memory_core.world_memory_projection._neo4j_writer import (
    WorldMemoryNeo4jWriter,
)
from app.memory_core.world_memory_projection._write_codec import JsonValue
from app.memory_core.world_memory_projection.payloads import (
    Neo4jNode,
    Neo4jPayload,
    Neo4jRelationship,
)


class StrictFakeDriver:
    closed: bool

    def __init__(self) -> None:
        self.closed = False

    def session(self, **config: str) -> Never:
        raise AssertionError(f"unexpected session: {config}")

    async def close(self) -> None:
        self.closed = True


class _SchemaResult:
    async def consume(self) -> None:
        return None


class _SchemaSession:
    def __init__(self, statements: list[str]) -> None:
        self._statements = statements

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def run(self, query: str, **parameters: JsonValue) -> _SchemaResult:
        assert parameters == {}
        self._statements.append(query)
        return _SchemaResult()


class _SchemaDriver:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def session(self, **config: str) -> _SchemaSession:
        assert config == {"database": "neo4j"}
        return _SchemaSession(self.statements)

    async def close(self) -> None:
        return None


def _command(
    *,
    key: str = "neo4j-key-1",
    label: str = "Observation",
    rel_type: str = "SUPPORTED_BY",
    content: str = "stable observation",
) -> Neo4jCommand:
    payload = Neo4jPayload(
        primary_node=Neo4jNode(
            labels=(label, "WorldMemoryNode"),
            identity="object-1",
            properties={
                "content": content,
                "confidence": 0.9,
                "active": True,
                "tags": ["one", "two"],
                "audit": {"source": "sensor", "nested": {"priority": 1}},
            },
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
                rel_type=rel_type,
                from_identity="object-1",
                to_identity="evidence-1",
                properties={"weight": 1, "metadata": {"kind": "direct"}},
            ),
        ),
        source_envelope={
            "aggregate_id": "00000000-0000-0000-0000-000000000001",
            "world_memory_snapshot": {
                "id": "object-1",
                "content": content,
                "nested": {"retained": True},
            },
        },
    )
    metadata = ProjectionMetadata(
        outbox_id=UUID("00000000-0000-0000-0000-000000000002"),
        aggregate_id=UUID("00000000-0000-0000-0000-000000000001"),
        event_type="world.observation.ingested",
        schema_version=7,
        projection_target="neo4j",
        idempotency_key=key,
    )
    return Neo4jCommand(metadata=metadata, payload=payload)


def test_encode_command_is_deterministic_and_complete() -> None:
    first_json, first_fingerprint, first_data = encode_command(_command())
    second_json, second_fingerprint, second_data = encode_command(_command())

    assert first_json == second_json
    assert first_fingerprint == second_fingerprint
    assert len(first_fingerprint) == 64
    assert first_data == {
        "metadata": {
            "outbox_id": "00000000-0000-0000-0000-000000000002",
            "aggregate_id": "00000000-0000-0000-0000-000000000001",
            "event_type": "world.observation.ingested",
            "schema_version": 7,
            "projection_target": "neo4j",
            "idempotency_key": "neo4j-key-1",
        },
        "primary_node": {
            "labels": ["Observation", "WorldMemoryNode"],
            "identity": "object-1",
            "properties": {
                "content": "stable observation",
                "confidence": 0.9,
                "active": True,
                "tags": ["one", "two"],
                "audit": {"source": "sensor", "nested": {"priority": 1}},
            },
        },
        "related_nodes": [
            {"labels": ["Evidence"], "identity": "evidence-1", "properties": {"title": "source"}}
        ],
        "relationships": [
            {
                "rel_type": "SUPPORTED_BY",
                "from_identity": "object-1",
                "to_identity": "evidence-1",
                "properties": {"weight": 1, "metadata": {"kind": "direct"}},
            }
        ],
        "source_envelope": {
            "aggregate_id": "00000000-0000-0000-0000-000000000001",
            "world_memory_snapshot": {
                "id": "object-1",
                "content": "stable observation",
                "nested": {"retained": True},
            },
        },
    }
    assert second_data == first_data


def test_encode_command_fingerprint_changes_for_payload_or_key() -> None:
    _, fingerprint, _ = encode_command(_command())
    _, changed_payload, _ = encode_command(_command(content="changed"))
    _, changed_key, _ = encode_command(_command(key="neo4j-key-2"))

    assert fingerprint != changed_payload
    assert fingerprint != changed_key


def test_materialize_properties_filters_only_neo4j_unsafe_values() -> None:
    nested: Final[dict[str, JsonValue]] = {"kept": True}
    mixed: Final[list[JsonValue]] = ["safe", {"nested": True}]
    properties: dict[str, JsonValue] = {
        "text": "value",
        "count": 3,
        "ratio": 0.5,
        "enabled": True,
        "missing": None,
        "items": ["a", 2, None],
        "nested": nested,
        "mixed": mixed,
        "identity": "ignored",
        "properties_json": "ignored",
    }

    materialized = materialize_properties(properties)
    audit_json = canonical_json(properties)

    assert materialized == {
        "text": "value",
        "count": 3,
        "ratio": 0.5,
        "enabled": True,
        "missing": None,
        "items": ["a", 2, None],
    }
    assert '"nested":{"kept":true}' in audit_json
    assert '"mixed":["safe",{"nested":true}]' in audit_json
    assert '"identity":"ignored"' in audit_json


@pytest.mark.asyncio
async def test_invalid_label_is_permanent_neo4j_error_before_session() -> None:
    writer = WorldMemoryNeo4jWriter(StrictFakeDriver())

    with pytest.raises(PermanentProjectionError) as error:
        await writer(_command(label="Bad`Label"))

    assert error.value.target == "neo4j"


@pytest.mark.asyncio
async def test_ensure_schema_creates_fulltext_index_after_constraints() -> None:
    driver = _SchemaDriver()

    await WorldMemoryNeo4jWriter(driver).ensure_schema()

    assert driver.statements[:2] == [
        "CREATE CONSTRAINT world_memory_projection_receipt_idempotency_key IF NOT EXISTS FOR (receipt:WorldMemoryProjectionReceipt) REQUIRE receipt.idempotency_key IS UNIQUE",
        "CREATE CONSTRAINT world_memory_node_identity IF NOT EXISTS FOR (node:WorldMemoryNode) REQUIRE node.identity IS UNIQUE",
    ]
    assert driver.statements[2] == (
        "CREATE FULLTEXT INDEX world_memory_node_content_fulltext IF NOT EXISTS "
        "FOR (node:WorldMemoryNode) ON EACH [node.content]"
    )


@pytest.mark.asyncio
async def test_invalid_relationship_identifier_is_permanent_neo4j_error() -> None:
    writer = WorldMemoryNeo4jWriter(StrictFakeDriver())

    with pytest.raises(PermanentProjectionError) as error:
        await writer(_command(rel_type="BAD TYPE"))

    assert error.value.target == "neo4j"


@pytest.mark.asyncio
async def test_aclose_only_closes_owned_driver() -> None:
    borrowed = StrictFakeDriver()
    await WorldMemoryNeo4jWriter(borrowed).aclose()
    assert not borrowed.closed

    owned = StrictFakeDriver()
    await WorldMemoryNeo4jWriter(owned, owns_driver=True).aclose()
    assert owned.closed
