from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol, TypedDict, runtime_checkable

from elastic_transport import ObjectApiResponse
from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import BadRequestError, ConflictError

from ..projection_worker.contracts import PermanentProjectionError
from ._commands import ElasticsearchCommand
from ._write_codec import ElasticsearchDocument, JsonValue, encode_elasticsearch_document

_DEFAULT_INDEX_NAME = "esragpro-world-memory-v1"
_MAPPING_VERSION = 1
_OpType = Literal["create"]


class _FieldMapping(TypedDict):
    type: str


class _DynamicObjectMapping(TypedDict):
    type: Literal["object"]
    dynamic: Literal[True]


class _ProjectionMetadataMapping(TypedDict):
    properties: dict[str, dict[str, str]]


class _IndexMappings(TypedDict):
    _meta: dict[str, int | str]
    dynamic: Literal["strict"]
    properties: dict[str, _FieldMapping | _DynamicObjectMapping | _ProjectionMetadataMapping]


@runtime_checkable
class _IndicesClient(Protocol):
    async def exists(self, *, index: str) -> bool: ...

    async def create(self, *, index: str, mappings: _IndexMappings) -> Mapping[str, JsonValue]: ...

    async def get_mapping(self, *, index: str) -> Mapping[str, JsonValue]: ...


class _FakeElasticsearchClient(Protocol):
    @property
    def indices(self) -> _IndicesClient: ...

    async def index(
        self,
        *,
        index: str,
        id: str,
        document: ElasticsearchDocument,
        op_type: _OpType,
    ) -> Mapping[str, JsonValue]: ...

    async def get(self, *, index: str, id: str) -> Mapping[str, JsonValue]: ...

    async def close(self) -> None: ...


_MAPPINGS: _IndexMappings = {
    "_meta": {"mapping_version": _MAPPING_VERSION, "purpose": "world_memory_projection"},
    "dynamic": "strict",
    "properties": {
        "id": {"type": "keyword"},
        "object_kind": {"type": "keyword"},
        "content": {"type": "text"},
        "evidence_ids": {"type": "keyword"},
        "timestamps": {"type": "object", "dynamic": True},
        "validity": {"type": "object", "dynamic": True},
        "supersedes": {"type": "keyword"},
        "contradicts": {"type": "keyword"},
        "version": {"type": "integer"},
        "fusion_version": {"type": "integer"},
        "projection_version": {"type": "integer"},
        "status": {"type": "keyword"},
        "metadata": {"type": "object", "dynamic": True},
        "source_envelope": {"type": "object", "dynamic": True},
        "projection_metadata": {
            "properties": {
                "outbox_id": {"type": "keyword"},
                "aggregate_id": {"type": "keyword"},
                "event_type": {"type": "keyword"},
                "schema_version": {"type": "integer"},
                "projection_target": {"type": "keyword"},
                "idempotency_key": {"type": "keyword"},
            },
        },
        "payload_fingerprint": {"type": "keyword"},
    },
}


def _is_already_exists(error: BadRequestError) -> bool:
    return "resource_already_exists_exception" in str(error)


def _response_body(
    response: Mapping[str, JsonValue] | ObjectApiResponse[dict[str, JsonValue]],
) -> Mapping[str, JsonValue]:
    if isinstance(response, ObjectApiResponse):
        return response.body
    return response


