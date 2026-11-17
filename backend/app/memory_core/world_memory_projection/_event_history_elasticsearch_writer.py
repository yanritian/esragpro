from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final, Literal, Protocol, TypeAlias, final
from uuid import UUID

from elastic_transport import ObjectApiResponse
from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import BadRequestError, ConflictError

from ..projection_worker.contracts import PermanentProjectionError
from ..repositories.world_memory_authority import envelope_has_evidence
from ._commands import ElasticsearchCommand, ProjectionMetadata
from ._write_codec import JsonValue
from ._writer_protocols import ElasticsearchWriter

_DEFAULT_INDEX_NAME: Final = "world-memory-events-v2"
_MAPPING_VERSION: Final = 2


EventHistoryDocument: TypeAlias = dict[str, JsonValue]


class _IndicesClient(Protocol):
    async def exists(self, *, index: str) -> bool: ...

    async def create(
        self,
        *,
        index: str,
        mappings: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]: ...

    async def get_mapping(self, *, index: str) -> Mapping[str, JsonValue]: ...

    async def put_mapping(
        self,
        *,
        index: str,
        properties: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, JsonValue]: ...


class _ElasticsearchClient(Protocol):
    @property
    def indices(self) -> _IndicesClient: ...

    async def index(
        self,
        *,
        index: str,
        id: str,
        document: Mapping[str, JsonValue],
        op_type: Literal["create"],
    ) -> Mapping[str, JsonValue]: ...

    async def get(self, *, index: str, id: str) -> Mapping[str, JsonValue]: ...

    async def update(
        self,
        *,
        index: str,
        id: str,
        doc: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]: ...

    async def close(self) -> None: ...


_MAPPINGS: Mapping[str, JsonValue] = {
    "_meta": {"mapping_version": _MAPPING_VERSION, "purpose": "world_memory_event_history"},
    "dynamic": "strict",
    "properties": {
        "envelope_id": {"type": "keyword"},
        "authority_envelope_id": {"type": "keyword"},
        "has_evidence": {"type": "boolean"},
        "object_id": {"type": "keyword"},
        "aggregate_id": {"type": "keyword"},
        "object_kind": {"type": "keyword"},
        "content": {"type": "text"},
        "occurred_at": {"type": "date"},
        "event_type": {"type": "keyword"},
        "schema_version": {"type": "integer"},
        "projection_target": {"type": "keyword"},
        "payload_fingerprint": {"type": "keyword"},
    },
}


def _response_body(
    response: Mapping[str, JsonValue] | ObjectApiResponse[dict[str, JsonValue]],
) -> Mapping[str, JsonValue]:
    if isinstance(response, ObjectApiResponse):
        return response.body
    return response


def _fingerprint(document: EventHistoryDocument) -> str:
    encoded = json.dumps(
        {
            key: value
            for key, value in document.items()
            if key not in {"payload_fingerprint", "has_evidence"}
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _authority_envelope_id(metadata: ProjectionMetadata) -> str:
    match metadata.authority_envelope_id:
        case UUID() as authority_envelope_id:
            return str(authority_envelope_id)
        case None:
            raise PermanentProjectionError(
                "world-memory event history: authority_envelope_id is required",
                target="elasticsearch",
            )


def encode_event_history_document(command: ElasticsearchCommand) -> EventHistoryDocument:
    if command.occurred_at is None:
        raise PermanentProjectionError(
            "world-memory event history: occurred_at is required",
            target="elasticsearch",
        )
    envelope_id = _authority_envelope_id(command.metadata)
    document: EventHistoryDocument = {
        "envelope_id": envelope_id,
        "authority_envelope_id": envelope_id,
        "has_evidence": envelope_has_evidence(command.payload.source_envelope),
        "object_id": command.payload.id,
        "aggregate_id": str(command.metadata.aggregate_id),
        "object_kind": command.payload.object_kind,
        "content": command.payload.content,
        "occurred_at": command.occurred_at.isoformat(),
        "event_type": command.metadata.event_type,
        "schema_version": command.metadata.schema_version,
        "projection_target": command.metadata.projection_target,
        "payload_fingerprint": "",
    }
    document["payload_fingerprint"] = _fingerprint(document)
    return document


@final
class WorldMemoryEventHistoryElasticsearchWriter:
    def __init__(
        self,
        client: AsyncElasticsearch | _ElasticsearchClient,
        *,
        index_name: str = _DEFAULT_INDEX_NAME,
        owns_client: bool = False,
    ) -> None:
        self._client: AsyncElasticsearch | _ElasticsearchClient = client
        self._index_name: str = index_name
        self._owns_client: bool = owns_client

    async def ensure_index(self) -> None:
        if await self._client.indices.exists(index=self._index_name):
            response = await self._client.indices.get_mapping(index=self._index_name)
            mapping = _response_body(response).get(self._index_name)
            match mapping:
                case {
                    "mappings": {
                        "_meta": {"mapping_version": int() as version},
                        "properties": dict() as properties,
                    }
                } if version == _MAPPING_VERSION:
                    missing_properties: dict[str, Mapping[str, Any]] = {}
                    if "authority_envelope_id" not in properties:
                        missing_properties["authority_envelope_id"] = {"type": "keyword"}
                    if "has_evidence" not in properties:
                        missing_properties["has_evidence"] = {"type": "boolean"}
                    if missing_properties:
                        _ = await self._client.indices.put_mapping(
                            index=self._index_name,
                            properties=missing_properties,
                        )
                    return
                case _:
                    raise PermanentProjectionError(
                        "world-memory event history: existing mapping version is incompatible",
                        target="elasticsearch",
                    )
        try:
            _ = await self._client.indices.create(index=self._index_name, mappings=_MAPPINGS)
        except ConflictError:
            return
        except BadRequestError as exc:
            if "resource_already_exists_exception" in str(exc):
                return
            raise

    async def __call__(self, command: ElasticsearchCommand) -> None:
        document = encode_event_history_document(command)
        envelope_id = _authority_envelope_id(command.metadata)
        try:
            _ = await self._client.index(
                index=self._index_name,
                id=envelope_id,
                document=document,
                op_type="create",
            )
        except ConflictError as exc:
            source = _response_body(
                await self._client.get(index=self._index_name, id=envelope_id)
            ).get("_source")
            match source:
                case {"payload_fingerprint": str() as fingerprint}:
                    if fingerprint == document["payload_fingerprint"]:
                        if source.get("has_evidence") != document["has_evidence"]:
                            _ = await self._client.update(
                                index=self._index_name,
                                id=envelope_id,
                                doc={"has_evidence": document["has_evidence"]},
                            )
                        return
                    raise PermanentProjectionError(
                        "world-memory event history: envelope payload conflict",
                        target="elasticsearch",
                    ) from exc
                case _:
                    raise PermanentProjectionError(
                        "world-memory event history: existing document is malformed",
                        target="elasticsearch",
                    ) from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.close()


@final
class WorldMemoryElasticsearchEventFanoutWriter:
    def __init__(
        self,
        current_writer: ElasticsearchWriter,
        event_history_writer: ElasticsearchWriter,
    ) -> None:
        self._current_writer: ElasticsearchWriter = current_writer
        self._event_history_writer: ElasticsearchWriter = event_history_writer

    async def __call__(self, command: ElasticsearchCommand) -> None:
        await self._current_writer(command)
        await self._event_history_writer(command)


__all__ = [
    "EventHistoryDocument",
    "WorldMemoryElasticsearchEventFanoutWriter",
    "WorldMemoryEventHistoryElasticsearchWriter",
    "encode_event_history_document",
]
