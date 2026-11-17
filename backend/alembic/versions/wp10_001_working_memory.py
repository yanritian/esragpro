"""
Agent Memory System - Working Memory Migration
创建会话和对话历史表
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'wp10_001_working_memory'
down_revision = None  # 需要根据实际情况调整
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 创建会话表
    op.create_table(
        'sessions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('session_metadata', postgresql.JSONB(), server_default='{}'),
        sa.Column('status', sa.String(20), server_default='active'),
    )

    # 创建索引
    op.create_index('idx_sessions_user', 'sessions', ['user_id'])
    op.create_index('idx_sessions_tenant', 'sessions', ['tenant_id'])
    op.create_index('idx_sessions_status', 'sessions', ['status'])
    op.create_index('idx_sessions_created_at', 'sessions', ['created_at'])

    # 创建对话历史表
    op.create_table(
        'conversations',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('session_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('sessions.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('role', sa.String(20), nullable=False),  # user, assistant, system
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('message_metadata', postgresql.JSONB(), server_default='{}'),
    )

    # 创建索引
    op.create_index('idx_conversations_session', 'conversations', ['session_id'])
    op.create_index('idx_conversations_timestamp', 'conversations', ['timestamp'])
    op.create_index('idx_conversations_role', 'conversations', ['role'])


def downgrade() -> None:
    op.drop_index('idx_conversations_role')
    op.drop_index('idx_conversations_timestamp')
    op.drop_index('idx_conversations_session')
    op.drop_table('conversations')

    op.drop_index('idx_sessions_created_at')
    op.drop_index('idx_sessions_status')
    op.drop_index('idx_sessions_tenant')
    op.drop_index('idx_sessions_user')
    op.drop_table('sessions')