class WorldMemoryElasticsearchWriter:
    def __init__(
        self,
        client: AsyncElasticsearch | _FakeElasticsearchClient,
        *,
        index_name: str = _DEFAULT_INDEX_NAME,
        owns_client: bool = False,
    ) -> None:
        self._client: AsyncElasticsearch | _FakeElasticsearchClient = client
        self._index_name: str = index_name
        self._owns_client: bool = owns_client

    async def ensure_index(self) -> None:
        if await self._index_exists():
            if isinstance(self._client, AsyncElasticsearch) or hasattr(
                self._client.indices,
                "get_mapping",
            ):
                await self._verify_existing_mapping()
            return
        try:
            await self._create_index()
        except ConflictError:
            return
        except BadRequestError as exc:
            if _is_already_exists(exc):
                return
            raise

    async def __call__(self, command: ElasticsearchCommand) -> None:
        try:
            document = encode_elasticsearch_document(command)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise PermanentProjectionError(
                "elasticsearch projection: document serialization failed",
                target="elasticsearch",
            ) from exc

        try:
            await self._index_document(command.metadata.idempotency_key, document)
        except ConflictError as exc:
            existing = await self._get_document(command.metadata.idempotency_key)
            source = _response_body(existing).get("_source")
            if not isinstance(source, dict):
                raise PermanentProjectionError(
                    "elasticsearch projection: existing document is malformed",
                    target="elasticsearch",
                ) from exc
            fingerprint = source.get("payload_fingerprint")
            if not isinstance(fingerprint, str):
                raise PermanentProjectionError(
                    "elasticsearch projection: existing fingerprint is malformed",
                    target="elasticsearch",
                ) from exc
            if fingerprint == document["payload_fingerprint"]:
                return
            raise PermanentProjectionError(
                "elasticsearch projection: idempotency key payload conflict",
                target="elasticsearch",
            ) from exc

    async def aclose(self) -> None:
        if self._owns_client:
            if isinstance(self._client, AsyncElasticsearch):
                await self._client.close()
            else:
                await self._client.close()

    async def _index_exists(self) -> bool:
        if isinstance(self._client, AsyncElasticsearch):
            return bool(await self._client.indices.exists(index=self._index_name))
        return await self._client.indices.exists(index=self._index_name)

    async def _create_index(self) -> None:
        if isinstance(self._client, AsyncElasticsearch):
            _ = await self._client.indices.create(index=self._index_name, mappings=_MAPPINGS)
        else:
            _ = await self._client.indices.create(index=self._index_name, mappings=_MAPPINGS)

    async def _verify_existing_mapping(self) -> None:
        response = await self._get_mapping()
        index_mapping = response.get(self._index_name)
        if not isinstance(index_mapping, dict):
            raise PermanentProjectionError(
                "elasticsearch projection: existing index mapping is malformed",
                target="elasticsearch",
            )
        mappings = index_mapping.get("mappings")
        if not isinstance(mappings, dict):
            raise PermanentProjectionError(
                "elasticsearch projection: existing index mapping is malformed",
                target="elasticsearch",
            )
        metadata = mappings.get("_meta")
        version = metadata.get("mapping_version") if isinstance(metadata, dict) else None
        if not isinstance(version, int) or isinstance(version, bool):
            raise PermanentProjectionError(
                "elasticsearch projection: existing mapping version is malformed",
                target="elasticsearch",
            )
        if version != _MAPPING_VERSION:
            raise PermanentProjectionError(
                "elasticsearch projection: existing mapping version is incompatible",
                target="elasticsearch",
            )

    async def _get_mapping(self) -> Mapping[str, JsonValue]:
        if isinstance(self._client, AsyncElasticsearch):
            response = await self._client.indices.get_mapping(index=self._index_name)
        else:
            response = await self._client.indices.get_mapping(index=self._index_name)
        return _response_body(response)

    async def _index_document(self, document_id: str, document: ElasticsearchDocument) -> None:
        if isinstance(self._client, AsyncElasticsearch):
            _ = await self._client.index(
                index=self._index_name,
                id=document_id,
                document=document,
                op_type="create",
            )
        else:
            _ = await self._client.index(
                index=self._index_name,
                id=document_id,
                document=document,
                op_type="create",
            )

    async def _get_document(
        self,
        document_id: str,
    ) -> Mapping[str, JsonValue] | ObjectApiResponse[dict[str, JsonValue]]:
        if isinstance(self._client, AsyncElasticsearch):
            return await self._client.get(index=self._index_name, id=document_id)
        return await self._client.get(index=self._index_name, id=document_id)


__all__ = ["WorldMemoryElasticsearchWriter"]
