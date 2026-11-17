"""Narrow async writer protocols for the world-memory handler boundary (P6-E).

Each protocol accepts exactly one typed command. Concrete writers
(Elasticsearch / Neo4j) are intentionally *not* implemented here —
this slice defines only the contract. The handlers invoke these
protocols; downstream slices supply the concrete writers.

Protocols are structural (``Protocol`` + ``runtime_checkable``) so
callables of matching shape — not just subclasses — satisfy them.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ._commands import ElasticsearchCommand, Neo4jCommand


@runtime_checkable
class ElasticsearchWriter(Protocol):
    """Async writer for :class:`ElasticsearchCommand`.

    Implementations perform the actual ES upsert. They must be
    idempotent with respect to ``command.metadata.idempotency_key``.
    Exceptions propagate to the caller — the handler forwards them
    unchanged so the worker can retry.
    """

    async def __call__(self, command: ElasticsearchCommand) -> None: ...


@runtime_checkable
class Neo4jWriter(Protocol):
    """Async writer for :class:`Neo4jCommand`.

    Implementations perform the actual Neo4j transaction. They must
    be idempotent with respect to ``command.metadata.idempotency_key``.
    Exceptions propagate to the caller unchanged.
    """

    async def __call__(self, command: Neo4jCommand) -> None: ...


__all__ = ["ElasticsearchWriter", "Neo4jWriter"]
