"""
Ingestion Service - 异步摄取服务

WP3 核心流程：
1. 原始文档先登记 document/document_version
2. 创建 ingestion_job（幂等）
3. 异步执行 OCR、embedding、图抽取
4. 使用 Outbox 模式驱动投影更新
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ingestion import (
    IngestionJob,
    IngestionJobOutbox,
    IngestionJobState,
    OutboxEvent,
    ProjectionTarget,
)
from app.db.models.identity import Document, DocumentVersion
from app.services.stable_id import StableIDGenerator, VersionedChunker
from app.security.principal import PrincipalContext

logger = logging.getLogger(__name__)


class IngestionService:
    """异步摄取服务"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.chunker = VersionedChunker()

    async def create_ingestion_job(
        self,
        tenant_id: UUID,
        knowledge_base_id: UUID,
        source_ref: str,
        idempotency_key: str,
        principal: PrincipalContext,
        metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[IngestionJob, bool]:
        """
        创建摄取任务（幂等）

        Args:
            tenant_id: 租户 ID
            knowledge_base_id: 知识库 ID
            source_ref: 来源引用（从上传会话消费）
            idempotency_key: 幂等键
            principal: 当前主体
            metadata: 可选元数据

        Returns:
            (IngestionJob, is_new) - 任务和是否新创建
        """
        # 幂等检查：查找现有 job
        existing_job = await self._find_job_by_idempotency_key(
            tenant_id, idempotency_key
        )

        if existing_job:
            logger.info(f"Idempotent job found: {existing_job.id}")
            return existing_job, False

        # 使用稳定 ID 生成器创建确定性 ID
        content_hash = metadata.get("content_hash", "") if metadata else ""
        source_hash = StableIDGenerator.compute_content_hash(source_ref)

        # 生成稳定的 Document ID（基于 tenant + kb + source）
        document_id = StableIDGenerator.generate_document_id(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            source_hash=source_hash,
        )

        # 生成稳定的 Version ID（基于 document + version_number + content_hash）
        version_id = StableIDGenerator.generate_version_id(
            document_id=document_id,
            version_number=1,
            content_hash=content_hash,
        )

        # 创建 Document
        document = Document(
            id=document_id,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            title=metadata.get("title", "Untitled") if metadata else "Untitled",
            doc_type=metadata.get("doc_type", "unknown") if metadata else "unknown",
            is_active=True,
        )
        self.session.add(document)

        # 创建 DocumentVersion
        version = DocumentVersion(
            id=version_id,
            document_id=document_id,
            version_number=1,
            content_hash=metadata.get("content_hash", "") if metadata else "",
            storage_key=source_ref,
            size_bytes=metadata.get("size_bytes", 0) if metadata else 0,
            state="uploaded",
            extra_data=metadata,
        )
        self.session.add(version)

        # 创建 IngestionJob
        job = IngestionJob(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            version_id=version_id,
            idempotency_key=idempotency_key,
            state=IngestionJobState.PENDING,
            source_ref=source_ref,
            extra_data=metadata,
        )
        self.session.add(job)

        # 创建 Outbox 事件
        outbox_event = IngestionJobOutbox(
            job_id=job.id,
            event_type=OutboxEvent.JOB_CREATED,
            payload={
                "tenant_id": str(tenant_id),
                "knowledge_base_id": str(knowledge_base_id),
                "document_id": str(document_id),
                "version_id": str(version_id),
                "source_ref": source_ref,
            },
        )
        self.session.add(outbox_event)

        await self.session.flush()

        return job, True

    async def _find_job_by_idempotency_key(
        self,
        tenant_id: UUID,
        idempotency_key: str,
    ) -> Optional[IngestionJob]:
        """根据幂等键查找现有任务"""
        result = await self.session.execute(
            select(IngestionJob).where(
                and_(
                    IngestionJob.tenant_id == tenant_id,
                    IngestionJob.idempotency_key == idempotency_key,
                )
            )
        )
        return result.scalar_one_or_none()

    async def start_job(self, job_id: UUID) -> bool:
        """
        开始执行摄取任务

        状态转换：pending -> processing
        """
        result = await self.session.execute(
            select(IngestionJob).where(IngestionJob.id == job_id)
        )
        job = result.scalar_one_or_none()

        if not job:
            return False

        if job.state != IngestionJobState.PENDING:
            return False

        # 更新状态
        job.state = IngestionJobState.PROCESSING
        job.started_at = datetime.utcnow()
        job.attempt += 1

        # 创建 Outbox 事件
        outbox_event = IngestionJobOutbox(
            job_id=job.id,
            event_type=OutboxEvent.JOB_STARTED,
            payload={"attempt": job.attempt},
        )
        self.session.add(outbox_event)

        await self.session.flush()
        return True

    async def complete_job(self, job_id: UUID) -> bool:
        """
        完成摄取任务

        状态转换：processing -> completed
        """
        result = await self.session.execute(
            select(IngestionJob).where(IngestionJob.id == job_id)
        )
        job = result.scalar_one_or_none()

        if not job:
            return False

        if job.state != IngestionJobState.PROCESSING:
            return False

        # 更新状态
        job.state = IngestionJobState.COMPLETED
        job.completed_at = datetime.utcnow()

        # 创建 Outbox 事件
        outbox_event = IngestionJobOutbox(
            job_id=job.id,
            event_type=OutboxEvent.JOB_COMPLETED,
            payload={},
        )
        self.session.add(outbox_event)

        await self.session.flush()
        return True

    async def fail_job(
        self,
        job_id: UUID,
        error_code: str,
        error_message: str,
        retry_after_seconds: int = 60,
    ) -> bool:
        """
        标记任务失败（可重试）

        状态转换：processing -> failed
        """
        result = await self.session.execute(
            select(IngestionJob).where(IngestionJob.id == job_id)
        )
        job = result.scalar_one_or_none()

        if not job:
            return False

        if job.state != IngestionJobState.PROCESSING:
            return False

        # 更新状态
        job.state = IngestionJobState.FAILED
        job.last_error_code = error_code
        job.last_error_message = error_message

        # 设置下次重试时间
        if job.attempt < job.max_attempts:
            job.next_retry_at = datetime.utcnow() + timedelta(seconds=retry_after_seconds)

        # 创建 Outbox 事件
        outbox_event = IngestionJobOutbox(
            job_id=job.id,
            event_type=OutboxEvent.JOB_FAILED,
            payload={
                "error_code": error_code,
                "error_message": error_message,
                "attempt": job.attempt,
                "max_attempts": job.max_attempts,
            },
        )
        self.session.add(outbox_event)

        await self.session.flush()
        return True

    async def request_es_projection(self, job_id: UUID) -> bool:
        """
        请求 ES 投影更新

        创建 Outbox 事件驱动 ES 索引
        """
        result = await self.session.execute(
            select(IngestionJob).where(IngestionJob.id == job_id)
        )
        job = result.scalar_one_or_none()

        if not job:
            return False

        outbox_event = IngestionJobOutbox(
            job_id=job.id,
            event_type=OutboxEvent.ES_INDEX_REQUESTED,
            projection_target=ProjectionTarget.ELASTICSEARCH,
            payload={
                "document_id": str(job.document_id),
                "version_id": str(job.version_id),
            },
        )
        self.session.add(outbox_event)

        await self.session.flush()
        return True

    async def get_pending_jobs(
        self,
        tenant_id: Optional[UUID] = None,
        limit: int = 100,
    ) -> list[IngestionJob]:
        """
        获取待处理的任务

        包括：pending 状态和到期重试的 failed 状态
        """
        now = datetime.utcnow()

        conditions = [
            IngestionJob.state == IngestionJobState.PENDING,
        ]

        if tenant_id:
            conditions.append(IngestionJob.tenant_id == tenant_id)

        # Pending jobs
        result = await self.session.execute(
            select(IngestionJob)
            .where(and_(*conditions))
            .limit(limit)
        )
        pending_jobs = list(result.scalars().all())

        # Retryable failed jobs
        retry_conditions = [
            IngestionJob.state == IngestionJobState.FAILED,
            IngestionJob.next_retry_at <= now,
        ]
        if tenant_id:
            retry_conditions.append(IngestionJob.tenant_id == tenant_id)

        result = await self.session.execute(
            select(IngestionJob)
            .where(and_(*retry_conditions))
            .limit(limit)
        )
        retry_jobs = list(result.scalars().all())

        return pending_jobs + retry_jobs


async def create_ingestion_job(
    session: AsyncSession,
    tenant_id: UUID,
    knowledge_base_id: UUID,
    source_ref: str,
    idempotency_key: str,
    principal: PrincipalContext,
    metadata: Optional[dict[str, Any]] = None,
) -> tuple[IngestionJob, bool]:
    """便捷函数：创建摄取任务"""
    service = IngestionService(session)
    return await service.create_ingestion_job(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        source_ref=source_ref,
        idempotency_key=idempotency_key,
        principal=principal,
        metadata=metadata,
    )