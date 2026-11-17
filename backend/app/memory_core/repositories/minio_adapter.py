"""
MinIO Object Storage Adapter (M6) -- DEPRECATED / LEGACY

This adapter is a MOCK/STUB from an earlier M6 task.  All methods return
placeholder values (True/None/[]).  It is NOT used by the P1-08 MinIO
evidence projection pipeline.

The P1-08 production path uses ``MinioProjectionAdapter`` from
``app.memory_core.repositories.minio_projection.adapter``.  That adapter
talks to a real MinIO/S3 endpoint with deterministic object keys,
content-hash idempotency, and proper error classification.

Do NOT add new callers of this legacy adapter.  Any new MinIO work
should go through ``MinioProjectionAdapter``.
"""

import logging
from typing import Optional, Dict, Any
from uuid import UUID

logger = logging.getLogger(__name__)


class MinIOObjectStorageAdapter:
    """
    MinIO adapter for observation and draft object storage.

    Features:
    - Object upload/download
    - Bucket management
    - Tenant isolation
    - Versioning
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket_name: str = "memory-objects",
    ):
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket_name = bucket_name
        # In production, this would use minio.asyncio
        logger.info(f"MinIO adapter initialized: {endpoint}")

    async def upload_object(
        self,
        object_id: UUID,
        data: bytes,
        content_type: str = "application/json",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Upload object to MinIO.

        Args:
            object_id: Object ID
            data: Object data
            content_type: Content type
            metadata: Object metadata

        Returns:
            True if uploaded
        """
        # In production: await client.put_object(...)
        logger.info(f"Uploaded object {object_id} to MinIO ({len(data)} bytes)")
        return True  # Mock

    async def download_object(
        self,
        object_id: UUID,
    ) -> Optional[bytes]:
        """
        Download object from MinIO.

        Args:
            object_id: Object ID

        Returns:
            Object data or None
        """
        # In production: response = await client.get_object(...)
        logger.info(f"Downloaded object {object_id} from MinIO")
        return None  # Mock

    async def delete_object(
        self,
        object_id: UUID,
    ) -> bool:
        """
        Delete object from MinIO.

        Args:
            object_id: Object ID

        Returns:
            True if deleted
        """
        # In production: await client.remove_object(...)
        logger.info(f"Deleted object {object_id} from MinIO")
        return True  # Mock

    async def object_exists(
        self,
        object_id: UUID,
    ) -> bool:
        """
        Check if object exists.

        Args:
            object_id: Object ID

        Returns:
            True if exists
        """
        # In production: await client.stat_object(...)
        logger.info(f"Checked object {object_id} exists")
        return False  # Mock

    async def get_object_metadata(
        self,
        object_id: UUID,
    ) -> Optional[Dict[str, Any]]:
        """
        Get object metadata.

        Args:
            object_id: Object ID

        Returns:
            Object metadata or None
        """
        # In production: stat = await client.stat_object(...)
        logger.info(f"Retrieved metadata for object {object_id}")
        return None  # Mock

    async def list_objects(
        self,
        prefix: Optional[str] = None,
        limit: int = 100,
    ) -> list[Dict[str, Any]]:
        """
        List objects.

        Args:
            prefix: Object prefix
            limit: Max results

        Returns:
            List of objects
        """
        # In production: objects = await client.list_objects(...)
        logger.info(f"Listed objects with prefix: {prefix}")
        return []  # Mock

    async def create_bucket(
        self,
        bucket_name: Optional[str] = None,
    ) -> bool:
        """
        Create bucket.

        Args:
            bucket_name: Bucket name

        Returns:
            True if created
        """
        # In production: await client.make_bucket(...)
        logger.info(f"Created bucket: {bucket_name or self.bucket_name}")
        return True  # Mock

    async def delete_bucket(
        self,
        bucket_name: Optional[str] = None,
    ) -> bool:
        """
        Delete bucket.

        Args:
            bucket_name: Bucket name

        Returns:
            True if deleted
        """
        # In production: await client.remove_bucket(...)
        logger.info(f"Deleted bucket: {bucket_name or self.bucket_name}")
        return True  # Mock

    async def upload_observation(
        self,
        observation_id: UUID,
        observation_data: Dict[str, Any],
    ) -> bool:
        """
        Upload observation object.

        Args:
            observation_id: Observation ID
            observation_data: Observation data

        Returns:
            True if uploaded
        """
        import json

        data = json.dumps(observation_data).encode()
        return await self.upload_object(
            object_id=observation_id,
            data=data,
            content_type="application/json",
            metadata={"type": "observation"},
        )

    async def upload_draft(
        self,
        draft_id: UUID,
        draft_data: Dict[str, Any],
    ) -> bool:
        """
        Upload draft object.

        Args:
            draft_id: Draft ID
            draft_data: Draft data

        Returns:
            True if uploaded
        """
        import json

        data = json.dumps(draft_data).encode()
        return await self.upload_object(
            object_id=draft_id,
            data=data,
            content_type="application/json",
            metadata={"type": "draft"},
        )
