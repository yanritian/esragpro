from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Final, TypeAlias, TypedDict

from ._commands import ElasticsearchCommand

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | dict[str, "JsonValue"] | list["JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class ProjectionMetadataDocument(TypedDict):
    outbox_id: str
    aggregate_id: str
    event_type: str
    schema_version: int
    projection_target: str
    idempotency_key: str


class ElasticsearchDocument(TypedDict):
    id: str
    object_kind: str
    content: str
    evidence_ids: list[str]
    timestamps: JsonObject
    validity: JsonObject | None
    supersedes: list[str]
    contradicts: list[str]
    version: int | None
    fusion_version: int | None
    projection_version: int | None
    status: str | None
    metadata: JsonObject
    source_envelope: JsonObject
    projection_metadata: ProjectionMetadataDocument
    payload_fingerprint: str


_JSON_SEPARATORS: Final[tuple[str, str]] = (",", ":")


def _copy_json_object(value: Mapping[str, JsonValue]) -> JsonObject:
    return {key: item for key, item in value.items()}


def _copy_optional_json_object(
    value: Mapping[str, JsonValue] | None,
) -> JsonObject | None:
    if value is None:
        return None
    return _copy_json_object(value)


def _copy_string_list(value: list[str]) -> list[JsonValue]:
    return [item for item in value]


def _copy_projection_metadata(value: ProjectionMetadataDocument) -> JsonObject:
    return {
        "outbox_id": value["outbox_id"],
        "aggregate_id": value["aggregate_id"],
        "event_type": value["event_type"],
        "schema_version": value["schema_version"],
        "projection_target": value["projection_target"],
        "idempotency_key": value["idempotency_key"],
    }


def _without_fingerprint(document: ElasticsearchDocument) -> JsonObject:
    return {
        "id": document["id"],
        "object_kind": document["object_kind"],
        "content": document["content"],
        "evidence_ids": _copy_string_list(document["evidence_ids"]),
        "timestamps": _copy_json_object(document["timestamps"]),
        "validity": _copy_optional_json_object(document["validity"]),
        "supersedes": _copy_string_list(document["supersedes"]),
        "contradicts": _copy_string_list(document["contradicts"]),
        "version": document["version"],
        "fusion_version": document["fusion_version"],
        "projection_version": document["projection_version"],
        "status": document["status"],
        "metadata": _copy_json_object(document["metadata"]),
        "source_envelope": _copy_json_object(document["source_envelope"]),
        "projection_metadata": _copy_projection_metadata(document["projection_metadata"]),
    }


def _fingerprint(document: ElasticsearchDocument) -> str:
    encoded = json.dumps(
        _without_fingerprint(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=_JSON_SEPARATORS,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def encode_elasticsearch_document(command: ElasticsearchCommand) -> ElasticsearchDocument:
    payload = command.payload
    metadata: ProjectionMetadataDocument = {
        "outbox_id": str(command.metadata.outbox_id),
        "aggregate_id": str(command.metadata.aggregate_id),
        "event_type": command.metadata.event_type,
        "schema_version": command.metadata.schema_version,
        "projection_target": command.metadata.projection_target,
        "idempotency_key": command.metadata.idempotency_key,
    }
    document = ElasticsearchDocument(
        id=payload.id,
        object_kind=payload.object_kind,
        content=payload.content,
        evidence_ids=list(payload.evidence_ids),
        timestamps=_copy_json_object(payload.timestamps),
        validity=_copy_optional_json_object(payload.validity),
        supersedes=list(payload.supersedes),
        contradicts=list(payload.contradicts),
        version=payload.version,
        fusion_version=payload.fusion_version,
        projection_version=payload.projection_version,
        status=payload.status,
        metadata=_copy_json_object(payload.metadata),
        source_envelope=_copy_json_object(payload.source_envelope),
        projection_metadata=metadata,
        payload_fingerprint="",
    )
    document["payload_fingerprint"] = _fingerprint(document)
    return document


def canonical_json(value: Mapping[str, JsonValue]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=_JSON_SEPARATORS,
    )


__all__ = [
    "ElasticsearchDocument",
    "JsonObject",
    "JsonValue",
    "ProjectionMetadataDocument",
    "canonical_json",
    "encode_elasticsearch_document",
]
