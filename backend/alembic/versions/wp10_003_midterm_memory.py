"""
Agent Memory System - Mid-term Memory Migration
创建任务追踪表
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'wp10_003_midterm_memory'
down_revision = 'wp10_001_working_memory'  # 需要根据实际情况调整
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 创建任务表
    op.create_table(
        'tasks',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('session_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('sessions.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(20), server_default='pending'),  # pending, in_progress, completed, failed
        sa.Column('progress', sa.Integer(), server_default='0'),  # 0-100
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('task_metadata', postgresql.JSONB(), server_default='{}'),
    )

    # 创建索引
    op.create_index('idx_tasks_session', 'tasks', ['session_id'])
    op.create_index('idx_tasks_user', 'tasks', ['user_id'])
    op.create_index('idx_tasks_tenant', 'tasks', ['tenant_id'])
    op.create_index('idx_tasks_status', 'tasks', ['status'])

    # 创建任务步骤表
    op.create_table(
        'task_steps',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('task_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tasks.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('step_order', sa.Integer(), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('status', sa.String(20), server_default='pending'),
        sa.Column('result', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('step_metadata', postgresql.JSONB(), server_default='{}'),
    )

    # 创建索引
    op.create_index('idx_task_steps_task', 'task_steps', ['task_id'])
    op.create_index('idx_task_steps_order', 'task_steps', ['task_id', 'step_order'])

    # 创建任务结果表
    op.create_table(
        'task_results',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('task_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tasks.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('step_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('task_steps.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('result_type', sa.String(50)),  # text, json, file, etc.
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('result_metadata', postgresql.JSONB(), server_default='{}'),
    )

    # 创建索引
    op.create_index('idx_task_results_task', 'task_results', ['task_id'])
    op.create_index('idx_task_results_step', 'task_results', ['step_id'])


def downgrade() -> None:
    op.drop_index('idx_task_results_step')
    op.drop_index('idx_task_results_task')
    op.drop_table('task_results')

    op.drop_index('idx_task_steps_order')
    op.drop_index('idx_task_steps_task')
    op.drop_table('task_steps')

    op.drop_index('idx_tasks_status')
    op.drop_index('idx_tasks_tenant')
    op.drop_index('idx_tasks_user')
    op.drop_index('idx_tasks_session')
    op.drop_table('tasks')
