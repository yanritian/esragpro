"""
Agent Memory System - Mid-term Memory Models
任务追踪的 SQLAlchemy 模型
"""
from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import String, Text, DateTime, Integer, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class Task(Base):
    """任务模型"""
    __tablename__: str = 'tasks'

    id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False)
    user_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), nullable=False, index=True)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)  # pending, in_progress, completed, failed
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    task_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # 关系
    steps: Mapped[list["TaskStep"]] = relationship('TaskStep', back_populates='task', cascade='all, delete-orphan')
    results: Mapped[list["TaskResult"]] = relationship('TaskResult', back_populates='task', cascade='all, delete-orphan')

    __table_args__ = (
        Index('idx_tasks_session_status', 'session_id', 'status'),
    )


class TaskStep(Base):
    """任务步骤模型"""
    __tablename__: str = 'task_steps'

    id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default='pending')  # pending, in_progress, completed, failed
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    step_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # 关系
    task: Mapped[Optional["Task"]] = relationship('Task', back_populates='steps')

    __table_args__ = (
        Index('idx_task_steps_task_order', 'task_id', 'step_order'),
    )


class TaskResult(Base):
    """任务结果模型"""
    __tablename__: str = 'task_results'

    id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False)
    step_id: Mapped[Optional[UUID]] = mapped_column(PGUUID[UUID](as_uuid=True), ForeignKey('task_steps.id', ondelete='SET NULL'), nullable=True)
    result_type: Mapped[str] = mapped_column(String(50))  # text, json, file, etc.
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    result_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # 关系
    task: Mapped[Optional["Task"]] = relationship('Task', back_populates='results')