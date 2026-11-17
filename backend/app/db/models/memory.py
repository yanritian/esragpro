"""
Agent Memory System - Working Memory Models
会话和对话历史的 SQLAlchemy 模型
"""
from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import String, Text, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class Session(Base):
    """会话模型"""
    __tablename__: str = 'sessions'

    id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), nullable=False, index=True)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    session_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default='active', index=True)  # active, expired, closed

    # 关系
    conversations: Mapped[list["Conversation"]] = relationship('Conversation', back_populates='session', cascade='all, delete-orphan')

    __table_args__ = (
        Index('idx_sessions_user_tenant', 'user_id', 'tenant_id'),
        Index('idx_sessions_created_at', 'created_at'),
    )


class Conversation(Base):
    """对话历史模型"""
    __tablename__: str = 'conversations'

    id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user, assistant, system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    message_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # 关系
    session: Mapped[Optional["Session"]] = relationship('Session', back_populates='conversations')

    __table_args__ = (
        Index('idx_conversations_session_timestamp', 'session_id', 'timestamp'),
        Index('idx_conversations_role', 'role'),
    )