"""
Upload Session Model - 上传会话模型

WP3 核心流程：
1. created → uploaded → finalized → consumed
2. source_ref 供 IngestionJob 单次消费
3. 过期自动清理
"""
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import String, Integer, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UploadSessionState:
    """上传会话状态"""
    CREATED = "created"
    UPLOADED = "uploaded"
    FINALIZED = "finalized"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    ABORTED = "aborted"


class UploadSession(Base):
    """
    上传会话模型

    安全文件摄取流程：
    1. 客户端创建会话（created）
    2. 客户端上传内容（uploaded）
    3. 客户端确认完成（finalized）-> 生成 source_ref
    4. IngestionJob 消费（consumed）-> source_ref 失效
    5. 过期自动清理（expired）
    """
    __tablename__ = "upload_sessions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False)

    # 文件信息
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # 状态
    state: Mapped[str] = mapped_column(String(50), default=UploadSessionState.CREATED, nullable=False)

    # source_ref（finalized 后生成，consumed 后失效）
    source_ref: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # 临时存储位置（uploaded 后填充）
    storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # 过期时间（默认 24 小时）
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.utcnow() + timedelta(hours=24),
        nullable=False
    )

    # 创建者和时间戳
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_upload_sessions_tenant_id", "tenant_id"),
        Index("ix_upload_sessions_knowledge_base_id", "knowledge_base_id"),
        Index("ix_upload_sessions_state", "state"),
        Index("ix_upload_sessions_expires_at", "expires_at"),
    )