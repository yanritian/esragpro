from collections.abc import Mapping
from typing import Literal, Protocol, TypedDict
from uuid import UUID

import pytest
from elastic_transport import ApiResponseMeta, HttpHeaders, NodeConfig
from elasticsearch import ConflictError

from app.memory_core.projection_worker.contracts import PermanentProjectionError
from app.memory_core.world_memory_projection._commands import (
    ElasticsearchCommand,
    ProjectionMetadata,
)
from app.memory_core.world_memory_projection._elasticsearch_writer import (
    WorldMemoryElasticsearchWriter,
)
from app.memory_core.world_memory_projection._write_codec import (
    ElasticsearchDocument,
    JsonValue,
    ProjectionMetadataDocument,
    encode_elasticsearch_document,
)
from app.memory_core.world_memory_projection.payloads import ElasticsearchPayload


class ProjectionMetadataMapping(TypedDict):
    properties: dict[str, dict[str, str]]


class FieldMapping(TypedDict):
    type: str


class DynamicObjectMapping(TypedDict):
    type: Literal["object"]
    dynamic: Literal[True]


class IndexMappings(TypedDict):
    _meta: dict[str, int | str]
    dynamic: Literal["strict"]
    properties: dict[str, FieldMapping | DynamicObjectMapping | ProjectionMetadataMapping]


class IndexCall(TypedDict):
    index: str
    id: str
    document: ElasticsearchDocument
    op_type: Literal["create"]


class IndicesClient(Protocol):
    async def exists(self, *, index: str) -> bool: ...
    async def create(self, *, index: str, mappings: IndexMappings) -> Mapping[str, JsonValue]: ...
    async def get_mapping(self, *, index: str) -> Mapping[str, JsonValue]: ...


class ElasticsearchClient(Protocol):
    indices: IndicesClient

    async def index(
        self,
        *,
        index: str,
        id: str,
        document: ElasticsearchDocument,
        op_type: Literal["create"],
    ) -> Mapping[str, JsonValue]: ...

    async def aclose(self) -> None: ...


class FakeIndices:
    def __init__(
        self,
        exists: bool = False,
        mapping: Mapping[str, JsonValue] | None = None,
    ) -> None:
        self.exists_value: bool = exists
        self.create_calls: list[tuple[str, IndexMappings]] = []
        self.get_mapping_calls: list[str] = []
        self.mapping_response: Mapping[str, JsonValue] = (
            mapping
            if mapping is not None
            else {"world-memory": {"mappings": {"_meta": {"mapping_version": 1}}}}
        )

    async def exists(self, *, index: str) -> bool:
        assert index == "world-memory"
        return self.exists_value

    async def create(self, *, index: str, mappings: IndexMappings) -> Mapping[str, JsonValue]:
        self.create_calls.append((index, mappings))
        return {"acknowledged": True}

    async def get_mapping(self, *, index: str) -> Mapping[str, JsonValue]:
        assert index == "world-memory"
        self.get_mapping_calls.append(index)
        return self.mapping_response


class FakeClient:
    def __init__(
        self,
        *,
        exists: bool = False,
        mapping: Mapping[str, JsonValue] | None = None,
    ) -> None:
        self.indices: FakeIndices = FakeIndices(exists, mapping)
        self.index_calls: list[IndexCall] = []
        self.index_result: dict[str, JsonValue] = {"result": "created"}
        self.index_error: BaseException | None = None
        self.closed: bool = False
        self.get_source: dict[str, JsonValue] = {}

    async def index(
        self,
        *,
        index: str,
        id: str,
        document: ElasticsearchDocument,
        op_type: Literal["create"],
    ) -> Mapping[str, JsonValue]:
        self.index_calls.append(
            {"index": index, "id": id, "document": document, "op_type": op_type}
        )
        if self.index_error is not None:
            raise self.index_error
        return self.index_result

    async def aclose(self) -> None:
        self.closed = True

    async def get(self, *, index: str, id: str) -> Mapping[str, JsonValue]:
        assert index == "world-memory"
        assert id == "idem-1"
        return {"_source": self.get_source}

    async def close(self) -> None:
        self.closed = True


def _command(*, key: str = "idem-1", content: str = "stable") -> ElasticsearchCommand:
    payload = ElasticsearchPayload(
        id="object-1",
        object_kind="observation",
        content=content,
        evidence_ids=("e-1",),
        timestamps={"observed_at": "2026-01-01T00:00:00+00:00"},
        validity=None,
        supersedes=(),
        contradicts=(),
        version=1,
        fusion_version=None,
        projection_version=1,
        status="active",
        metadata={"source": "sensor", "nested": {"priority": 1}},
        source_envelope={"world_memory_snapshot": {"id": "object-1"}},
    )
    metadata = ProjectionMetadata(
        outbox_id=UUID("00000000-0000-0000-0000-000000000002"),
        aggregate_id=UUID("00000000-0000-0000-0000-000000000001"),
        event_type="world.observation.ingested",
        schema_version=1,
        projection_target="elasticsearch",
        idempotency_key=key,
    )
    return ElasticsearchCommand(metadata=metadata, payload=payload)


