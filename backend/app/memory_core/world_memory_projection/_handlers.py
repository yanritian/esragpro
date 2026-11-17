"""World-memory handler boundary for ES and Neo4j targets (P6-E).

Each handler is a callable satisfying :class:`ProjectionHandler`. The
handler enforces the expected projection target, converts the event
through the accepted P6-D adapter, wraps the result in a typed
command, and delegates to the injected narrow writer.

Failure classification:

- **Target mismatch** — the event's ``projection_target`` does not
  equal the handler's expected target. Translated to
  :class:`PermanentProjectionError`.
- **Adapter failure** — :class:`WorldMemoryProjectionAdapterError`
  (malformed / unsupported snapshot). Translated to
  :class:`PermanentProjectionError`.
- **Writer / infrastructure failure** — any other exception raised by
  the writer propagates unchanged so the P6-C worker can retry.

The handler never mutates the event or the adapted payload.
"""
from __future__ import annotations

from datetime import datetime
from typing import Final

from ..projection_worker.contracts import (
    PermanentProjectionError,
    ProjectionEvent,
)
from ._commands import (
    ElasticsearchCommand,
    Neo4jCommand,
    metadata_from_event,
)
from ._neo4j_adapter import to_neo4j_payload
from ._writer_protocols import ElasticsearchWriter, Neo4jWriter
from .adapters import to_elasticsearch_payload
from .errors import WorldMemoryProjectionAdapterError

_TARGET_MISMATCH_TEMPLATE = (
    "world-memory {target} handler received event for target {actual!r}"
)
_ELASTICSEARCH_TARGET: Final[str] = "elasticsearch"
_NEO4J_TARGET: Final[str] = "neo4j"


def _event_occurred_at(event: ProjectionEvent) -> datetime | None:
    raw_value = event.payload.get("occurred_at")
    match raw_value:
        case None:
            return None
        case str() as raw_time:
            normalized = raw_time[:-1] + "+00:00" if raw_time.endswith("Z") else raw_time
            try:
                occurred_at = datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise PermanentProjectionError(
                    "world-memory event history: occurred_at is malformed",
                    target=_ELASTICSEARCH_TARGET,
                ) from exc
            if occurred_at.tzinfo is None:
                raise PermanentProjectionError(
                    "world-memory event history: occurred_at must include a timezone",
                    target=_ELASTICSEARCH_TARGET,
                )
            return occurred_at
        case _:
            raise PermanentProjectionError(
                "world-memory event history: occurred_at must be an ISO-8601 string",
                target=_ELASTICSEARCH_TARGET,
            )


class WorldMemoryElasticsearchHandler:
    """Callable handler for the world-memory Elasticsearch target.

    Constructed with an :class:`ElasticsearchWriter`.
    """

    def __init__(
        self,
        writer: ElasticsearchWriter,
    ) -> None:
        self._writer = writer

    @property
    def target(self) -> str:
        return _ELASTICSEARCH_TARGET

    async def __call__(self, event: ProjectionEvent) -> None:
        if event.projection_target != _ELASTICSEARCH_TARGET:
            raise PermanentProjectionError(
                _TARGET_MISMATCH_TEMPLATE.format(
                    target=_ELASTICSEARCH_TARGET,
                    actual=event.projection_target,
                ),
                target=_ELASTICSEARCH_TARGET,
            )

        try:
            payload = to_elasticsearch_payload(event)
        except WorldMemoryProjectionAdapterError as exc:
            raise PermanentProjectionError(
                f"elasticsearch adapter failure: {exc}",
                target=_ELASTICSEARCH_TARGET,
            ) from exc

        command = ElasticsearchCommand(
            metadata=metadata_from_event(event),
            payload=payload,
            occurred_at=_event_occurred_at(event),
        )
        await self._writer(command)


class WorldMemoryNeo4jHandler:
    """Callable handler for the world-memory Neo4j target.

    Symmetric to :class:`WorldMemoryElasticsearchHandler` for Neo4j.
    """

    def __init__(
        self,
        writer: Neo4jWriter,
    ) -> None:
        self._writer = writer

    @property
    def target(self) -> str:
        return _NEO4J_TARGET

    async def __call__(self, event: ProjectionEvent) -> None:
        if event.projection_target != _NEO4J_TARGET:
            raise PermanentProjectionError(
                _TARGET_MISMATCH_TEMPLATE.format(
                    target=_NEO4J_TARGET,
                    actual=event.projection_target,
                ),
                target=_NEO4J_TARGET,
            )

        try:
            payload = to_neo4j_payload(event)
        except WorldMemoryProjectionAdapterError as exc:
            raise PermanentProjectionError(
                f"neo4j adapter failure: {exc}",
                target=_NEO4J_TARGET,
            ) from exc

        command = Neo4jCommand(
            metadata=metadata_from_event(event),
            payload=payload,
        )
        await self._writer(command)


__all__ = [
    "WorldMemoryElasticsearchHandler",
    "WorldMemoryNeo4jHandler",
]
