"""
Upload Session Service - 上传会话服务

WP3 核心流程：
1. create_session(): 创建上传会话
2. upload_content(): 上传内容到临时存储
3. finalize_session(): 完成上传，生成 source_ref
4. consume_source_ref(): 消费 source_ref，创建 IngestionJob
5. cleanup_expired(): 清理过期会话
"""
import logging
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.upload_session import UploadSession, UploadSessionState
from app.db.models.ingestion import IngestionJob
from app.storage.blob import BlobStorage, get_blob_storage
from app.security.principal import PrincipalContext

logger = logging.getLogger(__name__)


class UploadSessionService:
    """上传会话服务"""

    def __init__(self, session: AsyncSession, blob_storage: Optional[BlobStorage] = None):
        self.session = session
        self.blob_storage = blob_storage or get_blob_storage()

    async def create_session(
        self,
        tenant_id: UUID,
        knowledge_base_id: UUID,
        filename: str,
        media_type: str,
        size_bytes: int,
        principal: PrincipalContext,
        expires_in_hours: int = 24,
    ) -> UploadSession:
        """
        创建上传会话

        Args:
            tenant_id: 租户 ID
            knowledge_base_id: 知识库 ID
            filename: 文件名
            media_type: MIME 类型
            size_bytes: 文件大小
            principal: 当前主体
            expires_in_hours: 过期时间（小时）

        Returns:
            UploadSession
        """
        # 检查文件大小限制（最大 1GB）
        if size_bytes > 1073741824:
            raise ValueError("File size exceeds maximum limit (1GB)")

        # 创建会话
        upload_session = UploadSession(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            filename=filename,
            media_type=media_type,
            size_bytes=size_bytes,
            state=UploadSessionState.CREATED,
            expires_at=datetime.utcnow() + timedelta(hours=expires_in_hours),
            created_by=principal.subject_id,
        )

        self.session.add(upload_session)
        await self.session.flush()

        logger.info(f"Upload session created: {upload_session.id}")

        return upload_session

    async def upload_content(
        self,
        session_id: UUID,
        tenant_id: UUID,
        content: bytes,
    ) -> UploadSession:
        """
        上传内容

        Args:
            session_id: 会话 ID
            tenant_id: 租户 ID
            content: 文件内容

        Returns:
            UploadSession
        """
        # 查找会话
        result = await self.session.execute(
            select(UploadSession).where(
                and_(
                    UploadSession.id == session_id,
                    UploadSession.tenant_id == tenant_id,
                )
            )
        )
        upload_session = result.scalar_one_or_none()

        if not upload_session:
            raise ValueError(f"Upload session not found: {session_id}")

        if upload_session.state != UploadSessionState.CREATED:
            raise ValueError(f"Upload session in wrong state: {upload_session.state}")

        if upload_session.expires_at < datetime.utcnow():
            raise ValueError("Upload session has expired")

        # 检查内容大小
        if len(content) != upload_session.size_bytes:
            raise ValueError(f"Content size mismatch: expected {upload_session.size_bytes}, got {len(content)}")

        # 计算哈希
        content_hash = await self.blob_storage.compute_hash(content)

        # 存储内容
        storage_key = await self.blob_storage.put(
            tenant_id=tenant_id,
            key=f"upload/{session_id}",
            content=content,
            content_type=upload_session.media_type,
        )

        # 更新会话
        upload_session.state = UploadSessionState.UPLOADED
        upload_session.sha256 = content_hash
        upload_session.storage_key = storage_key
        upload_session.updated_at = datetime.utcnow()

        await self.session.flush()

        logger.info(f"Upload content uploaded: {session_id}, hash: {content_hash}")

        return upload_session

    async def finalize_session(
        self,
        session_id: UUID,
        tenant_id: UUID,
    ) -> UploadSession:
        """
        完成上传会话

        生成 source_ref 供 IngestionJob 消费

        Args:
            session_id: 会话 ID
            tenant_id: 租户 ID

        Returns:
            UploadSession
        """
        # 查找会话
        result = await self.session.execute(
            select(UploadSession).where(
                and_(
                    UploadSession.id == session_id,
                    UploadSession.tenant_id == tenant_id,
                )
            )
        )
        upload_session = result.scalar_one_or_none()

        if not upload_session:
            raise ValueError(f"Upload session not found: {session_id}")

        if upload_session.state != UploadSessionState.UPLOADED:
            raise ValueError(f"Upload session in wrong state: {upload_session.state}")

        if upload_session.expires_at < datetime.utcnow():
            raise ValueError("Upload session has expired")

        # 生成 source_ref（单次消费令牌）
        source_ref = f"upload-session://{session_id}/{upload_session.sha256}"

        # 更新会话
        upload_session.state = UploadSessionState.FINALIZED
        upload_session.source_ref = source_ref
        upload_session.updated_at = datetime.utcnow()

        await self.session.flush()

        logger.info(f"Upload session finalized: {session_id}, source_ref: {source_ref}")

        return upload_session

    async def consume_source_ref(
        self,
        tenant_id: UUID,
        knowledge_base_id: UUID,
        source_ref: str,
        principal: PrincipalContext,
    ) -> tuple[IngestionJob, bool]:
        """
        消费 source_ref 创建 IngestionJob

        source_ref 只能被消费一次

        Args:
            tenant_id: 租户 ID
            knowledge_base_id: 知识库 ID
            source_ref: 来源引用
            principal: 当前主体

        Returns:
            (IngestionJob, is_new) - 任务和是否新创建
        """
        # 解析 source_ref
        if not source_ref.startswith("upload-session://"):
            raise ValueError(f"Invalid source_ref format: {source_ref}")

        parts = source_ref[16:].split("/")
        if len(parts) != 2:
            raise ValueError(f"Invalid source_ref format: {source_ref}")

        session_id = UUID(parts[0])
        # 哈希已包含在 source_ref 中，用于完整性验证

        # 查找会话
        result = await self.session.execute(
            select(UploadSession).where(
                and_(
                    UploadSession.id == session_id,
                    UploadSession.tenant_id == tenant_id,
                    UploadSession.knowledge_base_id == knowledge_base_id,
                    UploadSession.source_ref == source_ref,
                )
            )
        )
        upload_session = result.scalar_one_or_none()

        if not upload_session:
            raise ValueError("Upload session not found or source_ref mismatch")

        if upload_session.state != UploadSessionState.FINALIZED:
            raise ValueError(f"Upload session in wrong state: {upload_session.state}")

        # 使用 sha256 作为幂等键
        idempotency_key = upload_session.sha256
        if idempotency_key is None:
            raise ValueError("Upload session has no content hash; cannot create ingestion job")

        # 检查是否已消费
        job_result = await self.session.execute(
            select(IngestionJob).where(
                and_(
                    IngestionJob.tenant_id == tenant_id,
                    IngestionJob.idempotency_key == idempotency_key,
                )
            )
        )
        existing_job = job_result.scalar_one_or_none()

        if existing_job:
            # 已消费，返回已有任务
            logger.info(f"Source ref already consumed: {source_ref}, job: {existing_job.id}")
            return existing_job, False

        # 创建 IngestionJob（这里简化，实际需要完整的 Document/Version 创建）
        from app.services.ingestion import IngestionService

        ingestion_service = IngestionService(self.session)
        job, is_new = await ingestion_service.create_ingestion_job(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            source_ref=source_ref,
            idempotency_key=idempotency_key,
            principal=principal,
            metadata={
                "filename": upload_session.filename,
                "media_type": upload_session.media_type,
                "size_bytes": upload_session.size_bytes,
                "content_hash": upload_session.sha256,
            },
        )

        # 标记会话已消费
        upload_session.state = UploadSessionState.CONSUMED
        upload_session.updated_at = datetime.utcnow()

        await self.session.flush()

        logger.info(f"Source ref consumed: {source_ref}, job: {job.id}")

        return job, is_new

    async def abort_session(
        self,
        session_id: UUID,
        tenant_id: UUID,
    ) -> bool:
        """
        中止上传会话

        Args:
            session_id: 会话 ID
            tenant_id: 租户 ID

        Returns:
            是否成功
        """
        result = await self.session.execute(
            select(UploadSession).where(
                and_(
                    UploadSession.id == session_id,
                    UploadSession.tenant_id == tenant_id,
                )
            )
        )
        upload_session = result.scalar_one_or_none()

        if not upload_session:
            return False

        # 只能中止 created 或 uploaded 状态
        if upload_session.state not in [UploadSessionState.CREATED, UploadSessionState.UPLOADED]:
            return False

        # 删除临时存储
        if upload_session.storage_key:
            await self.blob_storage.delete(
                tenant_id=tenant_id,
                storage_key=upload_session.storage_key,
            )

        # 更新状态
        upload_session.state = UploadSessionState.ABORTED
        upload_session.updated_at = datetime.utcnow()

        await self.session.flush()

        logger.info(f"Upload session aborted: {session_id}")

        return True

    async def cleanup_expired(self, batch_size: int = 100) -> int:
        """
        清理过期会话

        Args:
            batch_size: 批次大小

        Returns:
            清理数量
        """
        now = datetime.utcnow()

        result = await self.session.execute(
            select(UploadSession)
            .where(
                and_(
                    UploadSession.expires_at < now,
                    UploadSession.state.in_([
                        UploadSessionState.CREATED,
                        UploadSessionState.UPLOADED,
                        UploadSessionState.FINALIZED,
                    ]),
                )
            )
            .limit(batch_size)
        )
        expired_sessions = list(result.scalars().all())

        count = 0
        for upload_session in expired_sessions:
            try:
                # 删除临时存储
                if upload_session.storage_key:
                    await self.blob_storage.delete(
                        tenant_id=upload_session.tenant_id,
                        storage_key=upload_session.storage_key,
                    )

                # 更新状态
                upload_session.state = UploadSessionState.EXPIRED
                upload_session.updated_at = now
                count += 1
            except Exception as e:
                logger.error(f"Failed to cleanup session {upload_session.id}: {e}")

        await self.session.flush()

        logger.info(f"Cleaned up {count} expired sessions")

        return count