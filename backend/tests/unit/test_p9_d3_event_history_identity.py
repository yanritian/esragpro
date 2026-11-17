from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal, TypedDict, final
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory_core.projection_worker.contracts import PermanentProjectionError
from app.memory_core.repositories.world_memory_projection_outbox import (
    StoredProjectionRow,
    WorldMemoryProjectionOutboxRepository,
)
from app.memory_core.world_memory_projection._commands import (
    ElasticsearchCommand,
    ProjectionMetadata,
    metadata_from_event,
)
from app.memory_core.world_memory_projection._event_history_elasticsearch_writer import (
    WorldMemoryEventHistoryElasticsearchWriter,
)
from app.memory_core.world_memory_projection._write_codec import JsonValue
from app.memory_core.world_memory_projection.payloads import ElasticsearchPayload

_OCCURRED_AT = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


class _IndexCall(TypedDict):
    index: str
    id: str
    document: Mapping[str, JsonValue]
    op_type: Literal["create"]


@final
class _FakeIndices:
    def __init__(self, properties: Mapping[str, JsonValue] | None = None) -> None:
        self.put_mapping_calls: list[tuple[str, Mapping[str, JsonValue]]] = []
        self._properties = properties if properties is not None else {}

    async def exists(self, *, index: str) -> bool:
        del index
        return True

    async def create(
        self,
        *,
        index: str,
        mappings: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        del index, mappings
        return {"acknowledged": True}

    async def get_mapping(self, *, index: str) -> Mapping[str, JsonValue]:
        return {
            index: {
                "mappings": {
                    "_meta": {"mapping_version": 2},
                    "properties": dict(self._properties),
                }
            }
        }

    async def put_mapping(
        self,
        *,
        index: str,
        properties: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        self.put_mapping_calls.append((index, properties))
        return {"acknowledged": True}


@final
class _FakeClient:
    def __init__(self, properties: Mapping[str, JsonValue] | None = None) -> None:
        self.indices = _FakeIndices(properties)
        self.index_calls: list[_IndexCall] = []

    async def index(
        self,
        *,
        index: str,
        id: str,
        document: Mapping[str, JsonValue],
        op_type: Literal["create"],
    ) -> Mapping[str, JsonValue]:
        self.index_calls.append(
            {"index": index, "id": id, "document": document, "op_type": op_type}
        )
        return {"result": "created"}

    async def get(self, *, index: str, id: str) -> Mapping[str, JsonValue]:
        del index, id
        return {"_source": {}}

    async def close(self) -> None:
        return None


def _projection_row() -> StoredProjectionRow:
    aggregate_id = UUID("00000000-0000-0000-0000-000000000033")
    return StoredProjectionRow(
        id=UUID("00000000-0000-0000-0000-000000000011"),
        authority_envelope_id=UUID("00000000-0000-0000-0000-000000000022"),
        event_type="world.observation.ingested",
        schema_version=1,
        aggregate_id=aggregate_id,
        projection_target="elasticsearch",
        idempotency_key="world-projection:authority:v1:elasticsearch",
        envelope={
            "event_type": "world.observation.ingested",
            "schema_version": 1,
            "aggregate_type": "world_memory",
            "aggregate_id": str(aggregate_id),
            "projection_target": "elasticsearch",
            "idempotency_key": "world-projection:authority:v1:elasticsearch",
            "occurred_at": "2026-08-02T12:00:00+00:00",
            "world_memory_snapshot": {
                "_kind": "observation",
                "id": "00000000-0000-0000-0000-000000000099",
                "content": "historic sensor reading",
                "evidence_ids": ["evidence-1"],
            },
        },
        state="pending",
        attempt_count=0,
        max_attempts=8,
        available_at=_OCCURRED_AT,
        lease_owner=None,
        lease_expires_at=None,
        processed_at=None,
        last_error=None,
        created_at=_OCCURRED_AT,
        updated_at=_OCCURRED_AT,
    )


def _payload() -> ElasticsearchPayload:
    return ElasticsearchPayload(
        id="00000000-0000-0000-0000-000000000099",
        object_kind="observation",
        content="historic sensor reading",
        evidence_ids=("evidence-1",),
        timestamps={},
        validity=None,
        supersedes=(),
        contradicts=(),
        version=1,
        fusion_version=None,
        projection_version=None,
        status="active",
        metadata={},
        source_envelope={"world_memory_snapshot": {"id": "object-1"}},
    )


@pytest.mark.asyncio
async def test_outbox_mapping_preserves_authority_identity_for_event_history() -> None:
    row = _projection_row()
    async with AsyncSession() as session:
        event = WorldMemoryProjectionOutboxRepository(session).map_to_projection_event(row)

    metadata = metadata_from_event(event)
    command = ElasticsearchCommand(metadata=metadata, payload=_payload(), occurred_at=_OCCURRED_AT)
    client = _FakeClient()

    await WorldMemoryEventHistoryElasticsearchWriter(client)(command)

    call = client.index_calls[0]
    assert event.outbox_id == row.id
    assert event.authority_envelope_id == row.authority_envelope_id
    assert call["id"] == str(row.authority_envelope_id)
    assert call["document"]["envelope_id"] == str(row.authority_envelope_id)
    assert call["document"]["authority_envelope_id"] == str(row.authority_envelope_id)
    assert call["document"]["envelope_id"] != str(row.id)


@pytest.mark.asyncio
async def test_event_history_writer_requires_an_authority_marker() -> None:
    metadata = ProjectionMetadata(
        outbox_id=UUID("00000000-0000-0000-0000-000000000011"),
        aggregate_id=UUID("00000000-0000-0000-0000-000000000033"),
        event_type="world.observation.ingested",
        schema_version=1,
        projection_target="elasticsearch",
        idempotency_key="world-projection:authority:v1:elasticsearch",
    )
    command = ElasticsearchCommand(metadata=metadata, payload=_payload(), occurred_at=_OCCURRED_AT)
    client = _FakeClient()

    with pytest.raises(PermanentProjectionError, match="authority_envelope_id"):
        await WorldMemoryEventHistoryElasticsearchWriter(client)(command)
    assert client.index_calls == []


@pytest.mark.asyncio
async def test_existing_v2_mapping_adds_the_authority_marker_once() -> None:
    missing_marker = _FakeClient()
    existing_marker = _FakeClient(
        properties={
            "authority_envelope_id": {"type": "keyword"},
            "has_evidence": {"type": "boolean"},
        }
    )

    await WorldMemoryEventHistoryElasticsearchWriter(missing_marker).ensure_index()
    await WorldMemoryEventHistoryElasticsearchWriter(existing_marker).ensure_index()

    assert missing_marker.indices.put_mapping_calls == [
        (
            "world-memory-events-v2",
            {
                "authority_envelope_id": {"type": "keyword"},
                "has_evidence": {"type": "boolean"},
            },
        )
    ]
    assert existing_marker.indices.put_mapping_calls == []
