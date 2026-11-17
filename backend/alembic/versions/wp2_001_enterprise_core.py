"""WP2: Enterprise core - tenant, user, knowledge base, document, ACL

Revision ID: wp2_001
Revises:
Create Date: 2026-07-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


revision: str = 'wp2_001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Tenants
    op.create_table(
        'tenants',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('slug', sa.String(100), unique=True, nullable=False),
        sa.Column('is_active', sa.Boolean, default=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.func.now()),
    )
    op.create_index('ix_tenants_slug', 'tenants', ['slug'])

    # Users
    op.create_table(
        'users',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('username', sa.String(255), nullable=False),
        sa.Column('email', sa.String(255)),
        sa.Column('password_hash', sa.String(255), nullable=False),
        sa.Column('role', sa.String(50), default='user'),
        sa.Column('is_active', sa.Boolean, default=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.func.now()),
        sa.UniqueConstraint('tenant_id', 'username', name='uq_users_tenant_username'),
    )
    op.create_index('ix_users_tenant_id', 'users', ['tenant_id'])

    # Knowledge Bases
    op.create_table(
        'knowledge_bases',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('slug', sa.String(100), nullable=False),
        sa.Column('description', sa.Text),
        sa.Column('is_active', sa.Boolean, default=True),
        sa.Column('ontology_version', sa.String(50)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.func.now()),
        sa.UniqueConstraint('tenant_id', 'slug', name='uq_knowledge_bases_tenant_slug'),
    )
    op.create_index('ix_knowledge_bases_tenant_id', 'knowledge_bases', ['tenant_id'])

    # Memberships
    op.create_table(
        'memberships',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('knowledge_base_id', UUID(as_uuid=True), sa.ForeignKey('knowledge_bases.id'), nullable=False),
        sa.Column('role', sa.String(50), default='reader'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('user_id', 'knowledge_base_id', name='uq_memberships_user_kb'),
    )
    op.create_index('ix_memberships_user_id', 'memberships', ['user_id'])
    op.create_index('ix_memberships_kb_id', 'memberships', ['knowledge_base_id'])

    # Documents
    op.create_table(
        'documents',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('knowledge_base_id', UUID(as_uuid=True), sa.ForeignKey('knowledge_bases.id'), nullable=False),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('source', sa.String(500), nullable=False),
        sa.Column('doc_type', sa.String(50), nullable=False),
        sa.Column('is_active', sa.Boolean, default=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.func.now()),
    )
    op.create_index('ix_documents_tenant_id', 'documents', ['tenant_id'])
    op.create_index('ix_documents_kb_id', 'documents', ['knowledge_base_id'])

    # Document Versions
    op.create_table(
        'document_versions',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('document_id', UUID(as_uuid=True), sa.ForeignKey('documents.id'), nullable=False),
        sa.Column('version_number', sa.Integer, nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('storage_key', sa.String(500), nullable=False),
        sa.Column('size_bytes', sa.Integer, nullable=False),
        sa.Column('state', sa.String(50), default='uploaded'),
        sa.Column('metadata', JSONB),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('document_id', 'version_number', name='uq_document_versions_doc_version'),
    )
    op.create_index('ix_document_versions_document_id', 'document_versions', ['document_id'])

    # Authorization Revisions
    op.create_table(
        'authorization_revisions',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('change_type', sa.String(50), nullable=False),
        sa.Column('subject_id', sa.String(255), nullable=False),
        sa.Column('details', JSONB),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_auth_revisions_tenant_id', 'authorization_revisions', ['tenant_id'])
    op.create_index('ix_auth_revisions_revision', 'authorization_revisions', ['revision'])

    # Revocation Barriers
    op.create_table(
        'revocation_barriers',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('knowledge_base_id', UUID(as_uuid=True), sa.ForeignKey('knowledge_bases.id')),
        sa.Column('document_id', UUID(as_uuid=True), sa.ForeignKey('documents.id')),
        sa.Column('authorization_revision', sa.Integer, nullable=False),
        sa.Column('barrier_type', sa.String(50), nullable=False),
        sa.Column('is_active', sa.Boolean, default=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('cleared_at', sa.DateTime(timezone=True)),
    )
    op.create_index('ix_revocation_barriers_tenant_id', 'revocation_barriers', ['tenant_id'])
    op.create_index('ix_revocation_barriers_kb_id', 'revocation_barriers', ['knowledge_base_id'])
    op.create_index('ix_revocation_barriers_doc_id', 'revocation_barriers', ['document_id'])

    # Knowledge Base Grants
    op.create_table(
        'knowledge_base_grants',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('knowledge_base_id', UUID(as_uuid=True), sa.ForeignKey('knowledge_bases.id'), nullable=False),
        sa.Column('subject_type', sa.String(50), nullable=False),
        sa.Column('subject_id', sa.String(255), nullable=False),
        sa.Column('permission', sa.String(50), nullable=False),
        sa.Column('is_active', sa.Boolean, default=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('knowledge_base_id', 'subject_type', 'subject_id', 'permission', name='uq_kb_grants'),
    )
    op.create_index('ix_kb_grants_kb_id', 'knowledge_base_grants', ['knowledge_base_id'])
    op.create_index('ix_kb_grants_subject', 'knowledge_base_grants', ['subject_type', 'subject_id'])


def downgrade() -> None:
    op.drop_table('knowledge_base_grants')
    op.drop_table('revocation_barriers')
    op.drop_table('authorization_revisions')
    op.drop_table('document_versions')
    op.drop_table('documents')
    op.drop_table('memberships')
    op.drop_table('knowledge_bases')
    op.drop_table('users')
    op.drop_table('tenants')