"""
ARQ Tasks - 异步任务定义

WP3 核心任务：
- process_ingestion_job: 处理摄取任务
- process_outbox_events: 处理 Outbox 事件
- process_deletion_job: 处理删除任务
"""
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from arq import cron
from arq.connections import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.core.config import settings
from app.db.models.ingestion import (
    IngestionJob,
    IngestionJobOutbox,
    IngestionJobState,
    ProjectionTarget,
)
from app.services.ingestion import IngestionService
from app.workers.settings import WorkerSettings, get_worker_settings

logger = logging.getLogger(__name__)


async def process_ingestion_job(
    ctx: dict[str, Any],
    job_id: str,
) -> dict[str, Any]:
    """
    处理摄取任务

    流程：
    1. 从 Blob 存储获取文件
    2. 解析文档（OCR/PDF/DOCX）
    3. 分块（使用稳定 ID）
    4. 生成 embedding
    5. 写入 ES 投影
    6. 更新任务状态
    """
    job_uuid = UUID(job_id)

    logger.info(f"Processing ingestion job: {job_id}")

    # 创建数据库会话
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
    )
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with async_session() as session:
            # 获取任务
            result = await session.execute(
                select(IngestionJob).where(IngestionJob.id == job_uuid)
            )
            job = result.scalar_one_or_none()

            if not job:
                logger.error(f"Job not found: {job_id}")
                return {"success": False, "error": "Job not found"}

            if job.state != IngestionJobState.PENDING:
                logger.warning(f"Job in wrong state: {job.state}")
                return {"success": False, "error": f"Wrong state: {job.state}"}

            # 开始处理
            service = IngestionService(session)
            await service.start_job(job_uuid)

            # TODO: 实际处理流程
            # 1. 从 Blob 获取文件
            # 2. 解析文档
            # 3. 分块
            # 4. 生成 embedding
            # 5. 写入 ES

            # 标记完成
            await service.complete_job(job_uuid)

            logger.info(f"Job completed: {job_id}")
            return {"success": True, "job_id": job_id}

    except Exception as e:
        logger.error(f"Job failed: {job_id}, error: {e}")

        # 标记失败
        async with async_session() as session:
            service = IngestionService(session)
            await service.fail_job(
                job_uuid,
                error_code="PROCESSING_ERROR",
                error_message=str(e),
            )

        return {"success": False, "error": str(e)}

    finally:
        await engine.dispose()


async def process_outbox_events(
    ctx: dict[str, Any],
) -> dict[str, Any]:
    """
    处理 Outbox 事件

    分发到 ES/Neo4j 投影器
    """
    worker_settings: WorkerSettings = ctx["worker_settings"]

    logger.info("Processing outbox events")

    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
    )
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with async_session() as session:
            # 获取待处理事件
            result = await session.execute(
                select(IngestionJobOutbox)
                .where(IngestionJobOutbox.projection_state == "pending")
                .limit(worker_settings.es_batch_size)
            )
            events = list(result.scalars().all())

            processed = 0
            failed = 0

            for event in events:
                try:
                    # 根据投影目标分发
                    if event.projection_target == ProjectionTarget.ELASTICSEARCH:
                        # 处理 ES 投影
                        await process_es_projection(session, event)
                    elif event.projection_target == ProjectionTarget.NEO4J:
                        # 处理 Neo4j 投影
                        await process_neo4j_projection(session, event)
                    else:
                        # 默认处理
                        logger.warning(f"Unknown projection target: {event.projection_target}")

                    event.projection_state = "completed"
                    event.processed_at = datetime.utcnow()
                    processed += 1

                except Exception as e:
                    logger.error(f"Event processing failed: {event.id}, error: {e}")
                    event.projection_state = "failed"
                    failed += 1

            await session.flush()

            logger.info(f"Processed {processed} events, {failed} failed")
            return {"processed": processed, "failed": failed}

    finally:
        await engine.dispose()


async def process_es_projection(
    session: AsyncSession,
    event: IngestionJobOutbox,
) -> None:
    """
    处理 ES 投影

    TODO: 实现实际的 ES 索引逻辑
    """
    logger.info(f"Processing ES projection for event: {event.id}")
    # 占位：实际需要调用 ES 投影器
    pass


async def process_neo4j_projection(
    session: AsyncSession,
    event: IngestionJobOutbox,
) -> None:
    """
    处理 Neo4j 投影

    TODO: 实现实际的 Neo4j 索引逻辑
    """
    logger.info(f"Processing Neo4j projection for event: {event.id}")
    # 占位：实际需要调用 Neo4j 投影器
    pass


async def process_deletion_job(
    ctx: dict[str, Any],
    job_id: str,
) -> dict[str, Any]:
    """
    处理删除任务

    流程：
    1. 创建 tombstone
    2. 删除 ES 投影
    3. 删除 Neo4j 投影
    4. 删除 Blob 存储
    5. 更新任务状态
    """
    logger.info(f"Processing deletion job: {job_id}")

    # TODO: 实现删除传播逻辑

    return {"success": True, "job_id": job_id}


# ARQ Worker 配置
async def startup(ctx: dict[str, Any]) -> None:
    """Worker 启动"""
    ctx["worker_settings"] = get_worker_settings()
    logger.info("Worker started")


async def shutdown(ctx: dict[str, Any]) -> None:
    """Worker 关闭"""
    logger.info("Worker stopped")


class ARQWorkerConfig:
    """ARQ Worker 配置"""

    functions = [
        process_ingestion_job,
        process_outbox_events,
        process_deletion_job,
    ]

    on_startup = startup
    on_shutdown = shutdown

    # 定时任务
    cron_jobs = [
        cron(process_outbox_events, minute=0),  # 每分钟检查一次
    ]

    # Redis 配置
    redis_settings = Redis(
        host=get_worker_settings().redis_host,
        port=get_worker_settings().redis_port,
        database=get_worker_settings().redis_db,
    )

    # 并发配置
    max_jobs = get_worker_settings().max_concurrent_jobs
    job_timeout = get_worker_settings().job_timeout_seconds