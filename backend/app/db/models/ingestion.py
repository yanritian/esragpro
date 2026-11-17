"""
Ingestion Job - 异步摄取任务模型

WP3 核心概念：
- 原始文档先登记 document/document_version，再创建 ingestion_job
- 上传 API 不同步执行 OCR、embedding 和图抽取
- 使用状态机管理摄取流程
"""
from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import String, DateTime, Text, ForeignKey
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IngestionJob(Base):
    """
    摄取任务模型

    状态机：
    pending -> processing -> completed
           -> processing -> failed (可重试)
           -> cancelled
    """
    __tablename__ = "ingestion_jobs"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)
    version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("document_versions.id"), nullable=False)

    # 幂等键：防止重复提交
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)

    # 状态机
    state: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    # pending, processing, completed, failed, cancelled

    # 重试控制
    attempt: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=5)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 来源引用（从上传会话消费）
    source_ref: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # 元数据（避免与 SQLAlchemy metadata 冲突）
    extra_data: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSONB, nullable=True)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # 幂等键唯一约束
        # 同一 tenant + idempotency_key 只能有一个 job
        # 注意：需要在迁移中创建唯一索引
    )


class IngestionJobOutbox(Base):
    """
    Outbox 模式：驱动 ES 与 Neo4j 投影

    事务边界：
    - 在 PG 事务中写入 job 状态变更和 outbox 事件
    - 异步 worker 读取 outbox 并执行投影更新
    """
    __tablename__ = "ingestion_job_outbox"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)

    # 关联的 job
    job_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("ingestion_jobs.id"), nullable=False)

    # 事件类型
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # job.created, job.started, es.index.requested, es.index.completed,
    # neo4j.index.requested, neo4j.index.completed, job.completed, job.failed

    # 事件载荷
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # 投影状态
    projection_target: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # elasticsearch, neo4j, cache

    projection_state: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    # pending, processing, completed, failed

    # 投影元数据
    projection_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class DeletionJob(Base):
    """
    删除任务模型

    删除传播：
    - 产生 tombstone
    - 传播至对象存储、ES、Neo4j、缓存
    - 失败可重放
    """
    __tablename__ = "deletion_jobs"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)

    state: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    # pending, processing, completed, failed

    # 幂等键
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)

    # 删除范围
    delete_chunks: Mapped[bool] = mapped_column(default=True)
    delete_versions: Mapped[bool] = mapped_column(default=True)
    delete_storage: Mapped[bool] = mapped_column(default=True)

    # Revocation Barrier（立即阻断旧访问）
    barrier_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


# 状态枚举
class IngestionJobState:
    """摄取任务状态"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutboxEvent:
    """Outbox 事件类型"""
    JOB_CREATED = "job.created"
    JOB_STARTED = "job.started"
    ES_INDEX_REQUESTED = "es.index.requested"
    ES_INDEX_COMPLETED = "es.index.completed"
    NEO4J_INDEX_REQUESTED = "neo4j.index.requested"
    NEO4J_INDEX_COMPLETED = "neo4j.index.completed"
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"


class ProjectionTarget:
    """投影目标"""
    ELASTICSEARCH = "elasticsearch"
    NEO4J = "neo4j"
    CACHE = "cache"