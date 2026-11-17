"""MinIO projection package (P1-08).

Provides raw-evidence storage in MinIO/S3-compatible object stores.
"""

from app.memory_core.repositories.minio_projection.models import (
    MinioEvidencePayload,
    PermanentMinioPayloadError,
    compute_content_hash,
    validate_evidence_payload,
)

__all__ = [
    "MinioEvidencePayload",
    "PermanentMinioPayloadError",
    "compute_content_hash",
    "validate_evidence_payload",
]
