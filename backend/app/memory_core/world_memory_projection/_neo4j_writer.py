"""Concrete Neo4j writer for world-memory projection commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import TracebackType
from typing import Final, Protocol, Self, overload, runtime_checkable

from neo4j import AsyncDriver, Query

from ..projection_worker.contracts import PermanentProjectionError
from ._commands import Neo4jCommand
from ._neo4j_codec import (
    canonical_json,
    encode_command,
    materialize_properties,
    validate_identifier,
)
from ._write_codec import JsonValue

_RECEIPT_CONSTRAINT: Final[str] = (
    "CREATE CONSTRAINT world_memory_projection_receipt_idempotency_key IF NOT EXISTS "
    "FOR (receipt:WorldMemoryProjectionReceipt) REQUIRE receipt.idempotency_key IS UNIQUE"
)
_NODE_CONSTRAINT: Final[str] = (
    "CREATE CONSTRAINT world_memory_node_identity IF NOT EXISTS "
    "FOR (node:WorldMemoryNode) REQUIRE node.identity IS UNIQUE"
)
_NODE_FULLTEXT_INDEX: Final[str] = (
    "CREATE FULLTEXT INDEX world_memory_node_content_fulltext IF NOT EXISTS "
    "FOR (node:WorldMemoryNode) ON EACH [node.content]"
)


class _Record(Protocol):
    def __getitem__(self, key: str) -> str: ...


class _ResultSummary(Protocol): ...


@runtime_checkable
class _Result(Protocol):
    async def single(self) -> _Record | None: ...

    async def consume(self) -> _ResultSummary: ...


@runtime_checkable
class _ManagedTransaction(Protocol):
    async def run(self, query: str | Query, **parameters: JsonValue) -> _Result: ...


@runtime_checkable
class _Session(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def run(self, query: str | Query, **parameters: JsonValue) -> _Result: ...

    async def execute_write(
        self,
        callback: Callable[
            [_ManagedTransaction, Neo4jCommand, str, str, Mapping[str, JsonValue]],
            Awaitable[None],
        ],
        command: Neo4jCommand,
        command_json: str,
        fingerprint: str,
        command_data: Mapping[str, JsonValue],
    ) -> None: ...


@runtime_checkable
class _Driver(Protocol):
    def session(self, **config: str) -> _Session: ...

    async def close(self) -> None: ...


def _require_driver(driver: object) -> _Driver:
    if not isinstance(driver, _Driver):
        raise TypeError("driver must provide an async Neo4j driver interface")
    return driver


class WorldMemoryNeo4jWriter:
    """Write world-memory graph commands with receipt-based idempotency."""

    @overload
    def __init__(
        self,
        driver: AsyncDriver,
        *,
        database: str = "neo4j",
        owns_driver: bool = False,
    ) -> None: ...

    @overload
    def __init__(
        self,
        driver: _Driver,
        *,
        database: str = "neo4j",
        owns_driver: bool = False,
    ) -> None: ...

    def __init__(
        self,
        driver: object,
        *,
        database: str = "neo4j",
        owns_driver: bool = False,
    ) -> None:
        self._driver: _Driver = _require_driver(driver)
        self._database: str = database
        self._owns_driver: bool = owns_driver

    async def ensure_schema(self) -> None:
        """Create the two uniqueness constraints required by the writer."""
        async with self._driver.session(database=self._database) as session:
            for statement in (_RECEIPT_CONSTRAINT, _NODE_CONSTRAINT, _NODE_FULLTEXT_INDEX):
                result = await session.run(statement)
                _ = await result.consume()

    async def __call__(self, command: Neo4jCommand) -> None:
        command_json, fingerprint, command_data = encode_command(command)
        self._validate_command_identifiers(command)

        async with self._driver.session(database=self._database) as session:
            await session.execute_write(
                self._write_transaction,
                command,
                command_json,
                fingerprint,
                command_data,
            )

    @staticmethod
    def _validate_command_identifiers(command: Neo4jCommand) -> None:
        payload = command.payload
        for node in (payload.primary_node, *payload.related_nodes):
            for label in node.labels:
                validate_identifier(label)
        for relationship in payload.relationships:
            validate_identifier(relationship.rel_type)

    async def _write_transaction(
        self,
        tx: _ManagedTransaction,
        command: Neo4jCommand,
        command_json: str,
        fingerprint: str,
        command_data: Mapping[str, JsonValue],
    ) -> None:
        receipt_result = await tx.run(
            """
            MERGE (receipt:WorldMemoryProjectionReceipt {
                idempotency_key: $idempotency_key
            })
            ON CREATE SET receipt.fingerprint = $fingerprint,
                          receipt.command_json = $command_json,
                          receipt.metadata_json = $metadata_json
            RETURN receipt.fingerprint AS fingerprint
            """,
            idempotency_key=command.metadata.idempotency_key,
            fingerprint=fingerprint,
            command_json=command_json,
            metadata_json=self._metadata_json(command_data),
        )
        record = await receipt_result.single()
        if record is None or record["fingerprint"] != fingerprint:
            raise PermanentProjectionError(
                "neo4j projection: idempotency key payload conflict", target="neo4j"
            )

        nodes = self._all_nodes(command)
        for labels, identity, properties in nodes:
            await self._merge_node(tx, labels, identity, properties)
        for relationship in command.payload.relationships:
            await self._merge_relationship(
                tx,
                relationship.rel_type,
                relationship.from_identity,
                relationship.to_identity,
                relationship.properties,
            )

    @staticmethod
    def _metadata_json(command_data: Mapping[str, JsonValue]) -> str:
        metadata = command_data["metadata"]
        if not isinstance(metadata, dict):
            raise PermanentProjectionError(
                "neo4j projection: encoded metadata is malformed", target="neo4j"
            )
        return canonical_json(metadata)

    @staticmethod
    def _all_nodes(
        command: Neo4jCommand,
    ) -> tuple[tuple[tuple[str, ...], str, Mapping[str, JsonValue]], ...]:
        payload = command.payload
        known: dict[str, tuple[tuple[str, ...], Mapping[str, JsonValue]]] = {
            payload.primary_node.identity: (
                payload.primary_node.labels,
                payload.primary_node.properties,
            )
        }
        for node in payload.related_nodes:
            known[node.identity] = (node.labels, node.properties)
        for relationship in payload.relationships:
            _ = known.setdefault(relationship.from_identity, ((), {}))
            _ = known.setdefault(relationship.to_identity, ((), {}))
        return tuple(
            (labels, identity, properties) for identity, (labels, properties) in known.items()
        )

    async def _merge_node(
        self,
        tx: _ManagedTransaction,
        labels: Sequence[str],
        identity: str,
        properties: Mapping[str, JsonValue],
    ) -> None:
        label_clauses = "".join(f"\nSET node:{label}" for label in labels)
        query = f"""
            MERGE (node:WorldMemoryNode {{identity: $identity}})
            SET node += $properties,
                node.identity = $identity,
                node.properties_json = $properties_json{label_clauses}
            """
        result = await tx.run(
            query,
            identity=identity,
            properties=materialize_properties(properties),
            properties_json=self._properties_json(properties),
        )
        _ = await result.consume()

    async def _merge_relationship(
        self,
        tx: _ManagedTransaction,
        rel_type: str,
        from_identity: str,
        to_identity: str,
        properties: Mapping[str, JsonValue],
    ) -> None:
        query = f"""
            MERGE (source:WorldMemoryNode {{identity: $from_identity}})
            MERGE (target:WorldMemoryNode {{identity: $to_identity}})
            MERGE (source)-[relationship:{rel_type}]->(target)
            SET relationship += $properties,
                relationship.properties_json = $properties_json
            """
        result = await tx.run(
            query,
            from_identity=from_identity,
            to_identity=to_identity,
            properties=materialize_properties(properties),
            properties_json=self._properties_json(properties),
        )
        _ = await result.consume()

    @staticmethod
    def _properties_json(properties: Mapping[str, JsonValue]) -> str:
        return canonical_json(properties)

    async def aclose(self) -> None:
        """Close the driver only when ownership was explicitly transferred."""
        if self._owns_driver:
            await self._driver.close()


__all__ = ["WorldMemoryNeo4jWriter"]
