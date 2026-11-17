from __future__ import annotations

from uuid import UUID

from app.memory_core.world_memory_projection._commands import (
    ElasticsearchCommand,
    ProjectionMetadata,
)
from app.memory_core.world_memory_projection._write_codec import (
    encode_elasticsearch_document,
)
from app.memory_core.world_memory_projection.payloads import ElasticsearchPayload


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


def test_identical_command_has_stable_fingerprint_and_complete_envelope() -> None:
    first = encode_elasticsearch_document(_command())
    second = encode_elasticsearch_document(_command())

    assert first == second
    assert first == {
        "id": "object-1",
        "object_kind": "observation",
        "content": "stable",
        "evidence_ids": ["e-1"],
        "timestamps": {"observed_at": "2026-01-01T00:00:00+00:00"},
        "validity": None,
        "supersedes": [],
        "contradicts": [],
        "version": 1,
        "fusion_version": None,
        "projection_version": 1,
        "status": "active",
        "metadata": {"source": "sensor", "nested": {"priority": 1}},
        "source_envelope": {"world_memory_snapshot": {"id": "object-1"}},
        "projection_metadata": {
            "outbox_id": "00000000-0000-0000-0000-000000000002",
            "aggregate_id": "00000000-0000-0000-0000-000000000001",
            "event_type": "world.observation.ingested",
            "schema_version": 1,
            "projection_target": "elasticsearch",
            "idempotency_key": "idem-1",
        },
        "payload_fingerprint": first["payload_fingerprint"],
    }
    assert len(first["payload_fingerprint"]) == 64


def test_codec_fingerprint_changes_for_payload_or_command_identity() -> None:
    first = encode_elasticsearch_document(_command())
    assert (
        first["payload_fingerprint"]
        != encode_elasticsearch_document(_command(content="changed"))["payload_fingerprint"]
    )
    assert (
        first["payload_fingerprint"]
        != encode_elasticsearch_document(_command(key="idem-2"))["payload_fingerprint"]
    )
