"""WP3: Asynchronous ingestion - jobs, outbox, deletion

Revision ID: wp3_001
Revises: wp2_001
Create Date: 2026-07-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


revision: str = 'wp3_001'
down_revision: Union[str, None] = 'wp2_001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ingestion Jobs - 异步摄取任务
    op.create_table(
        'ingestion_jobs',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('knowledge_base_id', UUID(as_uuid=True), sa.ForeignKey('knowledge_bases.id'), nullable=False),
        sa.Column('document_id', UUID(as_uuid=True), sa.ForeignKey('documents.id'), nullable=False),
        sa.Column('version_id', UUID(as_uuid=True), sa.ForeignKey('document_versions.id'), nullable=False),
        sa.Column('idempotency_key', sa.String(255), nullable=False),
        sa.Column('state', sa.String(50), default='pending', nullable=False),
        # 重试控制
        sa.Column('attempt', sa.Integer, default=0),
        sa.Column('max_attempts', sa.Integer, default=5),
        sa.Column('next_retry_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error_code', sa.String(100), nullable=True),
        sa.Column('last_error_message', sa.Text, nullable=True),
        # 来源引用
        sa.Column('source_ref', sa.String(500), nullable=True),
        # 元数据 (列名为 metadata，避免与 SQLAlchemy metadata 冲突)
        sa.Column('metadata', JSONB, nullable=True),
        # 时间戳
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        # 幂等键唯一约束：同一 tenant + idempotency_key 只能有一个 job
        sa.UniqueConstraint('tenant_id', 'idempotency_key', name='uq_ingestion_jobs_tenant_idempotency'),
    )
    op.create_index('ix_ingestion_jobs_tenant_id', 'ingestion_jobs', ['tenant_id'])
    op.create_index('ix_ingestion_jobs_knowledge_base_id', 'ingestion_jobs', ['knowledge_base_id'])
    op.create_index('ix_ingestion_jobs_state', 'ingestion_jobs', ['state'])
    op.create_index('ix_ingestion_jobs_next_retry_at', 'ingestion_jobs', ['next_retry_at'])

    # Ingestion Job Outbox - Outbox 模式驱动投影
    op.create_table(
        'ingestion_job_outbox',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('job_id', UUID(as_uuid=True), sa.ForeignKey('ingestion_jobs.id'), nullable=False),
        sa.Column('event_type', sa.String(100), nullable=False),
        sa.Column('payload', JSONB, nullable=False),
        # 投影状态
        sa.Column('projection_target', sa.String(50), nullable=True),
        sa.Column('projection_state', sa.String(50), default='pending', nullable=False),
        sa.Column('projection_metadata', JSONB, nullable=True),
        # 时间戳
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_ingestion_job_outbox_job_id', 'ingestion_job_outbox', ['job_id'])
    op.create_index('ix_ingestion_job_outbox_event_type', 'ingestion_job_outbox', ['event_type'])
    op.create_index('ix_ingestion_job_outbox_projection_state', 'ingestion_job_outbox', ['projection_state'])

    # Deletion Jobs - 删除任务
    op.create_table(
        'deletion_jobs',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('document_id', UUID(as_uuid=True), sa.ForeignKey('documents.id'), nullable=False),
        sa.Column('state', sa.String(50), default='pending', nullable=False),
        sa.Column('idempotency_key', sa.String(255), nullable=False),
        # 删除范围
        sa.Column('delete_chunks', sa.Boolean, default=True),
        sa.Column('delete_versions', sa.Boolean, default=True),
        sa.Column('delete_storage', sa.Boolean, default=True),
        # Revocation Barrier
        sa.Column('barrier_id', UUID(as_uuid=True), nullable=True),
        # 时间戳
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('tenant_id', 'idempotency_key', name='uq_deletion_jobs_tenant_idempotency'),
    )
    op.create_index('ix_deletion_jobs_tenant_id', 'deletion_jobs', ['tenant_id'])
    op.create_index('ix_deletion_jobs_state', 'deletion_jobs', ['state'])

    # Upload Sessions - 上传会话
    op.create_table(
        'upload_sessions',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('knowledge_base_id', UUID(as_uuid=True), sa.ForeignKey('knowledge_bases.id'), nullable=False),
        # 文件信息
        sa.Column('filename', sa.String(255), nullable=False),
        sa.Column('media_type', sa.String(100), nullable=False),
        sa.Column('size_bytes', sa.Integer, nullable=False),
        sa.Column('sha256', sa.String(64), nullable=True),
        # 状态
        sa.Column('state', sa.String(50), default='created', nullable=False),
        # source_ref（finalized 后生成，consumed 后失效）
        sa.Column('source_ref', sa.String(500), nullable=True),
        # 临时存储位置
        sa.Column('storage_key', sa.String(500), nullable=True),
        # 过期时间
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        # 创建者和时间戳
        sa.Column('created_by', sa.String(255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_upload_sessions_tenant_id', 'upload_sessions', ['tenant_id'])
    op.create_index('ix_upload_sessions_knowledge_base_id', 'upload_sessions', ['knowledge_base_id'])
    op.create_index('ix_upload_sessions_state', 'upload_sessions', ['state'])
    op.create_index('ix_upload_sessions_expires_at', 'upload_sessions', ['expires_at'])


def downgrade() -> None:
    op.drop_index('ix_upload_sessions_expires_at', 'upload_sessions')
    op.drop_index('ix_upload_sessions_state', 'upload_sessions')
    op.drop_index('ix_upload_sessions_knowledge_base_id', 'upload_sessions')
    op.drop_index('ix_upload_sessions_tenant_id', 'upload_sessions')
    op.drop_table('upload_sessions')

    op.drop_index('ix_deletion_jobs_state', 'deletion_jobs')
    op.drop_index('ix_deletion_jobs_tenant_id', 'deletion_jobs')
    op.drop_table('deletion_jobs')

    op.drop_index('ix_ingestion_job_outbox_projection_state', 'ingestion_job_outbox')
    op.drop_index('ix_ingestion_job_outbox_event_type', 'ingestion_job_outbox')
    op.drop_index('ix_ingestion_job_outbox_job_id', 'ingestion_job_outbox')
    op.drop_table('ingestion_job_outbox')

    op.drop_index('ix_ingestion_jobs_next_retry_at', 'ingestion_jobs')
    op.drop_index('ix_ingestion_jobs_state', 'ingestion_jobs')
    op.drop_index('ix_ingestion_jobs_knowledge_base_id', 'ingestion_jobs')
    op.drop_index('ix_ingestion_jobs_tenant_id', 'ingestion_jobs')
    op.drop_table('ingestion_jobs')