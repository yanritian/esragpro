from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID, uuid4

import pytest
from elastic_transport import ObjectApiResponse
from elasticsearch import AsyncElasticsearch

from app.memory_core.projection_worker.contracts import PermanentProjectionError
from app.memory_core.world_memory_projection._commands import (
    ElasticsearchCommand,
    ProjectionMetadata,
)
from app.memory_core.world_memory_projection._elasticsearch_writer import (
    WorldMemoryElasticsearchWriter,
)
from app.memory_core.world_memory_projection._write_codec import JsonValue
from app.memory_core.world_memory_projection.payloads import ElasticsearchPayload


def _command(*, key: str, content: str = "stable observation") -> ElasticsearchCommand:
    object_id = "world-object-1"
    payload = ElasticsearchPayload(
        id=object_id,
        object_kind="observation",
        content=content,
        evidence_ids=("evidence-1",),
        timestamps={"observed_at": "2026-07-31T12:00:00+00:00"},
        validity=None,
        supersedes=(),
        contradicts=(),
        version=1,
        fusion_version=None,
        projection_version=1,
        status="active",
        metadata={"source": "live-p6-f"},
        source_envelope={
            "aggregate_id": str(UUID("00000000-0000-0000-0000-000000000001")),
            "event_type": "world.observation.ingested",
            "world_memory_snapshot": {
                "id": object_id,
                "content": content,
                "kind": "observation",
            },
        },
    )
    metadata = ProjectionMetadata(
        outbox_id=uuid4(),
        aggregate_id=UUID("00000000-0000-0000-0000-000000000001"),
        event_type="world.observation.ingested",
        schema_version=1,
        projection_target="elasticsearch",
        idempotency_key=key,
    )
    return ElasticsearchCommand(metadata=metadata, payload=payload)


async def _refresh_and_count(
    client: AsyncElasticsearch,
    index_name: str,
) -> int:
    _ = await client.indices.refresh(index=index_name)
    response = _response_body(await client.count(index=index_name))
    count = response["count"]
    assert isinstance(count, int)
    return count


def _response_body(response: ObjectApiResponse[JsonValue]) -> Mapping[str, JsonValue]:
    body = response.body
    assert isinstance(body, dict)
    return body


def _source(response: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    source = response["_source"]
    assert isinstance(source, dict)
    return source


@pytest.mark.asyncio
async def test_live_write_stores_idempotency_key_payload_and_fingerprint(
    elasticsearch_client: AsyncElasticsearch,
    world_memory_writer: WorldMemoryElasticsearchWriter,
    world_memory_index_name: str,
) -> None:
    command = _command(key=f"p6-f-idem-{uuid4().hex}")

    await world_memory_writer(command)
    index_name = world_memory_index_name
    assert await _refresh_and_count(elasticsearch_client, index_name) == 1
    stored = _response_body(
        await elasticsearch_client.get(
            index=index_name,
            id=command.metadata.idempotency_key,
        ),
    )

    assert stored["_id"] == command.metadata.idempotency_key
    source = _source(stored)
    assert source["id"] == command.payload.id
    assert source["object_kind"] == command.payload.object_kind
    assert source["content"] == command.payload.content
    assert source["evidence_ids"] == ["evidence-1"]
    projection_metadata = source["projection_metadata"]
    assert isinstance(projection_metadata, dict)
    assert projection_metadata["idempotency_key"] == command.metadata.idempotency_key
    assert source["source_envelope"] == command.payload.source_envelope
    assert isinstance(source["payload_fingerprint"], str)
    assert len(source["payload_fingerprint"]) == 64


@pytest.mark.asyncio
async def test_live_exact_replay_creates_no_duplicate(
    elasticsearch_client: AsyncElasticsearch,
    world_memory_writer: WorldMemoryElasticsearchWriter,
    world_memory_index_name: str,
) -> None:
    command = _command(key=f"p6-f-replay-{uuid4().hex}")

    await world_memory_writer(command)
    await world_memory_writer(command)

    assert (
        await _refresh_and_count(
            elasticsearch_client,
            world_memory_index_name,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_live_changed_payload_with_same_key_is_permanent_error(
    elasticsearch_client: AsyncElasticsearch,
    world_memory_writer: WorldMemoryElasticsearchWriter,
    world_memory_index_name: str,
) -> None:
    key = f"p6-f-conflict-{uuid4().hex}"
    original = _command(key=key)
    changed = _command(key=key, content="changed observation")

    await world_memory_writer(original)
    with pytest.raises(PermanentProjectionError) as error:
        await world_memory_writer(changed)

    assert error.value.target == "elasticsearch"
    stored = _response_body(
        await elasticsearch_client.get(
            index=world_memory_index_name,
            id=key,
        ),
    )
    assert _source(stored)["content"] == original.payload.content


@pytest.mark.asyncio
async def test_live_distinct_keys_preserve_two_history_documents(
    elasticsearch_client: AsyncElasticsearch,
    world_memory_writer: WorldMemoryElasticsearchWriter,
    world_memory_index_name: str,
) -> None:
    first = _command(key=f"p6-f-history-a-{uuid4().hex}")
    second = _command(key=f"p6-f-history-b-{uuid4().hex}")

    await world_memory_writer(first)
    await world_memory_writer(second)

    assert (
        await _refresh_and_count(
            elasticsearch_client,
            world_memory_index_name,
        )
        == 2
    )
    first_stored = _response_body(
        await elasticsearch_client.get(
            index=world_memory_index_name,
            id=first.metadata.idempotency_key,
        ),
    )
    second_stored = _response_body(
        await elasticsearch_client.get(
            index=world_memory_index_name,
            id=second.metadata.idempotency_key,
        ),
    )
    assert first_stored["_id"] == first.metadata.idempotency_key
    assert second_stored["_id"] == second.metadata.idempotency_key
    assert _source(first_stored)["id"] == _source(second_stored)["id"]
