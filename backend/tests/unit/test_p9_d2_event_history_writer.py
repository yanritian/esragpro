from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal, TypedDict, final
from uuid import UUID

import pytest
from elastic_transport import ApiResponseMeta, HttpHeaders, NodeConfig
from elasticsearch.exceptions import ConflictError

from app.memory_core.projection_worker.contracts import PermanentProjectionError
from app.memory_core.world_memory_projection._commands import (
    ElasticsearchCommand,
    ProjectionMetadata,
)
from app.memory_core.world_memory_projection._event_history_elasticsearch_writer import (
    WorldMemoryElasticsearchEventFanoutWriter,
    WorldMemoryEventHistoryElasticsearchWriter,
    encode_event_history_document,
)
from app.memory_core.world_memory_projection._write_codec import JsonValue
from app.memory_core.world_memory_projection.payloads import ElasticsearchPayload

_DEFAULT_OCCURRED_AT = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
_DEFAULT_AUTHORITY_ENVELOPE_ID = UUID("00000000-0000-0000-0000-000000000003")


class _IndexCall(TypedDict):
    index: str
    id: str
    document: Mapping[str, JsonValue]
    op_type: Literal["create"]


@final
class _FakeIndices:
    def __init__(
        self,
        *,
        exists: bool = False,
        mapping_version: int = 2,
        properties: Mapping[str, JsonValue] | None = None,
    ) -> None:
        self.create_calls: list[tuple[str, Mapping[str, JsonValue]]] = []
        self.put_mapping_calls: list[tuple[str, Mapping[str, JsonValue]]] = []
        self._exists: bool = exists
        self._mapping_version: int = mapping_version
        self._properties: Mapping[str, JsonValue] = (
            properties if properties is not None else {}
        )

    async def exists(self, *, index: str) -> bool:
        del index
        return self._exists

    async def create(
        self,
        *,
        index: str,
        mappings: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        self.create_calls.append((index, mappings))
        return {"acknowledged": True}

    async def get_mapping(self, *, index: str) -> dict[str, JsonValue]:
        return {
            index: {
                "mappings": {
                    "_meta": {"mapping_version": self._mapping_version},
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
    def __init__(
        self,
        *,
        index_exists: bool = False,
        mapping_version: int = 2,
        properties: Mapping[str, JsonValue] | None = None,
    ) -> None:
        self.indices = _FakeIndices(
            exists=index_exists,
            mapping_version=mapping_version,
            properties=properties,
        )
        self.index_calls: list[_IndexCall] = []
        self.index_error: ConflictError | None = None
        self.existing_document: dict[str, JsonValue] = {}
        self.update_calls: list[dict[str, JsonValue]] = []

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
        if self.index_error is not None:
            raise self.index_error
        return {"result": "created"}

    async def get(self, *, index: str, id: str) -> Mapping[str, JsonValue]:
        del index, id
        return {"_source": self.existing_document}

    async def update(
        self,
        *,
        index: str,
        id: str,
        doc: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        self.update_calls.append({"index": index, "id": id, "doc": dict(doc)})
        self.existing_document.update(doc)
        return {"result": "updated"}

    async def close(self) -> None:
        return None


@final
class _RecordingWriter:
    def __init__(self, events: list[str], name: str, fail_once: bool = False) -> None:
        self._events = events
        self._name = name
        self._fail_once = fail_once

    async def __call__(self, command: ElasticsearchCommand) -> None:
        self._events.append(self._name)
        if self._fail_once:
            self._fail_once = False
            raise RuntimeError("temporary event history failure")


def _command(
    *,
    occurred_at: datetime | None = _DEFAULT_OCCURRED_AT,
    authority_envelope_id: UUID | None = _DEFAULT_AUTHORITY_ENVELOPE_ID,
) -> ElasticsearchCommand:
    payload = ElasticsearchPayload(
        id="00000000-0000-0000-0000-000000000099",
        object_kind="observation",
        content="historic sensor reading",
        evidence_ids=("evidence-1",),
        timestamps={"observed_at": "2026-08-02T12:00:00+00:00"},
        validity=None,
        supersedes=(),
        contradicts=(),
        version=1,
        fusion_version=None,
        projection_version=1,
        status="active",
        metadata={"source": "test"},
        source_envelope={
            "world_memory_snapshot": {
                "id": "object-1",
                "evidence_ids": ["evidence-1"],
            }
        },
    )
    return ElasticsearchCommand(
        metadata=ProjectionMetadata(
            outbox_id=UUID("00000000-0000-0000-0000-000000000001"),
            authority_envelope_id=authority_envelope_id,
            aggregate_id=UUID("00000000-0000-0000-0000-000000000002"),
            event_type="world.observation.ingested",
            schema_version=1,
            projection_target="elasticsearch",
            idempotency_key="event-history-idempotency",
        ),
        payload=payload,
        occurred_at=occurred_at,
    )


def _conflict(_document: Mapping[str, JsonValue]) -> ConflictError:
    meta = ApiResponseMeta(
        status=409,
        http_version="1.1",
        headers=HttpHeaders(),
        duration=0.0,
        node=NodeConfig(scheme="http", host="localhost", port=9200),
    )
    return ConflictError("conflict", meta, {"error": {"reason": "conflict"}})


@pytest.mark.asyncio
async def test_event_history_writer_creates_immutable_envelope_document() -> None:
    client = _FakeClient()
    writer = WorldMemoryEventHistoryElasticsearchWriter(client)
    command = _command()

    await writer.ensure_index()
    await writer(command)

    index_name, mapping = client.indices.create_calls[0]
    assert index_name == "world-memory-events-v2"
    assert mapping["_meta"] == {"mapping_version": 2, "purpose": "world_memory_event_history"}
    assert mapping["properties"]["authority_envelope_id"] == {"type": "keyword"}
    assert mapping["properties"]["has_evidence"] == {"type": "boolean"}
    call = client.index_calls[0]
    assert call["id"] == str(command.metadata.authority_envelope_id)
    assert call["op_type"] == "create"
    assert call["document"]["envelope_id"] == str(command.metadata.authority_envelope_id)
    assert call["document"]["authority_envelope_id"] == str(command.metadata.authority_envelope_id)
    assert call["document"]["has_evidence"] is True
    assert call["document"]["envelope_id"] != str(command.metadata.outbox_id)
    assert call["document"]["object_id"] == command.payload.id
    assert call["document"]["aggregate_id"] == str(command.metadata.aggregate_id)
    assert call["document"]["occurred_at"] == "2026-08-02T12:00:00+00:00"


@pytest.mark.asyncio
async def test_event_history_writer_rejects_an_incompatible_existing_mapping() -> None:
    client = _FakeClient(index_exists=True, mapping_version=1)

    with pytest.raises(PermanentProjectionError, match="mapping version is incompatible"):
        await WorldMemoryEventHistoryElasticsearchWriter(client).ensure_index()
    assert client.indices.create_calls == []


@pytest.mark.asyncio
async def test_event_history_writer_replays_only_an_identical_envelope() -> None:
    client = _FakeClient()
    writer = WorldMemoryEventHistoryElasticsearchWriter(client)
    command = _command()
    document = encode_event_history_document(command)
    client.index_error = _conflict(document)
    client.existing_document = document

    await writer(command)

    client.existing_document = encode_event_history_document(
        _command(occurred_at=datetime(2026, 8, 2, 12, 1, tzinfo=timezone.utc))
    )
    with pytest.raises(PermanentProjectionError):
        await writer(command)


@pytest.mark.asyncio
async def test_event_history_writer_enriches_a_matching_legacy_document_with_evidence_state(
) -> None:
    client = _FakeClient()
    writer = WorldMemoryEventHistoryElasticsearchWriter(client)
    command = _command()
    client.index_error = _conflict(encode_event_history_document(command))
    client.existing_document = dict(encode_event_history_document(command))
    del client.existing_document["has_evidence"]

    await writer(command)

    assert client.update_calls == [
        {
            "index": "world-memory-events-v2",
            "id": str(command.metadata.authority_envelope_id),
            "doc": {"has_evidence": True},
        }
    ]


@pytest.mark.asyncio
async def test_event_history_writer_rejects_a_missing_event_time() -> None:
    client = _FakeClient()
    writer = WorldMemoryEventHistoryElasticsearchWriter(client)

    with pytest.raises(PermanentProjectionError):
        await writer(_command(occurred_at=None))
    assert client.index_calls == []


@pytest.mark.asyncio
async def test_fanout_retries_history_after_current_writer_succeeds() -> None:
    events: list[str] = []
    writer = WorldMemoryElasticsearchEventFanoutWriter(
        _RecordingWriter(events, "v1"),
        _RecordingWriter(events, "v2", fail_once=True),
    )

    with pytest.raises(RuntimeError):
        await writer(_command())
    await writer(_command())

    assert events == ["v1", "v2", "v1", "v2"]
