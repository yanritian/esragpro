"""WP4: Claim Graph - 可信 Claim 图谱

Revision ID: wp4_001
Revises: wp3_001
Create Date: 2026-07-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


revision: str = 'wp4_001'
down_revision: Union[str, None] = 'wp3_001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ontology Versions - 本体版本
    op.create_table(
        'ontology_versions',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('knowledge_base_id', UUID(as_uuid=True), sa.ForeignKey('knowledge_bases.id'), nullable=False),
        sa.Column('version', sa.String(50), nullable=False),
        sa.Column('schema_json', JSONB, nullable=False),
        sa.Column('status', sa.String(50), default='active', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_ontology_versions_tenant_id', 'ontology_versions', ['tenant_id'])
    op.create_index('ix_ontology_versions_knowledge_base_id', 'ontology_versions', ['knowledge_base_id'])

    # Entities - 规范化实体
    op.create_table(
        'entities',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('knowledge_base_id', UUID(as_uuid=True), sa.ForeignKey('knowledge_bases.id'), nullable=False),
        sa.Column('canonical_name', sa.String(500), nullable=False),
        sa.Column('entity_type', sa.String(100), nullable=False),
        sa.Column('revision', sa.Integer, default=1, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_entities_tenant_id', 'entities', ['tenant_id'])
    op.create_index('ix_entities_knowledge_base_id', 'entities', ['knowledge_base_id'])
    op.create_index('ix_entities_canonical_name', 'entities', ['canonical_name'])

    # Entity Aliases - 实体别名
    op.create_table(
        'entity_aliases',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('entity_id', UUID(as_uuid=True), sa.ForeignKey('entities.id'), nullable=False),
        sa.Column('alias', sa.String(500), nullable=False),
        sa.Column('normalized_alias', sa.String(500), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_entity_aliases_tenant_id', 'entity_aliases', ['tenant_id'])
    op.create_index('ix_entity_aliases_entity_id', 'entity_aliases', ['entity_id'])
    op.create_index('ix_entity_aliases_normalized_alias', 'entity_aliases', ['normalized_alias'])

    # Claim Candidates - 候选声明
    op.create_table(
        'claim_candidates',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('knowledge_base_id', UUID(as_uuid=True), sa.ForeignKey('knowledge_bases.id'), nullable=False),
        # 实体引用
        sa.Column('subject_entity_id', UUID(as_uuid=True), sa.ForeignKey('entities.id'), nullable=False),
        sa.Column('predicate', sa.String(200), nullable=False),
        # 对象
        sa.Column('object_kind', sa.String(50), nullable=False),
        sa.Column('object_entity_id', UUID(as_uuid=True), sa.ForeignKey('entities.id'), nullable=True),
        sa.Column('object_value_json', JSONB, nullable=True),
        sa.Column('object_fingerprint', sa.String(128), nullable=True),
        # 时态
        sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
        # 状态和置信度
        sa.Column('state', sa.String(50), default='extracted', nullable=False),
        sa.Column('confidence', sa.Numeric(5, 4), default=0.0),
        # 提取来源
        sa.Column('extraction_provenance', JSONB, nullable=True),
        sa.Column('ontology_version_id', UUID(as_uuid=True), sa.ForeignKey('ontology_versions.id'), nullable=True),
        sa.Column('revision', sa.Integer, default=1, server_default='1'),
        sa.Column('created_by', sa.String(255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_claim_candidates_tenant_id', 'claim_candidates', ['tenant_id'])
    op.create_index('ix_claim_candidates_knowledge_base_id', 'claim_candidates', ['knowledge_base_id'])
    op.create_index('ix_claim_candidates_state', 'claim_candidates', ['state'])
    op.create_index('ix_claim_candidates_subject_entity_id', 'claim_candidates', ['subject_entity_id'])

    # Candidate Evidence - 候选证据
    op.create_table(
        'candidate_evidence',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('candidate_id', UUID(as_uuid=True), sa.ForeignKey('claim_candidates.id'), nullable=False),
        sa.Column('version_id', UUID(as_uuid=True), sa.ForeignKey('document_versions.id'), nullable=False),
        sa.Column('chunk_id', UUID(as_uuid=True), nullable=False),
        sa.Column('start_char', sa.Integer, nullable=False),
        sa.Column('end_char', sa.Integer, nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_candidate_evidence_candidate_id', 'candidate_evidence', ['candidate_id'])
    op.create_index('ix_candidate_evidence_version_id', 'candidate_evidence', ['version_id'])

    # Claims - 权威声明
    op.create_table(
        'claims',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('knowledge_base_id', UUID(as_uuid=True), sa.ForeignKey('knowledge_bases.id'), nullable=False),
        sa.Column('approved_from_candidate_id', UUID(as_uuid=True), sa.ForeignKey('claim_candidates.id'), nullable=False),
        # 实体引用
        sa.Column('subject_entity_id', UUID(as_uuid=True), sa.ForeignKey('entities.id'), nullable=False),
        sa.Column('predicate', sa.String(200), nullable=False),
        # 对象
        sa.Column('object_kind', sa.String(50), nullable=False),
        sa.Column('object_entity_id', UUID(as_uuid=True), sa.ForeignKey('entities.id'), nullable=True),
        sa.Column('object_value_json', JSONB, nullable=True),
        sa.Column('object_fingerprint', sa.String(128), nullable=True),
        # 时态
        sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
        # 发布状态
        sa.Column('publication_state', sa.String(50), default='active', nullable=False),
        sa.Column('ontology_version_id', UUID(as_uuid=True), sa.ForeignKey('ontology_versions.id'), nullable=True),
        sa.Column('revision', sa.Integer, default=1, server_default='1'),
        sa.Column('created_by', sa.String(255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_claims_tenant_id', 'claims', ['tenant_id'])
    op.create_index('ix_claims_knowledge_base_id', 'claims', ['knowledge_base_id'])
    op.create_index('ix_claims_publication_state', 'claims', ['publication_state'])
    op.create_index('ix_claims_subject_entity_id', 'claims', ['subject_entity_id'])

    # Claim Evidence - 声明证据
    op.create_table(
        'claim_evidence',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('claim_id', UUID(as_uuid=True), sa.ForeignKey('claims.id'), nullable=False),
        sa.Column('version_id', UUID(as_uuid=True), sa.ForeignKey('document_versions.id'), nullable=False),
        sa.Column('chunk_id', UUID(as_uuid=True), nullable=False),
        sa.Column('start_char', sa.Integer, nullable=False),
        sa.Column('end_char', sa.Integer, nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_claim_evidence_claim_id', 'claim_evidence', ['claim_id'])

    # Claim Review Decisions - 审核决策
    op.create_table(
        'claim_review_decisions',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('candidate_id', UUID(as_uuid=True), sa.ForeignKey('claim_candidates.id'), nullable=False),
        sa.Column('reviewer_id', sa.String(255), nullable=False),
        sa.Column('action', sa.String(50), nullable=False),
        sa.Column('reason', sa.Text, nullable=False),
        sa.Column('expected_revision', sa.Integer, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_claim_review_decisions_candidate_id', 'claim_review_decisions', ['candidate_id'])

    # Claim Conflicts - 声明冲突
    op.create_table(
        'claim_conflicts',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('left_candidate_id', UUID(as_uuid=True), sa.ForeignKey('claim_candidates.id'), nullable=False),
        sa.Column('right_candidate_id', UUID(as_uuid=True), sa.ForeignKey('claim_candidates.id'), nullable=False),
        sa.Column('rule', sa.String(100), nullable=False),
        sa.Column('state', sa.String(50), default='detected', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_claim_conflicts_tenant_id', 'claim_conflicts', ['tenant_id'])
    op.create_index('ix_claim_conflicts_left_candidate_id', 'claim_conflicts', ['left_candidate_id'])
    op.create_index('ix_claim_conflicts_right_candidate_id', 'claim_conflicts', ['right_candidate_id'])


def downgrade() -> None:
    op.drop_index('ix_claim_conflicts_right_candidate_id', 'claim_conflicts')
    op.drop_index('ix_claim_conflicts_left_candidate_id', 'claim_conflicts')
    op.drop_index('ix_claim_conflicts_tenant_id', 'claim_conflicts')
    op.drop_table('claim_conflicts')

    op.drop_index('ix_claim_review_decisions_candidate_id', 'claim_review_decisions')
    op.drop_table('claim_review_decisions')

    op.drop_index('ix_claim_evidence_claim_id', 'claim_evidence')
    op.drop_table('claim_evidence')

    op.drop_index('ix_claims_subject_entity_id', 'claims')
    op.drop_index('ix_claims_publication_state', 'claims')
    op.drop_index('ix_claims_knowledge_base_id', 'claims')
    op.drop_index('ix_claims_tenant_id', 'claims')
    op.drop_table('claims')

    op.drop_index('ix_candidate_evidence_version_id', 'candidate_evidence')
    op.drop_index('ix_candidate_evidence_candidate_id', 'candidate_evidence')
    op.drop_table('candidate_evidence')

    op.drop_index('ix_claim_candidates_subject_entity_id', 'claim_candidates')
    op.drop_index('ix_claim_candidates_state', 'claim_candidates')
    op.drop_index('ix_claim_candidates_knowledge_base_id', 'claim_candidates')
    op.drop_index('ix_claim_candidates_tenant_id', 'claim_candidates')
    op.drop_table('claim_candidates')

    op.drop_index('ix_entity_aliases_normalized_alias', 'entity_aliases')
    op.drop_index('ix_entity_aliases_entity_id', 'entity_aliases')
    op.drop_index('ix_entity_aliases_tenant_id', 'entity_aliases')
    op.drop_table('entity_aliases')

    op.drop_index('ix_entities_canonical_name', 'entities')
    op.drop_index('ix_entities_knowledge_base_id', 'entities')
    op.drop_index('ix_entities_tenant_id', 'entities')
    op.drop_table('entities')

    op.drop_index('ix_ontology_versions_knowledge_base_id', 'ontology_versions')
    op.drop_index('ix_ontology_versions_tenant_id', 'ontology_versions')
    op.drop_table('ontology_versions')