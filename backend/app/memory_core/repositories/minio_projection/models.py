"""MinIO projection payload models and validation (P1-08).

Defines the JSON-safe payload contract for MinIO raw-evidence storage.
Provides validation to ensure deterministic, restart-persistent evidence
projection without ACL/tenant/authorization fields.

The object body stored in MinIO is the original ``memory_snapshot.content``
text -- the immutable source content that the memory captured.  The
evidence-reference identity (source_id, source_type, chunk_id) and the
content hash are stored as object metadata for traceability.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID


class PermanentMinioPayloadError(Exception):
    """Permanent payload validation error for MinIO projection.

    Raised when the payload is structurally invalid and no retry can fix it.
    The worker will dead-letter the envelope immediately.
    """


@dataclass(frozen=True)
class MinioEvidencePayload:
    """Validated MinIO evidence payload.

    The ``evidence_bytes`` field contains the original source content
    (``memory_snapshot.content``) encoded as UTF-8.  The content_hash
    is the SHA-256 of those bytes.  The evidence-reference identity
    fields (source_id, source_type, chunk_id) are stored as MinIO
    object metadata alongside the hash.
    """

    memory_id: UUID
    evidence_bytes: bytes
    content_hash: str
    source_id: str
    source_type: str
    timestamp: str
    chunk_id: Optional[str]
    confidence: float
    metadata: dict[str, Any]

    def object_key(self) -> str:
        """Compute deterministic object key from memory_id and content_hash.

        Format: memory/{memory_id}/evidence/{content_hash[:16]}
        This ensures:
        - Same memory_id + same evidence = same key (idempotent)
        - Different evidence = different key (collision detected)
        - Keys are stable across restarts

        The stored full hash in object metadata must be verified
        before declaring an idempotent success to guard against the
        16-hex-char truncation collision space.
        """
        return f"memory/{self.memory_id}/evidence/{self.content_hash[:16]}"


def compute_content_hash(evidence_bytes: bytes) -> str:
    """Compute SHA-256 hash of evidence bytes.

    Returns a hex digest string used for deterministic object key derivation.
    """
    return hashlib.sha256(evidence_bytes).hexdigest()


def validate_evidence_payload(
    payload: dict[str, Any],
    *,
    envelope_idempotency_key: str,
    envelope_aggregate_id: str,
    envelope_event_type: str,
    envelope_schema_version: int,
    envelope_target: str,
) -> MinioEvidencePayload:
    """Validate and extract MinIO evidence payload from projection event.

    The object body is ``memory_snapshot.content`` -- the original
    immutable source text that the memory captured.  The evidence
    reference identity (source_id, source_type, chunk_id) and the
    content hash are stored as MinIO object metadata for traceability.

    Checks:
    - Envelope fields match expected values
    - Memory snapshot contains content and evidence_refs
    - Evidence refs have required fields

    Raises PermanentMinioPayloadError on validation failure.
    """
    # Validate envelope
    if envelope_event_type != "memory.remembered":
        raise PermanentMinioPayloadError(
            f"minio: expected event_type='memory.remembered', got {envelope_event_type!r}"
        )
    if envelope_schema_version != 1:
        raise PermanentMinioPayloadError(
            f"minio: expected schema_version=1, got {envelope_schema_version}"
        )
    if envelope_target != "minio":
        raise PermanentMinioPayloadError(
            f"minio: expected projection_target='minio', got {envelope_target!r}"
        )

    # Extract memory snapshot
    memory_snapshot = payload.get("memory_snapshot")
    if not isinstance(memory_snapshot, dict):
        raise PermanentMinioPayloadError("minio: payload missing 'memory_snapshot' dict")

    memory_id_str = memory_snapshot.get("id")
    if not isinstance(memory_id_str, str) or not memory_id_str.strip():
        raise PermanentMinioPayloadError("minio: memory_snapshot.id must be a non-empty string")

    try:
        memory_id = UUID(memory_id_str)
    except ValueError as exc:
        raise PermanentMinioPayloadError(f"minio: invalid memory_snapshot.id UUID: {exc}") from exc

    if str(memory_id) != envelope_aggregate_id:
        raise PermanentMinioPayloadError(
            f"minio: memory_snapshot.id {memory_id} does not match "
            f"envelope aggregate_id {envelope_aggregate_id}"
        )

    # --- Original evidence content (object body) ---
    # The MinIO object body is the memory_snapshot.content text.
    # This is the immutable source content the memory captured.
    snapshot_content = memory_snapshot.get("content")
    if not isinstance(snapshot_content, str):
        raise PermanentMinioPayloadError("minio: memory_snapshot.content must be a string")
    try:
        evidence_bytes = snapshot_content.encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PermanentMinioPayloadError(
            f"minio: memory_snapshot.content is not encodable: {exc}"
        ) from exc

    # --- Evidence reference identity (metadata, not body) ---
    evidence_refs = memory_snapshot.get("evidence_refs", [])
    if not isinstance(evidence_refs, list):
        raise PermanentMinioPayloadError("minio: memory_snapshot.evidence_refs must be a list")

    if len(evidence_refs) == 0:
        raise PermanentMinioPayloadError(
            "minio: memory_snapshot.evidence_refs is empty; "
            "MinIO projection requires at least one evidence ref"
        )

    # The first evidence ref provides identity/tracing metadata
    # stored on the MinIO object, not as the object body.
    first_ref = evidence_refs[0]
    if not isinstance(first_ref, dict):
        raise PermanentMinioPayloadError("minio: first evidence_ref must be a dict")

    source_id = first_ref.get("source_id")
    if not isinstance(source_id, str) or not source_id.strip():
        raise PermanentMinioPayloadError(
            "minio: first evidence_ref.source_id must be a non-empty string"
        )

    source_type = first_ref.get("source_type")
    if not isinstance(source_type, str) or not source_type.strip():
        raise PermanentMinioPayloadError(
            "minio: first evidence_ref.source_type must be a non-empty string"
        )

    timestamp = first_ref.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise PermanentMinioPayloadError(
            "minio: first evidence_ref.timestamp must be a non-empty string"
        )

    chunk_id = first_ref.get("chunk_id")
    if chunk_id is not None and not isinstance(chunk_id, str):
        raise PermanentMinioPayloadError(
            "minio: first evidence_ref.chunk_id must be a string or null"
        )

    confidence = first_ref.get("confidence", 1.0)
    if not isinstance(confidence, (int, float)):
        raise PermanentMinioPayloadError("minio: first evidence_ref.confidence must be a number")
    confidence = float(confidence)
    if confidence < 0.0 or confidence > 1.0:
        raise PermanentMinioPayloadError(
            f"minio: first evidence_ref.confidence {confidence} out of range [0, 1]"
        )

    metadata = first_ref.get("metadata", {})
    if not isinstance(metadata, dict):
        raise PermanentMinioPayloadError("minio: first evidence_ref.metadata must be a dict")

    content_hash = compute_content_hash(evidence_bytes)

    return MinioEvidencePayload(
        memory_id=memory_id,
        evidence_bytes=evidence_bytes,
        content_hash=content_hash,
        source_id=source_id,
        source_type=source_type,
        timestamp=timestamp,
        chunk_id=chunk_id,
        confidence=confidence,
        metadata=dict(metadata),
    )


__all__ = [
    "MinioEvidencePayload",
    "PermanentMinioPayloadError",
    "compute_content_hash",
    "validate_evidence_payload",
]