def _writer(client: FakeClient, *, owns_client: bool = False) -> WorldMemoryElasticsearchWriter:
    return WorldMemoryElasticsearchWriter(
        client, index_name="world-memory", owns_client=owns_client
    )


def _json_projection_metadata(value: ProjectionMetadataDocument) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    result["outbox_id"] = value["outbox_id"]
    result["aggregate_id"] = value["aggregate_id"]
    result["event_type"] = value["event_type"]
    result["schema_version"] = value["schema_version"]
    result["projection_target"] = value["projection_target"]
    result["idempotency_key"] = value["idempotency_key"]
    return result


def _source(document: ElasticsearchDocument) -> dict[str, JsonValue]:
    return {
        "id": document["id"],
        "object_kind": document["object_kind"],
        "content": document["content"],
        "evidence_ids": list[JsonValue](document["evidence_ids"]),
        "timestamps": document["timestamps"],
        "validity": document["validity"],
        "supersedes": list[JsonValue](document["supersedes"]),
        "contradicts": list[JsonValue](document["contradicts"]),
        "version": document["version"],
        "fusion_version": document["fusion_version"],
        "projection_version": document["projection_version"],
        "status": document["status"],
        "metadata": document["metadata"],
        "source_envelope": document["source_envelope"],
        "projection_metadata": _json_projection_metadata(document["projection_metadata"]),
        "payload_fingerprint": document["payload_fingerprint"],
    }


class FakeConflictError(ConflictError):
    def __init__(self, document: ElasticsearchDocument) -> None:
        self.body: dict[str, JsonValue] = {
            "error": {
                "reason": "idempotency_key conflict",
                "existing": document["payload_fingerprint"],
            }
        }
        meta = ApiResponseMeta(
            status=409,
            http_version="1.1",
            headers=HttpHeaders(),
            duration=0.0,
            node=NodeConfig(scheme="http", host="localhost", port=9200),
        )
        super().__init__("conflict", meta, self.body)


def _conflict(document: ElasticsearchDocument) -> FakeConflictError:
    return FakeConflictError(document)


@pytest.mark.asyncio
async def test_ensure_index_creates_mapping_and_tolerates_existing() -> None:
    missing = FakeClient()
    await _writer(missing).ensure_index()
    assert missing.indices.create_calls[0][0] == "world-memory"
    assert missing.indices.create_calls[0][1]["properties"]

    existing = FakeClient(
        exists=True,
        mapping={"world-memory": {"mappings": {"_meta": {"mapping_version": 1}}}},
    )
    await _writer(existing).ensure_index()
    assert existing.indices.create_calls == []
    assert existing.indices.get_mapping_calls == ["world-memory"]

    malformed = FakeClient(exists=True, mapping={"world-memory": {"mappings": {}}})
    with pytest.raises(PermanentProjectionError) as malformed_error:
        await _writer(malformed).ensure_index()
    assert malformed_error.value.target == "elasticsearch"

    incompatible = FakeClient(
        exists=True,
        mapping={"world-memory": {"mappings": {"_meta": {"mapping_version": 2}}}},
    )
    with pytest.raises(PermanentProjectionError) as incompatible_error:
        await _writer(incompatible).ensure_index()
    assert incompatible_error.value.target == "elasticsearch"


@pytest.mark.asyncio
async def test_first_create_uses_idempotency_key_and_create_operation() -> None:
    client = FakeClient()
    await _writer(client)(_command())
    assert client.index_calls[0]["id"] == "idem-1"
    assert client.index_calls[0]["op_type"] == "create"


@pytest.mark.asyncio
async def test_equal_fingerprint_conflict_is_idempotent() -> None:
    client = FakeClient()
    document = encode_elasticsearch_document(_command())
    client.index_error = _conflict(document)
    client.get_source = _source(document)
    assert await _writer(client)(_command()) is None


@pytest.mark.asyncio
async def test_different_fingerprint_conflict_is_permanent_for_elasticsearch() -> None:
    client = FakeClient()
    other_document = encode_elasticsearch_document(_command(content="other"))
    client.index_error = _conflict(other_document)
    client.get_source = _source(other_document)
    with pytest.raises(PermanentProjectionError) as error:
        await _writer(client)(_command())
    assert error.value.target == "elasticsearch"


@pytest.mark.asyncio
async def test_two_keys_create_two_history_documents() -> None:
    client = FakeClient()
    writer = _writer(client)
    await writer(_command(key="idem-1"))
    await writer(_command(key="idem-2"))
    assert [call["id"] for call in client.index_calls] == ["idem-1", "idem-2"]
    assert len(client.index_calls) == 2


@pytest.mark.asyncio
async def test_aclose_only_closes_owned_client() -> None:
    borrowed = FakeClient()
    await _writer(borrowed).aclose()
    assert not borrowed.closed
    owned = FakeClient()
    await _writer(owned, owns_client=True).aclose()
    assert owned.closed
